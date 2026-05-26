from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .types import Block
from .types import Page


ARTIFACT_FILENAMES = (
    "document_ir.json",
    "document.md",
    "validation_report.json",
    "pipeline_state.json",
)


def artifact_paths_for_output_dir(output_dir: Path) -> dict[str, Path]:
    return {name: output_dir / name for name in ARTIFACT_FILENAMES}


def load_document_ir(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def build_page_model(page_payload: dict[str, Any]) -> Page:
    page_index = int(page_payload.get("page_index", 0))
    blocks = page_payload.get("blocks", [])
    return Page(
        page_index=page_index,
        width=page_payload.get("width"),
        height=page_payload.get("height"),
        blocks=[
            Block(
                id=str(block.get("id") or f"p{page_index}_b{idx}"),
                type=str(block.get("type", "unknown")),
                text=str(block.get("text") or ""),
                bbox=block.get("bbox"),
                order=block.get("order"),
                confidence=block.get("confidence"),
                source=block.get("source") or {},
                page_index=page_index,
                semantic_type=block.get("semantic_type"),
                heading_level=block.get("heading_level"),
            )
            for idx, block in enumerate(blocks)
        ],
    )


def page_model_to_payload(page: Page) -> dict[str, Any]:
    return {
        "page_index": page.page_index,
        "width": page.width,
        "height": page.height,
        "blocks": [
            {
                "id": block.id,
                "type": block.type,
                "text": block.text,
                "bbox": block.bbox,
                "order": block.order,
                "confidence": block.confidence,
                "source": block.source,
                "page_index": block.page_index,
                "semantic_type": block.semantic_type,
                "heading_level": block.heading_level,
            }
            for block in page.blocks
        ],
    }


def format_merged_page_markdown(page_number: int, page_markdown: str) -> str:
    content = page_markdown.strip()
    if not content:
        content = "_No extracted content on this page._"
    return f"## Page {page_number}\n\n{content}"
