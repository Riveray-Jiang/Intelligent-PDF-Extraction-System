from __future__ import annotations

import argparse
import csv
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader


CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


@dataclass
class TableTextCheck:
    page_index: int
    page_number: int
    engine: str
    table_index: int
    total_cells: int
    matched_cells: int
    match_rate: float
    text_preview: str


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize(text: str) -> str:
    text = html.unescape(text)
    text = TAG_RE.sub(" ", text)
    text = WS_RE.sub("", text)
    return text.strip()


def _extract_cells(html_text: str) -> list[str]:
    cells: list[str] = []
    for raw in CELL_RE.findall(html_text):
        cell = _normalize(raw)
        if cell:
            cells.append(cell)
    return cells


def _load_pdf_page_texts(pdf_path: Path) -> dict[int, str]:
    reader = PdfReader(str(pdf_path))
    out: dict[int, str] = {}
    for idx, page in enumerate(reader.pages):
        out[idx] = _normalize(page.extract_text() or "")
    return out


def _load_html_tables(page_dir: Path, prefix: str) -> list[tuple[int, str]]:
    files = sorted(page_dir.glob(f"{prefix}_table_*.html"))
    tables: list[tuple[int, str]] = []
    for path in files:
        match = re.search(r"_table_(\d+)_", path.name)
        if not match:
            continue
        table_index = int(match.group(1)) - 1
        tables.append((table_index, path.read_text(encoding="utf-8")))
    return tables


def _match_cells_to_page(cells: list[str], page_text: str) -> tuple[int, int, float]:
    if not cells:
        return 0, 0, 0.0
    matched = 0
    for cell in cells:
        if len(cell) < 2:
            continue
        if cell in page_text:
            matched += 1
    total = len([c for c in cells if len(c) >= 2])
    if total == 0:
        return 0, 0, 0.0
    return total, matched, matched / total


def build_check(audit_dir: Path, output_dir: Path) -> None:
    manifest = _read_json(audit_dir / "audit_manifest.json")
    pdf_path = Path(manifest["pdf_path"])
    fallback_pages: list[int] = list(manifest["fallback_pages_0based"])
    page_texts = _load_pdf_page_texts(pdf_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    checks: list[TableTextCheck] = []
    page_rows: list[dict[str, Any]] = []

    for page_index in fallback_pages:
        page_number = page_index + 1
        page_dir = audit_dir / f"page_{page_number:03d}"
        page_text = page_texts[page_index]
        page_summary = {
            "page_index_0based": page_index,
            "page_number_1based": page_number,
            "page_text_length": len(page_text),
            "mineru_verified": True,
            "paddle_verified": True,
            "final_verified": True,
        }

        for engine in ("mineru", "paddle", "final"):
            engine_tables = _load_html_tables(page_dir, engine)
            engine_rates: list[float] = []
            for table_index, html_text in engine_tables:
                cells = _extract_cells(html_text)
                total, matched, rate = _match_cells_to_page(cells, page_text)
                engine_rates.append(rate)
                checks.append(
                    TableTextCheck(
                        page_index=page_index,
                        page_number=page_number,
                        engine=engine,
                        table_index=table_index,
                        total_cells=total,
                        matched_cells=matched,
                        match_rate=rate,
                        text_preview=" ".join(cells[:5])[:200],
                    )
                )
            if engine == "mineru":
                # Missing tables remain as placeholders and will naturally score 0.
                page_summary["mineru_verified"] = all(rate >= 0.80 for rate in engine_rates if rate > 0) and any(
                    rate == 0.0 for rate in engine_rates
                )
            else:
                page_summary[f"{engine}_verified"] = all(rate >= 0.80 for rate in engine_rates)

        page_summary["cascade_fix_verified"] = bool(
            page_summary["paddle_verified"] and page_summary["final_verified"]
        )
        page_rows.append(page_summary)

    csv_path = output_dir / "ground_truth_table_checks.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "page_index",
                "page_number",
                "engine",
                "table_index",
                "total_cells",
                "matched_cells",
                "match_rate",
                "text_preview",
            ],
        )
        writer.writeheader()
        for row in checks:
            writer.writerow(
                {
                    "page_index": row.page_index,
                    "page_number": row.page_number,
                    "engine": row.engine,
                    "table_index": row.table_index,
                    "total_cells": row.total_cells,
                    "matched_cells": row.matched_cells,
                    "match_rate": f"{row.match_rate:.4f}",
                    "text_preview": row.text_preview,
                }
            )

    page_csv = output_dir / "ground_truth_page_verdicts.csv"
    with page_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "page_index_0based",
                "page_number_1based",
                "page_text_length",
                "mineru_verified",
                "paddle_verified",
                "final_verified",
                "cascade_fix_verified",
            ],
        )
        writer.writeheader()
        writer.writerows(page_rows)

    verified_pages = [r["page_number_1based"] for r in page_rows if r["cascade_fix_verified"]]
    md_lines = [
        "# Ground Truth Check",
        "",
        "Method: compare extracted table cell text against the original PDF text layer on the same page.",
        "This is stronger than proxy metrics, but still not a full human-labeled benchmark.",
        "",
        f"- fallback_pages: {len(fallback_pages)}",
        f"- cascade_fix_verified_pages: {len(verified_pages)}",
        "",
        "| page | paddle_verified | final_verified | cascade_fix_verified |",
        "|---|---:|---:|---:|",
    ]
    for row in page_rows:
        md_lines.append(
            f"| {row['page_number_1based']} | {str(row['paddle_verified']).lower()} | "
            f"{str(row['final_verified']).lower()} | {str(row['cascade_fix_verified']).lower()} |"
        )
    (output_dir / "ground_truth_summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    summary = {
        "audit_dir": str(audit_dir),
        "pdf_path": str(pdf_path),
        "fallback_pages_1based": [p + 1 for p in fallback_pages],
        "cascade_fix_verified_pages_1based": verified_pages,
        "note": "Verified by matching table cell text to the original PDF text layer on each page.",
    }
    (output_dir / "ground_truth_manifest.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a page-level ground-truth check for production cascade output.")
    parser.add_argument("--audit-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    build_check(Path(args.audit_dir).resolve(), Path(args.output_dir).resolve())


if __name__ == "__main__":
    main()
