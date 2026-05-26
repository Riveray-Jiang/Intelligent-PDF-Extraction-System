from __future__ import annotations

from typing import Any

from .types import Page


def extract_image_agent_preview(page: Page, cached_record: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "image_alt_text": None,
        "image_interpretation_markdown": None,
        "image_agent_language": None,
        "image_agent_kind": None,
        "image_agent_generated": False,
        "image_agent_empty": False,
    }
    image_block = next((block for block in page.blocks if block.type.lower() == "image_interpretation"), None)
    if image_block is not None:
        source = image_block.source if isinstance(image_block.source, dict) else {}
        structured = source.get("structured_output") if isinstance(source.get("structured_output"), dict) else {}
        summary = str(structured.get("summary") or "").strip()
        markdown = str(image_block.text or "").strip()
        result["image_alt_text"] = summary or None
        result["image_interpretation_markdown"] = markdown or None
        result["image_agent_language"] = str(source.get("language") or "").strip() or None
        result["image_agent_kind"] = str(source.get("image_kind") or "").strip() or None
        result["image_agent_generated"] = True
        result["image_agent_empty"] = False
        return result

    if not isinstance(cached_record, dict):
        return result

    result["image_alt_text"] = str(cached_record.get("summary") or "").strip() or None
    result["image_interpretation_markdown"] = str(cached_record.get("markdown") or "").strip() or None
    result["image_agent_language"] = str(cached_record.get("language") or "").strip() or None
    result["image_agent_kind"] = str(cached_record.get("image_kind") or "").strip() or None
    result["image_agent_generated"] = bool(cached_record.get("generated", True))
    result["image_agent_empty"] = not bool(cached_record.get("has_meaningful_image", False))
    return result
