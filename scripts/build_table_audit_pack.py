from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium


@dataclass
class TableEntry:
    source: str
    page_index: int
    table_index: int
    table_body: str
    caption: str
    footnote: str
    missing_content: bool


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _is_missing_content(table_body: str, content: str) -> bool:
    return (not table_body.strip()) and (not content.strip())


def _extract_mineru_tables(content_list_path: Path, source_name: str) -> dict[int, list[TableEntry]]:
    data = _read_json(content_list_path)
    if not isinstance(data, list):
        raise ValueError(f"Expected list in {content_list_path}")

    out: dict[int, list[TableEntry]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        item_type = _to_str(item.get("type")).lower()
        if "table" not in item_type:
            continue
        page_index = int(item.get("page_idx", 0))
        table_body = _to_str(item.get("table_body"))
        content = _to_str(item.get("content")) + _to_str(item.get("text"))
        entry = TableEntry(
            source=source_name,
            page_index=page_index,
            table_index=len(out.get(page_index, [])),
            table_body=table_body,
            caption=_to_str(item.get("table_caption")),
            footnote=_to_str(item.get("table_footnote")),
            missing_content=_is_missing_content(table_body, content),
        )
        out.setdefault(page_index, []).append(entry)
    return out


def _extract_paddle_tables(paddle_dir: Path, source_name: str) -> dict[int, list[TableEntry]]:
    files = sorted(paddle_dir.glob("*_res.json"))
    if not files:
        raise ValueError(f"No *_res.json found under {paddle_dir}")

    out: dict[int, list[TableEntry]] = {}
    for fallback_page_index, path in enumerate(files):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        page_index = int(data.get("page_index", fallback_page_index))
        blocks = data.get("parsing_res_list", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            label = _to_str(block.get("block_label")).lower()
            if "table" not in label:
                continue
            table_body = _to_str(block.get("table_body"))
            content = _to_str(block.get("block_content")) + _to_str(block.get("text"))
            entry = TableEntry(
                source=source_name,
                page_index=page_index,
                table_index=len(out.get(page_index, [])),
                table_body=table_body if table_body else _to_str(block.get("block_content")),
                caption="",
                footnote="",
                missing_content=_is_missing_content(table_body, content),
            )
            out.setdefault(page_index, []).append(entry)
    return out


def _render_page(pdf_path: Path, page_index: int, out_png: Path, scale: float = 1.5) -> None:
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc.get_page(page_index)
        try:
            bitmap = page.render(scale=scale)
            pil_img = bitmap.to_pil()
            out_png.parent.mkdir(parents=True, exist_ok=True)
            pil_img.save(out_png)
        finally:
            page.close()
    finally:
        doc.close()


def _select_sample_pages(
    pipeline_tables: dict[int, list[TableEntry]],
    hybrid_tables: dict[int, list[TableEntry]],
    paddle_tables: dict[int, list[TableEntry]],
    sample_size: int,
    seed: int,
) -> list[int]:
    all_pages = sorted(set(pipeline_tables) | set(hybrid_tables) | set(paddle_tables))
    if not all_pages:
        return []

    # Always include pages where pipeline reports missing table content.
    priority = []
    for page in all_pages:
        miss = sum(1 for t in pipeline_tables.get(page, []) if t.missing_content)
        if miss > 0:
            priority.append((page, miss))
    priority_pages = [p for p, _ in sorted(priority, key=lambda x: (-x[1], x[0]))]

    selected: list[int] = []
    seen = set()
    for p in priority_pages:
        if p in seen:
            continue
        selected.append(p)
        seen.add(p)
        if len(selected) >= sample_size:
            return selected

    # Fill remaining slots by table density, then random tie-break.
    rng = random.Random(seed)
    leftovers = [p for p in all_pages if p not in seen]
    leftovers.sort(
        key=lambda p: (
            -len(pipeline_tables.get(p, [])) - len(hybrid_tables.get(p, [])) - len(paddle_tables.get(p, [])),
            rng.random(),
        )
    )
    for p in leftovers:
        selected.append(p)
        if len(selected) >= sample_size:
            break
    return selected


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _table_stats(entries: list[TableEntry]) -> tuple[int, int]:
    total = len(entries)
    missing = sum(1 for e in entries if e.missing_content)
    return total, missing


def build_audit_pack(
    pdf_path: Path,
    pipeline_content_list: Path,
    hybrid_content_list: Path,
    paddle_res_dir: Path,
    output_dir: Path,
    sample_size: int,
    seed: int,
) -> None:
    pipeline_tables = _extract_mineru_tables(pipeline_content_list, "mineru_pipeline")
    hybrid_tables = _extract_mineru_tables(hybrid_content_list, "mineru_hybrid")
    paddle_tables = _extract_paddle_tables(paddle_res_dir, "paddle_pp_structurev3")

    pages = _select_sample_pages(
        pipeline_tables=pipeline_tables,
        hybrid_tables=hybrid_tables,
        paddle_tables=paddle_tables,
        sample_size=sample_size,
        seed=seed,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "pdf_path": str(pdf_path),
        "sample_size": sample_size,
        "seed": seed,
        "selected_pages": pages,
    }
    _write_text(output_dir / "audit_manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    csv_path = output_dir / "audit_sheet.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "sample_id",
                "page_index",
                "page_number",
                "pipeline_table_total",
                "pipeline_missing_total",
                "hybrid_table_total",
                "hybrid_missing_total",
                "paddle_table_total",
                "paddle_missing_total",
                "gt_best_engine",
                "gt_notes",
            ]
        )

        for i, page_index in enumerate(pages, start=1):
            page_dir = output_dir / f"sample_{i:02d}_page_{page_index + 1:03d}"
            page_dir.mkdir(parents=True, exist_ok=True)

            _render_page(pdf_path, page_index, page_dir / "page.png")

            p_entries = pipeline_tables.get(page_index, [])
            h_entries = hybrid_tables.get(page_index, [])
            pd_entries = paddle_tables.get(page_index, [])
            p_total, p_missing = _table_stats(p_entries)
            h_total, h_missing = _table_stats(h_entries)
            pd_total, pd_missing = _table_stats(pd_entries)

            writer.writerow(
                [
                    f"S{i:02d}",
                    page_index,
                    page_index + 1,
                    p_total,
                    p_missing,
                    h_total,
                    h_missing,
                    pd_total,
                    pd_missing,
                    "",
                    "",
                ]
            )

            summary = {
                "sample_id": f"S{i:02d}",
                "page_index": page_index,
                "page_number": page_index + 1,
                "pipeline": [{"missing": e.missing_content, "caption": e.caption, "footnote": e.footnote} for e in p_entries],
                "hybrid": [{"missing": e.missing_content, "caption": e.caption, "footnote": e.footnote} for e in h_entries],
                "paddle": [{"missing": e.missing_content} for e in pd_entries],
            }
            _write_text(page_dir / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False))

            for entry in p_entries:
                body = entry.table_body.strip()
                label = "missing" if entry.missing_content else "ok"
                _write_text(
                    page_dir / f"pipeline_table_{entry.table_index + 1:02d}_{label}.html",
                    body if body else "<!-- missing table_body -->",
                )
            for entry in h_entries:
                body = entry.table_body.strip()
                label = "missing" if entry.missing_content else "ok"
                _write_text(
                    page_dir / f"hybrid_table_{entry.table_index + 1:02d}_{label}.html",
                    body if body else "<!-- missing table_body -->",
                )
            for entry in pd_entries:
                body = entry.table_body.strip()
                label = "missing" if entry.missing_content else "ok"
                _write_text(
                    page_dir / f"paddle_table_{entry.table_index + 1:02d}_{label}.html",
                    body if body else "<!-- missing table_body -->",
                )

    readme = """# Table Audit Pack

This folder contains sampled pages for manual ground-truth review.

Files:
- `audit_sheet.csv`: one row per sampled page; fill `gt_best_engine` and `gt_notes`.
- `sample_*/page.png`: rendered page image for visual reference.
- `sample_*/summary.json`: quick table stats for each engine.
- `sample_*/pipeline_table_*.html`: MinerU pipeline table extraction.
- `sample_*/hybrid_table_*.html`: MinerU hybrid table extraction.
- `sample_*/paddle_table_*.html`: Paddle pp_structurev3 table extraction.
"""
    _write_text(output_dir / "README.md", readme)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a manual table-audit pack from benchmark outputs.")
    parser.add_argument("--pdf", required=True, help="Source PDF path.")
    parser.add_argument("--pipeline-content-list", required=True, help="MinerU pipeline *_content_list.json path.")
    parser.add_argument("--hybrid-content-list", required=True, help="MinerU hybrid *_content_list.json path.")
    parser.add_argument("--paddle-res-dir", required=True, help="Directory containing Paddle *_res.json files.")
    parser.add_argument("--output-dir", required=True, help="Output directory.")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    build_audit_pack(
        pdf_path=Path(args.pdf).resolve(),
        pipeline_content_list=Path(args.pipeline_content_list).resolve(),
        hybrid_content_list=Path(args.hybrid_content_list).resolve(),
        paddle_res_dir=Path(args.paddle_res_dir).resolve(),
        output_dir=Path(args.output_dir).resolve(),
        sample_size=max(1, int(args.sample_size)),
        seed=int(args.seed),
    )


if __name__ == "__main__":
    main()
