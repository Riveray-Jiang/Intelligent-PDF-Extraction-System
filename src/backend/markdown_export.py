from __future__ import annotations

import re

from .types import Block
from .types import DocumentIR
from .types import Page


def _sorted_blocks(blocks: list[Block]) -> list[Block]:
    return sorted(blocks, key=lambda block: (block.order is None, block.order if block.order is not None else 10**9))


def _normalize_text(text: str) -> str:
    return text.strip()


def _normalize_heading_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return normalized
    normalized = re.sub(r"^(\d+(?:\.\d+)*\.?)(?=[A-Z])", r"\1 ", normalized)
    normalized = re.sub(r"^([IVXLC]+\.?)(?=[A-Z])", r"\1 ", normalized)
    return normalized


def _source_raw(block: Block) -> dict:
    raw = block.source.get("raw") if isinstance(block.source, dict) else None
    return raw if isinstance(raw, dict) else {}


def _source_text_list(block: Block, key: str) -> list[str]:
    value = _source_raw(block).get(key)
    if isinstance(value, str):
        text = _normalize_text(value)
        return [text] if text else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            text = _normalize_text(str(item))
            if text:
                result.append(text)
        return result
    return []


def _has_table_shell_evidence(block: Block) -> bool:
    raw = _source_raw(block)
    return bool(block.bbox or raw.get("bbox") or raw.get("img_path") or raw.get("table_caption"))


def _render_hard_break_lines(text: str) -> str:
    paragraphs: list[str] = []
    current_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current_lines:
                paragraphs.append("  \n".join(current_lines))
                current_lines = []
            continue
        current_lines.append(line)
    if current_lines:
        paragraphs.append("  \n".join(current_lines))
    return "\n\n".join(paragraphs)


def _reflow_definition_block(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\s*\n\s*", "\n", normalized)
    normalized = re.sub(r"\s*(式中[:：]|其中[:：]|where[:：]?)\s*", r"\n\1\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"([；;。])\s*(?=(?:[A-Za-z][A-Za-z0-9_./（）()]{0,20}\s*[—–\-]|L[pwaA][A-Za-z0-9_./（）()]{0,18}\s*[（(]))",
        r"\1\n",
        normalized,
    )
    normalized = re.sub(
        r"([。])\s*(?=(?:[A-Za-z][A-Za-z0-9_./（）()]{0,20}\s*(?:[$＝=]|[$]\s*[＝=])))",
        r"\1\n",
        normalized,
    )
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _looks_like_definition_block(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    flat_text = " ".join(lines) if lines else text.strip()
    if len(lines) > 18:
        return False

    if len(lines) >= 3:
        short_lines = sum(len(line) <= 90 for line in lines)
        if short_lines < max(3, int(len(lines) * 0.7)):
            return False

    definition_markers = 0
    trailing_definition_punctuation = 0
    formula_context = False

    for line in lines or [flat_text]:
        if re.match(r"^(?:[A-Za-z][A-Za-z0-9_./()（）+\-]{0,20}|L[pwaA][A-Za-z0-9_()（）+\-]{0,18})\s*[—–\-=:：]", line):
            definition_markers += 1
        definition_markers += len(re.findall(r"(?:^|[；;。]\s*)(?:[A-Za-z][A-Za-z0-9_./()（）]{0,20}|L[pwaA][A-Za-z0-9_()（）]{0,18})\s*[—–\-]", line))
        if "——" in line or "式中：" in line or "其中：" in line:
            definition_markers += 1
        if line.startswith(("式中", "其中", "where", "Where")):
            formula_context = True
        if re.search(r"[;；:：]\s*$", line):
            trailing_definition_punctuation += 1

    if definition_markers >= 2:
        return True
    if definition_markers >= 3 and re.search(r"[；;]", flat_text):
        return True
    if formula_context and (definition_markers >= 1 or trailing_definition_punctuation >= 2):
        return True
    return False


def _render_block(block: Block) -> str:
    text = _normalize_text(block.text)
    heading_text = _normalize_heading_text(text)
    block_type = block.type.lower()
    if block.heading_level is not None or block.semantic_type == "heading":
        if not heading_text:
            return ""
        heading_level = block.heading_level if block.heading_level is not None else 3
        heading_level = max(3, min(6, heading_level))
        return f"{'#' * heading_level} {heading_text}"
    if block_type in {"paragraph_title", "title", "section_title", "heading"}:
        if not heading_text:
            return ""
        return f"### {heading_text}"
    if block_type == "text":
        legacy_heading_level = _legacy_heading_level(text)
        if legacy_heading_level is not None:
            return f"{'#' * legacy_heading_level} {heading_text}"
        reflowed_definition = _reflow_definition_block(text)
        if _looks_like_definition_block(text) or (
            reflowed_definition != text
            and reflowed_definition.count("\n") >= 3
            and re.search(r"(?:式中[:：]|其中[:：]|[；;])", text)
        ):
            return _render_hard_break_lines(reflowed_definition)
    if block_type in {"figure_title", "image_caption"}:
        if not text:
            return ""
        return f"**{text}**"
    if block_type == "table":
        if not text:
            if _has_table_shell_evidence(block):
                captions = [f"**{caption}**" for caption in _source_text_list(block, "table_caption")]
                return "\n\n".join([*captions, "> [Table detected]"])
            return ""
        pieces: list[str] = []
        pieces.extend(f"**{caption}**" for caption in _source_text_list(block, "table_caption"))
        # Raw HTML is valid inside Markdown and preserves structure better
        # than flattening the table into plain text.
        pieces.append(text)
        pieces.extend(_source_text_list(block, "table_footnote"))
        return "\n\n".join(pieces)
    if block_type in {"image", "figure", "image_body"}:
        captions = _source_text_list(block, "image_caption")
        if captions:
            return "\n".join(f"> {caption}" for caption in captions)
        source = block.source if isinstance(block.source, dict) else {}
        fallback_markdown = str(source.get("fallback_extracted_markdown") or "").strip()
        if fallback_markdown:
            return fallback_markdown
        return "> [Image content present]"
    if block_type == "image_interpretation":
        if not text:
            return ""
        language = ""
        if isinstance(block.source, dict):
            language = str(block.source.get("language", "")).strip().lower()
        title = "Image Agent interpretation"
        if language.startswith("zh"):
            title = "Image Agent \u89e3\u8bfb"
        quote_lines = [f"> **{title}**", ">"]
        for line in text.splitlines():
            stripped = line.rstrip()
            quote_lines.append(f"> {stripped}" if stripped else ">")
        return "\n".join(quote_lines)
    if block_type in {"formula", "equation"}:
        if not text:
            return ""
        if text.startswith("$$") and text.endswith("$$"):
            return text
        if text.startswith("$") and text.endswith("$"):
            return text
        return f"$$\n{text}\n$$"
    if not text:
        return ""
    return text


def _max_page_extent(page: Page) -> float | None:
    bottoms = [float(block.bbox[3]) for block in page.blocks if block.bbox and len(block.bbox) >= 4]
    if not bottoms:
        return None
    return max(bottoms)


def _looks_like_page_marker(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if re.fullmatch(r"[-–—]?\s*\d+\s*[-–—]?", normalized):
        return True
    if re.fullmatch(r"page\s+\d+", normalized, flags=re.IGNORECASE):
        return True
    return False


def _legacy_heading_level(text: str) -> int | None:
    normalized = _normalize_heading_text(text.strip())
    if not normalized or len(normalized) > 180:
        return None
    if "\n" in normalized:
        return None
    if normalized.endswith((".", ";", ":", "?", "!")) and not re.match(r"^\d+(?:\.\d+)*\.\s+\S+", normalized):
        return None

    numbered_heading = re.match(r"^\d+(?:\.\d+)*\.?\s+\S+", normalized)
    roman_heading = re.match(r"^[IVXLC]+\.?\s+\S+", normalized)
    plain_heading = normalized in {
        "Abstract",
        "Introduction",
        "Background",
        "Methods",
        "Methodology",
        "Results",
        "Discussion",
        "Conclusion",
        "Conclusions",
        "References",
    }
    if numbered_heading:
        depth = len(numbered_heading.group(0).split()[0].rstrip(".").split("."))
        return max(3, min(6, 2 + depth))
    if roman_heading or plain_heading:
        return 3
    return None


def _is_edge_number_block(block: Block, page: Page) -> bool:
    if block.type.lower() not in {"number", "page_number"}:
        return False
    if not _looks_like_page_marker(block.text):
        return False
    if not block.bbox or len(block.bbox) < 4:
        return True
    max_extent = _max_page_extent(page)
    if not max_extent:
        return True
    top = float(block.bbox[1])
    bottom = float(block.bbox[3])
    return top <= max_extent * 0.12 or bottom >= max_extent * 0.88


def _preview_blocks(page: Page) -> list[Block]:
    filtered: list[Block] = []
    for block in _sorted_blocks(page.blocks):
        block_type = block.type.lower()
        if block_type in {"header", "footer", "discarded", "image_interpretation"}:
            continue
        if _is_edge_number_block(block, page):
            continue
        filtered.append(block)
    return filtered


def page_to_markdown(page: Page) -> str:
    lines: list[str] = []
    rendered_any = False
    for block in _sorted_blocks(page.blocks):
        rendered = _render_block(block)
        if not rendered:
            continue
        lines.append(rendered)
        rendered_any = True
    if not rendered_any:
        return "_No extracted content on this page._"
    return "\n\n".join(lines)


def page_to_preview_markdown(page: Page) -> str:
    preview_page = Page(
        page_index=page.page_index,
        width=page.width,
        height=page.height,
        blocks=_preview_blocks(page),
    )
    return page_to_markdown(preview_page)


def document_ir_to_markdown(document_ir: DocumentIR) -> str:
    lines: list[str] = [
        f"# {document_ir.doc_id}",
        "",
        f"- source_file: `{document_ir.source_file}`",
        f"- source_engine: `{document_ir.source_engine}`",
        f"- generated_at: `{document_ir.generated_at}`",
    ]

    for page in sorted(document_ir.pages, key=lambda page: page.page_index):
        lines.extend(["", f"## Page {page.page_index + 1}", ""])
        lines.append(page_to_markdown(page))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
