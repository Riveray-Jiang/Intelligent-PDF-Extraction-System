from __future__ import annotations

from pathlib import Path
from typing import Any


class PaddleAdapter:
    """Normalize PaddleOCR doc_parser outputs to IR-ready payload."""

    @staticmethod
    def _as_page_list(raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if not isinstance(raw, dict):
            return []

        pages = raw.get("pages")
        if isinstance(pages, list):
            return [item for item in pages if isinstance(item, dict)]

        result = raw.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]

        parsing_res_list = raw.get("parsing_res_list")
        if isinstance(parsing_res_list, list):
            return [{"page_index": 0, "parsing_res_list": parsing_res_list}]

        return []

    @staticmethod
    def _to_int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _fill_missing_block_orders(cls, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill missing block_order values with a monotonic fallback.

        Paddle doc_parser may omit block_order for some block types (e.g. figure_title/table).
        Validation uses order presence to detect reading-order anomalies, so we synthesize a
        stable fallback order while preserving existing explicit orders.
        """
        normalized: list[dict[str, Any]] = []
        next_order = 0
        for raw_block in blocks:
            block = dict(raw_block)
            current = cls._to_int_or_none(block.get("block_order"))
            if current is None:
                current = cls._to_int_or_none(block.get("order"))
            if current is None:
                current = next_order
            block["block_order"] = current
            next_order = max(next_order, current + 1)
            normalized.append(block)
        return normalized

    def parse(
        self,
        raw: dict[str, Any] | list[dict[str, Any]],
        source_file: str | None = None,
        selected_pages: list[int] | None = None,
    ) -> dict[str, Any]:
        payload = raw if isinstance(raw, dict) else {}
        pages_data = self._as_page_list(raw)

        normalized_pages: list[dict[str, Any]] = []
        for i, page in enumerate(pages_data):
            page_index = self._to_int_or_none(page.get("page_index"))
            if page_index is None:
                page_index = self._to_int_or_none(page.get("page_id"))
            if page_index is None:
                page_index = i

            blocks = page.get("blocks")
            if not isinstance(blocks, list):
                blocks = page.get("parsing_res_list")
            if not isinstance(blocks, list):
                blocks = page.get("layout")
            if not isinstance(blocks, list):
                blocks = []

            normalized_pages.append(
                {
                    "page_index": page_index,
                    "width": self._to_int_or_none(page.get("width")),
                    "height": self._to_int_or_none(page.get("height")),
                    "blocks": self._fill_missing_block_orders(
                        [item for item in blocks if isinstance(item, dict)]
                    ),
                }
            )

        if selected_pages is not None:
            selected = {int(i) for i in selected_pages}
            normalized_pages = [p for p in normalized_pages if p["page_index"] in selected]
            if not normalized_pages and selected:
                normalized_pages = [{"page_index": i, "blocks": []} for i in sorted(selected)]

        normalized_pages.sort(key=lambda item: item["page_index"])

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

        return {
            "doc_id": str(doc_id),
            "source_file": str(source_file),
            "pages": normalized_pages,
        }
