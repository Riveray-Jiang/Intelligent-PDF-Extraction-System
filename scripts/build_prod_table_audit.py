from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium


@dataclass
class TableRecord:
    page_index: int
    table_index: int
    engine: str
    html: str
    caption: str
    missing: bool


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_to_text(x) for x in value)
    return str(value)


def _html_missing(html: str, text: str = "") -> bool:
    return (not html.strip()) and (not text.strip())


def _extract_numeric_suffix(path: Path) -> int:
    match = re.search(r"_(\d+)_res\.json$", path.name)
    if not match:
        return -1
    return int(match.group(1))


def _extract_mineru_tables(content_list_path: Path) -> dict[int, list[TableRecord]]:
    data = _read_json(content_list_path)
    if not isinstance(data, list):
        raise ValueError(f"Expected list in {content_list_path}")

    result: dict[int, list[TableRecord]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        if "table" not in _to_text(item.get("type")).lower():
            continue
        page_index = int(item.get("page_idx", 0))
        html = _to_text(item.get("table_body"))
        text = _to_text(item.get("text")) + _to_text(item.get("content"))
        record = TableRecord(
            page_index=page_index,
            table_index=len(result.get(page_index, [])),
            engine="mineru",
            html=html,
            caption=_to_text(item.get("table_caption")),
            missing=_html_missing(html, text),
        )
        result.setdefault(page_index, []).append(record)
    return result


def _extract_paddle_tables(result_path: Path, page_index: int) -> list[TableRecord]:
    data = _read_json(result_path)
    if not isinstance(data, dict):
        return []
    blocks = data.get("parsing_res_list", [])
    if not isinstance(blocks, list):
        return []

    tables: list[TableRecord] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if "table" not in _to_text(block.get("block_label")).lower():
            continue
        html = _to_text(block.get("table_body")) or _to_text(block.get("block_content"))
        text = _to_text(block.get("text")) + _to_text(block.get("block_content"))
        tables.append(
            TableRecord(
                page_index=page_index,
                table_index=len(tables),
                engine="paddle",
                html=html,
                caption="",
                missing=_html_missing(html, text),
            )
        )
    return tables


def _extract_final_tables(document_ir_path: Path) -> tuple[dict[int, list[TableRecord]], dict[int, set[str]], str]:
    data = _read_json(document_ir_path)
    pages = data.get("pages", [])
    source_engine = _to_text(data.get("source_engine"))
    tables_by_page: dict[int, list[TableRecord]] = {}
    engines_by_page: dict[int, set[str]] = {}

    for page in pages:
        if not isinstance(page, dict):
            continue
        page_index = int(page.get("page_index", 0))
        blocks = page.get("blocks", [])
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_engine = _to_text(block.get("source", {}).get("engine"))
            if block_engine:
                engines_by_page.setdefault(page_index, set()).add(block_engine)
            if "table" not in _to_text(block.get("type")).lower():
                continue
            raw = block.get("source", {}).get("raw", {})
            html = _to_text(raw.get("table_body")) or _to_text(block.get("text"))
            caption = _to_text(raw.get("table_caption"))
            text = _to_text(block.get("text"))
            tables_by_page.setdefault(page_index, []).append(
                TableRecord(
                    page_index=page_index,
                    table_index=len(tables_by_page.get(page_index, [])),
                    engine=block_engine or source_engine,
                    html=html,
                    caption=caption,
                    missing=_html_missing(html, text),
                )
            )
    return tables_by_page, engines_by_page, source_engine


def _render_pdf_page(pdf_path: Path, page_index: int, out_png: Path, dpi: int = 150) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[page_index]
        image = page.render(scale=dpi / 72.0).to_pil()
        image.save(out_png, format="PNG")
    finally:
        doc.close()


def _write_html(path: Path, html: str, placeholder: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = html.strip() if html.strip() else f"<!-- {placeholder} -->"
    path.write_text(content, encoding="utf-8")


def _page_verdict(mineru_tables: list[TableRecord], final_tables: list[TableRecord], page_engines: set[str]) -> str:
    mineru_missing = sum(1 for t in mineru_tables if t.missing)
    final_missing = sum(1 for t in final_tables if t.missing)
    if mineru_missing > 0 and final_missing == 0 and "paddle" in page_engines:
        return "fixed_by_cascade"
    if mineru_missing > 0 and final_missing > 0:
        return "not_fixed"
    if mineru_missing == 0 and "paddle" in page_engines:
        return "rerun_not_needed_or_non_table_issue"
    return "unknown"


def build_audit(prod_run_dir: Path, output_dir: Path) -> None:
    document_ir_path = prod_run_dir / "document_ir.json"
    pipeline_state_path = prod_run_dir / "pipeline_state.json"
    parse_root = prod_run_dir / "parse"
    mineru_content_list = parse_root / "mineru" / "attempt_01_pipeline_gpu_auto" / "_selected_pages_input" / "auto" / "_selected_pages_input_content_list.json"
    paddle_dir = parse_root / "paddle" / "attempt_01_pp_structurev3_fp16"

    if not document_ir_path.exists():
        raise FileNotFoundError(document_ir_path)
    if not mineru_content_list.exists():
        raise FileNotFoundError(mineru_content_list)
    if not paddle_dir.exists():
        raise FileNotFoundError(paddle_dir)

    document_ir = _read_json(document_ir_path)
    pdf_path = Path(_to_text(document_ir.get("source_file")))
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    final_tables_by_page, page_engines, primary_engine = _extract_final_tables(document_ir_path)
    fallback_pages = sorted(page for page, engines in page_engines.items() if "paddle" in engines)
    paddle_files = sorted(paddle_dir.glob("*_res.json"), key=_extract_numeric_suffix)
    if len(paddle_files) != len(fallback_pages):
        raise ValueError(
            f"Mismatch between fallback pages ({len(fallback_pages)}) and paddle result files ({len(paddle_files)})"
        )

    mineru_tables_by_page = _extract_mineru_tables(mineru_content_list)
    paddle_tables_by_page = {
        page_index: _extract_paddle_tables(path, page_index)
        for page_index, path in zip(fallback_pages, paddle_files)
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []

    for page_index in fallback_pages:
        page_number = page_index + 1
        page_dir = output_dir / f"page_{page_number:03d}"
        page_dir.mkdir(parents=True, exist_ok=True)
        _render_pdf_page(pdf_path, page_index, page_dir / "source_page.png")

        mineru_tables = mineru_tables_by_page.get(page_index, [])
        paddle_tables = paddle_tables_by_page.get(page_index, [])
        final_tables = final_tables_by_page.get(page_index, [])
        engines = page_engines.get(page_index, set())

        for record in mineru_tables:
            label = "missing" if record.missing else "ok"
            _write_html(
                page_dir / f"mineru_table_{record.table_index + 1:02d}_{label}.html",
                record.html,
                "mineru table_body missing",
            )
        for record in paddle_tables:
            label = "ok" if not record.missing else "missing"
            _write_html(
                page_dir / f"paddle_table_{record.table_index + 1:02d}_{label}.html",
                record.html,
                "paddle table html missing",
            )
        for record in final_tables:
            label = "ok" if not record.missing else "missing"
            engine = record.engine or "unknown"
            _write_html(
                page_dir / f"final_table_{record.table_index + 1:02d}_{engine}_{label}.html",
                record.html,
                "final table html missing",
            )

        page_summary = {
            "page_index_0based": page_index,
            "page_number_1based": page_number,
            "page_engines": sorted(engines),
            "mineru_tables_total": len(mineru_tables),
            "mineru_tables_missing": sum(1 for t in mineru_tables if t.missing),
            "paddle_tables_total": len(paddle_tables),
            "paddle_tables_missing": sum(1 for t in paddle_tables if t.missing),
            "final_tables_total": len(final_tables),
            "final_tables_missing": sum(1 for t in final_tables if t.missing),
            "verdict": _page_verdict(mineru_tables, final_tables, engines),
        }
        (page_dir / "summary.json").write_text(json.dumps(page_summary, indent=2, ensure_ascii=False), encoding="utf-8")
        rows.append(page_summary)

    csv_path = output_dir / "audit_summary.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "page_index_0based",
                "page_number_1based",
                "page_engines",
                "mineru_tables_total",
                "mineru_tables_missing",
                "paddle_tables_total",
                "paddle_tables_missing",
                "final_tables_total",
                "final_tables_missing",
                "verdict",
            ],
        )
        writer.writeheader()
        for row in rows:
            row = dict(row)
            row["page_engines"] = ",".join(row["page_engines"])
            writer.writerow(row)

    fixed_pages = [r for r in rows if r["verdict"] == "fixed_by_cascade"]
    unresolved_pages = [r for r in rows if r["verdict"] != "fixed_by_cascade"]
    md_lines = [
        "# Production Table Audit",
        "",
        f"- primary_engine: {primary_engine}",
        f"- fallback_pages: {len(fallback_pages)}",
        f"- fixed_by_cascade: {len(fixed_pages)}",
        f"- unresolved: {len(unresolved_pages)}",
        "",
        "| page | mineru_missing | paddle_missing | final_missing | page_engines | verdict |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        md_lines.append(
            f"| {row['page_number_1based']} | {row['mineru_tables_missing']} | {row['paddle_tables_missing']} | "
            f"{row['final_tables_missing']} | {', '.join(row['page_engines'])} | {row['verdict']} |"
        )
    (output_dir / "audit_summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    summary = {
        "prod_run_dir": str(prod_run_dir),
        "pdf_path": str(pdf_path),
        "primary_engine": primary_engine,
        "fallback_pages_0based": fallback_pages,
        "fallback_pages_1based": [p + 1 for p in fallback_pages],
        "fixed_by_cascade_pages_1based": [r["page_number_1based"] for r in fixed_pages],
        "unresolved_pages_1based": [r["page_number_1based"] for r in unresolved_pages],
        "pipeline_state": _read_json(pipeline_state_path) if pipeline_state_path.exists() else {},
    }
    (output_dir / "audit_manifest.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an automatic audit pack for a production cascade run.")
    parser.add_argument("--prod-run-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    build_audit(Path(args.prod_run_dir).resolve(), Path(args.output_dir).resolve())


if __name__ == "__main__":
    main()
