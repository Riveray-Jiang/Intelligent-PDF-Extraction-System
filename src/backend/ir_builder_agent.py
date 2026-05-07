from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import re
from typing import Any

from .types import Block
from .types import DocumentIR
from .types import Page


class IRBuilderAgent:
    """Build engine-agnostic DocumentIR from raw engine outputs."""

    _EXPLICIT_HEADING_TYPES = {"title", "section_title", "paragraph_title", "heading"}
    _PLAIN_HEADING_TEXTS = {
        "abstract",
        "introduction",
        "background",
        "methods",
        "methodology",
        "results",
        "discussion",
        "conclusion",
        "conclusions",
        "references",
    }

    def run(self, engine: str, raw_output: dict) -> DocumentIR:
        engine = engine.lower()
        if engine == "paddle":
            return self._from_paddle(raw_output)
        if engine == "mineru":
            return self._from_mineru(raw_output)
        raise ValueError(f"Unsupported engine: {engine}")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _norm_bbox(value: Any) -> list[float] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        out: list[float] = []
        for item in value:
            try:
                out.append(float(item))
            except (TypeError, ValueError):
                return None
        return out

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _numbered_heading_depth(self, text: str) -> int | None:
        normalized = text.strip()
        match = re.match(r"^(\d+(?:\.\d+)*)\.?\s+\S+", normalized)
        if match:
            return len(match.group(1).split("."))
        if re.match(r"^[IVXLC]+\.?\s+\S+", normalized):
            return 1
        return None

    def _looks_like_heading_text(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized or len(normalized) > 180 or "\n" in normalized:
            return False
        if normalized.lower() in self._PLAIN_HEADING_TEXTS:
            return True
        if self._numbered_heading_depth(normalized) is not None:
            return True
        return False

    def _heading_level_from_signals(self, block_type: str, text: str, raw: dict[str, Any]) -> int | None:
        normalized = text.strip()
        if not normalized:
            return None

        candidates: list[int] = []
        raw_text_level = self._to_int(raw.get("text_level") or raw.get("level") or raw.get("heading_level"))
        if raw_text_level is not None and raw_text_level > 0:
            candidates.append(max(3, min(6, 2 + raw_text_level)))

        numbering_depth = self._numbered_heading_depth(normalized)
        if numbering_depth is not None:
            candidates.append(max(3, min(6, 2 + numbering_depth)))

        lowered_type = block_type.lower().strip()
        if lowered_type == "paragraph_title":
            candidates.append(4)
        elif lowered_type in {"title", "section_title", "heading"}:
            candidates.append(3)
        elif lowered_type == "text" and normalized.lower() in self._PLAIN_HEADING_TEXTS:
            candidates.append(3)

        if not candidates:
            return None
        return max(candidates)

    def _build_block(
        self,
        *,
        block_id: str,
        block_type: str,
        text: str,
        bbox: list[float] | None,
        order: int | None,
        confidence: float | None,
        page_index: int,
        engine: str,
        raw: dict[str, Any],
    ) -> Block:
        heading_level = None
        raw_text_level = self._to_int(raw.get("text_level") or raw.get("level") or raw.get("heading_level"))
        if raw_text_level is not None or block_type.lower() in self._EXPLICIT_HEADING_TYPES or self._looks_like_heading_text(text):
            heading_level = self._heading_level_from_signals(block_type, text, raw)

        semantic_type = "heading" if heading_level is not None else None
        source = {
            "engine": engine,
            "raw": raw,
        }
        if semantic_type:
            source["semantic_type_inferred"] = semantic_type
        if heading_level is not None:
            source["heading_level_inferred"] = heading_level

        return Block(
            id=f"p{page_index}_b{block_id}",
            type=block_type,
            text=text,
            bbox=bbox,
            order=order,
            confidence=confidence,
            semantic_type=semantic_type,
            heading_level=heading_level,
            source=source,
            page_index=page_index,
        )

    def _from_paddle(self, raw_output: dict) -> DocumentIR:
        source_file = str(raw_output.get("source_file", ""))
        doc_id = str(raw_output.get("doc_id") or raw_output.get("doc_name") or "unknown_doc")

        pages_data = raw_output.get("pages")
        if pages_data is None:
            pages_data = raw_output.get("paddle_pages", [])

        pages: list[Page] = []
        for i, page_data in enumerate(pages_data):
            page_index = int(page_data.get("page_index", i))
            blocks_data = page_data.get("blocks")
            if blocks_data is None:
                blocks_data = page_data.get("parsing_res_list", [])

            blocks: list[Block] = []
            for j, block_data in enumerate(blocks_data):
                block_id = block_data.get("id", block_data.get("block_id", j))
                block = self._build_block(
                    block_id=str(block_id),
                    block_type=str(block_data.get("type", block_data.get("block_label", "unknown"))),
                    text=str(block_data.get("text", block_data.get("block_content", ""))),
                    bbox=self._norm_bbox(block_data.get("bbox", block_data.get("block_bbox"))),
                    order=block_data.get("order", block_data.get("block_order")),
                    confidence=self._to_float(block_data.get("confidence", block_data.get("score"))),
                    page_index=page_index,
                    engine="paddle",
                    raw=block_data,
                )
                blocks.append(block)

            pages.append(
                Page(
                    page_index=page_index,
                    width=page_data.get("width"),
                    height=page_data.get("height"),
                    blocks=blocks,
                )
            )

        return DocumentIR(
            doc_id=doc_id,
            source_file=source_file,
            source_engine="paddle",
            generated_at=self._now_iso(),
            pages=pages,
        )

    def _from_mineru(self, raw_output: dict) -> DocumentIR:
        source_file = str(raw_output.get("source_file", ""))
        doc_id = str(raw_output.get("doc_id") or raw_output.get("doc_name") or "unknown_doc")

        content_list = raw_output.get("mineru_content_list")
        if content_list is None:
            content_list = raw_output.get("content_list")
        if content_list is None and isinstance(raw_output, dict):
            # allow caller to pass raw list under a generic key
            content_list = raw_output.get("items")
        if content_list is None:
            raise ValueError("MinerU raw_output must include mineru_content_list or content_list")

        page_items: dict[int, list[dict]] = defaultdict(list)
        for item in content_list:
            page_index = int(item.get("page_idx", 0))
            page_items[page_index].append(item)

        page_dimensions = raw_output.get("page_dimensions", {})
        selected_page_indices = raw_output.get("selected_page_indices")
        page_count = raw_output.get("page_count")
        if isinstance(selected_page_indices, list) and selected_page_indices:
            page_indices = sorted({int(i) for i in selected_page_indices})
        elif page_count is None:
            page_indices = sorted(page_items.keys())
        else:
            page_indices = list(range(int(page_count)))

        pages: list[Page] = []
        for page_index in page_indices:
            blocks: list[Block] = []
            items = page_items.get(page_index, [])
            for j, item in enumerate(items):
                text = item.get("content")
                if text is None:
                    text = item.get("text")
                if text is None:
                    text = item.get("table_body", "")
                block = self._build_block(
                    block_id=str(j),
                    block_type=str(item.get("type", "unknown")),
                    text=str(text),
                    bbox=self._norm_bbox(item.get("bbox")),
                    order=j,
                    confidence=self._to_float(item.get("score")),
                    page_index=page_index,
                    engine="mineru",
                    raw=item,
                )
                blocks.append(block)

            dims = page_dimensions.get(str(page_index)) or page_dimensions.get(page_index) or {}
            pages.append(
                Page(
                    page_index=page_index,
                    width=dims.get("width"),
                    height=dims.get("height"),
                    blocks=blocks,
                )
            )

        return DocumentIR(
            doc_id=doc_id,
            source_file=source_file,
            source_engine="mineru",
            generated_at=self._now_iso(),
            pages=pages,
        )
