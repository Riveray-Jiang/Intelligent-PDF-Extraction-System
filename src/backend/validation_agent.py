from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

import yaml

from .types import DocumentIR
from .types import ValidationReport


class ValidationAgent:
    """Run quality checks and generate validation report."""

    DEFAULT_EMPTY_PAGE_RATE_MAX = 0.06
    DEFAULT_ORDER_ANOMALY_RATE_MAX = 0.10
    DEFAULT_TABLE_ANOMALY_RATE_MAX = 0.20
    DEFAULT_COVERAGE_RATE_MIN = 0.94
    DEFAULT_ORDER_INVERSION_THRESHOLD = 0.35
    DEFAULT_ORDER_MISSING_RATIO_THRESHOLD = 0.20
    DEFAULT_TABLE_COLS_MAX = 40
    DEFAULT_TABLE_ROWS_MAX = 2000
    DEFAULT_TABLE_EMPTY_ROW_RATIO_MAX = 0.50
    DEFAULT_ORDER_OPTIONAL_TYPES = {
        "header",
        "footer",
        "number",
        "page_number",
        "watermark",
    }

    def __init__(self, config_path: str | Path | None = None) -> None:
        root = Path(__file__).resolve().parents[2]
        config_path = Path(config_path) if config_path else (root / "configs" / "quality_floor.yaml")

        self.EMPTY_PAGE_RATE_MAX = self.DEFAULT_EMPTY_PAGE_RATE_MAX
        self.ORDER_ANOMALY_RATE_MAX = self.DEFAULT_ORDER_ANOMALY_RATE_MAX
        self.TABLE_ANOMALY_RATE_MAX = self.DEFAULT_TABLE_ANOMALY_RATE_MAX
        self.COVERAGE_RATE_MIN = self.DEFAULT_COVERAGE_RATE_MIN
        self.ORDER_INVERSION_THRESHOLD = self.DEFAULT_ORDER_INVERSION_THRESHOLD
        self.ORDER_MISSING_RATIO_THRESHOLD = self.DEFAULT_ORDER_MISSING_RATIO_THRESHOLD
        self.TABLE_COLS_MAX = self.DEFAULT_TABLE_COLS_MAX
        self.TABLE_ROWS_MAX = self.DEFAULT_TABLE_ROWS_MAX
        self.TABLE_EMPTY_ROW_RATIO_MAX = self.DEFAULT_TABLE_EMPTY_ROW_RATIO_MAX
        self.ORDER_OPTIONAL_TYPES = set(self.DEFAULT_ORDER_OPTIONAL_TYPES)

        self._load_config(config_path)

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _load_config(self, config_path: Path) -> None:
        if not config_path.exists():
            return
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return

        metrics = data.get("metrics")
        if isinstance(metrics, dict):
            empty = metrics.get("empty_page_rate", {})
            if isinstance(empty, dict):
                value = self._as_float(empty.get("runtime_max"))
                if value is not None:
                    self.EMPTY_PAGE_RATE_MAX = value

            order = metrics.get("order_anomaly_rate", {})
            if isinstance(order, dict):
                value = self._as_float(order.get("runtime_max"))
                if value is not None:
                    self.ORDER_ANOMALY_RATE_MAX = value

            table = metrics.get("table_anomaly_rate", {})
            if isinstance(table, dict):
                value = self._as_float(table.get("runtime_max"))
                if value is not None:
                    self.TABLE_ANOMALY_RATE_MAX = value

            coverage = metrics.get("coverage_rate", {})
            if isinstance(coverage, dict):
                value = self._as_float(coverage.get("runtime_min"))
                if value is not None:
                    self.COVERAGE_RATE_MIN = value

        algorithms = data.get("algorithms")
        if isinstance(algorithms, dict):
            value = self._as_float(algorithms.get("order_inversion_anomaly_threshold"))
            if value is not None:
                self.ORDER_INVERSION_THRESHOLD = value

            value = self._as_float(algorithms.get("order_missing_ratio_threshold"))
            if value is not None:
                self.ORDER_MISSING_RATIO_THRESHOLD = value

            value = self._as_int(algorithms.get("table_cols_max"))
            if value is not None:
                self.TABLE_COLS_MAX = value

            value = self._as_int(algorithms.get("table_rows_max"))
            if value is not None:
                self.TABLE_ROWS_MAX = value

            value = self._as_float(algorithms.get("table_empty_row_ratio_max"))
            if value is not None:
                self.TABLE_EMPTY_ROW_RATIO_MAX = value

    @staticmethod
    def _has_page_content(page) -> bool:
        for block in page.blocks:
            text = (block.text or "").strip()
            if text:
                return True
            if "table" in block.type.lower():
                return True
        return False

    @staticmethod
    def _normalized_inversions(values: list[int]) -> float:
        n = len(values)
        if n < 2:
            return 0.0
        inv = 0
        for i in range(n):
            for j in range(i + 1, n):
                if values[i] > values[j]:
                    inv += 1
        max_pairs = (n * (n - 1)) / 2
        return float(inv) / float(max_pairs) if max_pairs else 0.0

    def _is_order_anomalous(self, page) -> bool:
        if not page.blocks:
            return False
        relevant_blocks = []
        for block in page.blocks:
            block_type = str(block.type or "").lower().strip()
            if block_type in self.ORDER_OPTIONAL_TYPES:
                continue
            text = (block.text or "").strip()
            if not text and "table" not in block_type:
                continue
            relevant_blocks.append(block)

        if len(relevant_blocks) < 2:
            return False

        orders = [int(b.order) for b in relevant_blocks if isinstance(b.order, int)]
        missing_ratio = float(len(relevant_blocks) - len(orders)) / float(len(relevant_blocks))
        inversion_ratio = self._normalized_inversions(orders)
        return (
            inversion_ratio > self.ORDER_INVERSION_THRESHOLD
            or missing_ratio > self.ORDER_MISSING_RATIO_THRESHOLD
        )

    @staticmethod
    def _strip_html_tags(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()

    def _table_stats(self, page) -> tuple[int, int]:
        total_tables = 0
        anomalous_tables = 0
        for block in page.blocks:
            if "table" not in block.type.lower():
                continue
            total_tables += 1

            raw = block.source.get("raw", {}) if isinstance(block.source, dict) else {}
            html = ""
            if isinstance(raw, dict):
                html = str(raw.get("table_body", "") or "")
            if not html:
                text = block.text or ""
                if "<table" in text.lower():
                    html = text

            if html:
                row_segments = re.findall(
                    r"<tr\b[^>]*>(.*?)</tr>",
                    html,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                rows = len(row_segments)
                cols = 0
                empty_rows = 0
                for row in row_segments:
                    cell_count = len(re.findall(r"<t[dh]\b", row, flags=re.IGNORECASE))
                    cols = max(cols, cell_count)
                    if self._strip_html_tags(row) == "":
                        empty_rows += 1
                empty_row_ratio = (float(empty_rows) / float(rows)) if rows else 1.0
                is_anomalous = (
                    rows == 0
                    or cols == 0
                    or cols > self.TABLE_COLS_MAX
                    or rows > self.TABLE_ROWS_MAX
                    or empty_row_ratio > self.TABLE_EMPTY_ROW_RATIO_MAX
                )
            else:
                is_anomalous = (block.text or "").strip() == ""

            if is_anomalous:
                anomalous_tables += 1

        return total_tables, anomalous_tables

    def run(
        self,
        document_ir: DocumentIR,
        source_non_blank_pages: Collection[int] | None = None,
    ) -> ValidationReport:
        source_non_blank_set = (
            {int(page_index) for page_index in source_non_blank_pages}
            if source_non_blank_pages is not None
            else None
        )
        pages_to_validate = [
            page
            for page in document_ir.pages
            if source_non_blank_set is None or page.page_index in source_non_blank_set
        ]
        non_blank_pages = len(pages_to_validate)
        pages_with_content = 0
        empty_pages = 0
        anomalous_order_pages = 0
        total_tables = 0
        anomalous_tables = 0
        failed_pages: list[int] = []

        for page in pages_to_validate:
            has_content = self._has_page_content(page)
            if has_content:
                pages_with_content += 1
            else:
                empty_pages += 1
                failed_pages.append(page.page_index)

            if has_content and self._is_order_anomalous(page):
                anomalous_order_pages += 1
                failed_pages.append(page.page_index)

            page_tables, page_anomalous_tables = self._table_stats(page)
            total_tables += page_tables
            anomalous_tables += page_anomalous_tables
            if page_anomalous_tables > 0:
                failed_pages.append(page.page_index)

        empty_page_rate = (float(empty_pages) / float(non_blank_pages)) if non_blank_pages else 0.0
        order_anomaly_rate = (
            float(anomalous_order_pages) / float(pages_with_content) if pages_with_content else 0.0
        )
        table_anomaly_rate = (
            float(anomalous_tables) / float(total_tables) if total_tables else 0.0
        )
        coverage_rate = (
            float(pages_with_content) / float(non_blank_pages) if non_blank_pages else 0.0
        )

        pass_quality_floor = (
            empty_page_rate <= self.EMPTY_PAGE_RATE_MAX
            and order_anomaly_rate <= self.ORDER_ANOMALY_RATE_MAX
            and table_anomaly_rate <= self.TABLE_ANOMALY_RATE_MAX
            and coverage_rate >= self.COVERAGE_RATE_MIN
        )

        return ValidationReport(
            empty_page_rate=empty_page_rate,
            order_anomaly_rate=order_anomaly_rate,
            table_anomaly_rate=table_anomaly_rate,
            coverage_rate=coverage_rate,
            non_blank_pages=non_blank_pages,
            pages_with_content=pages_with_content,
            empty_pages=empty_pages,
            anomalous_order_pages=anomalous_order_pages,
            total_tables=total_tables,
            anomalous_tables=anomalous_tables,
            failed_pages=sorted(set(failed_pages)),
            pass_quality_floor=pass_quality_floor,
        )
