from __future__ import annotations

from pathlib import Path
from typing import Any


class MineruAdapter:
    """Normalize MinerU *_content_list.json outputs to IR-ready payload."""

    @staticmethod
    def _extract_items(raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if not isinstance(raw, dict):
            return []

        for key in ("mineru_content_list", "content_list", "items"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def parse(
        self,
        raw: dict[str, Any] | list[dict[str, Any]],
        source_file: str | None = None,
        selected_pages: list[int] | None = None,
    ) -> dict[str, Any]:
        payload = raw if isinstance(raw, dict) else {}
        items = self._extract_items(raw)

        selected: set[int] | None = None
        if selected_pages is not None:
            selected = {int(i) for i in selected_pages}

        normalized_items: list[dict[str, Any]] = []
        for item in items:
            page_idx = self._to_int(
                item.get("page_idx", item.get("page_index", item.get("page_no", 0)))
            )
            if selected is not None and page_idx not in selected:
                continue
            normalized = dict(item)
            normalized["page_idx"] = page_idx
            normalized_items.append(normalized)

        source_file = (
            source_file
            or payload.get("source_file")
            or payload.get("input_path")
            or payload.get("pdf_path")
            or ""
        )
        doc_id = payload.get("doc_id") or payload.get("doc_name")
        if not doc_id:
            doc_id = Path(str(source_file)).stem if source_file else "unknown_doc"

        page_dimensions = payload.get("page_dimensions")
        if not isinstance(page_dimensions, dict):
            page_dimensions = {}

        out: dict[str, Any] = {
            "doc_id": str(doc_id),
            "source_file": str(source_file),
            "mineru_content_list": normalized_items,
            "page_dimensions": page_dimensions,
        }
        if selected_pages is not None:
            out["selected_page_indices"] = sorted({int(i) for i in selected_pages})
        return out
