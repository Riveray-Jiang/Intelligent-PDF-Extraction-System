from __future__ import annotations

import json
from pathlib import Path

import yaml

from backend import mineru_fast_upgrade_benchmark as bench
from backend.types import Block
from backend.types import DocumentIR
from backend.types import Page
from backend.types import ValidationReport


def _doc(blocks: list[Block]) -> DocumentIR:
    return DocumentIR(
        doc_id="doc",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-24T00:00:00Z",
        pages=[Page(page_index=0, blocks=blocks)],
    )


def _validation() -> ValidationReport:
    return ValidationReport(
        empty_page_rate=0.0,
        order_anomaly_rate=0.0,
        table_anomaly_rate=0.0,
        coverage_rate=1.0,
        non_blank_pages=1,
        pages_with_content=1,
        empty_pages=0,
        anomalous_order_pages=0,
        total_tables=0,
        anomalous_tables=0,
        failed_pages=[],
        pass_quality_floor=True,
    )


def test_quality_metrics_counts_duplicates_headings_and_stamp_terms() -> None:
    doc = _doc(
        [
            Block(id="b0", type="text", text="1. Introduction", order=0, page_index=0),
            Block(id="b1", type="text", text="江西航宏新能源科技有限公司", order=1, page_index=0),
            Block(id="b2", type="text", text="江西航宏新能源科技有限公司", order=2, page_index=0),
        ]
    )
    case = bench.DocumentCase(
        id="stamp",
        path=Path("demo.pdf"),
        selection_mode="all",
        selection=None,
        tags=("stamp",),
        expected_terms=("Introduction", "江西航宏"),
        stamp_terms=("江西航宏新能源科技有限公司",),
    )

    metrics = bench._quality_metrics(doc, _validation(), case)

    assert metrics["heading_count"] == 1
    assert metrics["duplicate_text_ratio"] > 0
    assert metrics["expected_terms_recall"] == 1.0
    assert metrics["stamp_recovery"] == 1.0


def test_doc_aggregation_computes_unique_text_coverage_against_best_candidate() -> None:
    rows = [
        {
            "candidate_id": "A",
            "candidate_label": "baseline",
            "doc_id": "doc",
            "run_type": "warm",
            "wall_time_sec": 2.0,
            "sec_per_page": 2.0,
            "pages": 1,
            "unique_text_chars": 50,
            "duplicate_text_ratio": 0.0,
            "garbled_text_rate": 0.0,
            "empty_page_rate": 0.0,
            "table_anomaly_rate": 0.0,
            "reading_order_issue_rate": 0.0,
            "heading_presence_rate": 1.0,
            "expected_terms_recall": 1.0,
            "stamp_recovery": None,
            "parse_error": None,
            "error_kind": None,
            "tags": ["ordinary"],
        },
        {
            "candidate_id": "B",
            "candidate_label": "probe",
            "doc_id": "doc",
            "run_type": "warm",
            "wall_time_sec": 2.2,
            "sec_per_page": 2.2,
            "pages": 1,
            "unique_text_chars": 100,
            "duplicate_text_ratio": 0.0,
            "garbled_text_rate": 0.0,
            "empty_page_rate": 0.0,
            "table_anomaly_rate": 0.0,
            "reading_order_issue_rate": 0.0,
            "heading_presence_rate": 1.0,
            "expected_terms_recall": 1.0,
            "stamp_recovery": None,
            "parse_error": None,
            "error_kind": None,
            "tags": ["ordinary"],
        },
    ]

    doc_rows = bench._aggregate_doc_rows(rows)
    by_candidate = {row["candidate_id"]: row for row in doc_rows}

    assert by_candidate["A"]["unique_text_coverage"] == 0.5
    assert by_candidate["B"]["unique_text_coverage"] == 1.0


def test_decision_requires_latency_and_stability() -> None:
    candidate_rows = [
        {
            "candidate_id": "A",
            "candidate_label": "baseline",
            "warm_sec_per_page_median": 2.0,
            "warm_sec_per_page_p95": 2.5,
            "ordinary_warm_sec_per_page_median": 2.0,
            "native_text_warm_sec_per_page_median": 2.0,
            "unique_text_coverage": 0.8,
            "duplicate_text_ratio": 0.1,
            "table_anomaly_rate": 0.1,
            "reading_order_issue_rate": 0.05,
            "heading_presence_rate": 0.2,
            "stamp_recovery": 0.0,
            "parse_error_count": 0,
            "timeout_count": 0,
            "process_crash_count": 0,
            "cache_or_model_load_error_count": 0,
        },
        {
            "candidate_id": "B",
            "candidate_label": "pipeline3",
            "warm_sec_per_page_median": 2.3,
            "warm_sec_per_page_p95": 3.0,
            "ordinary_warm_sec_per_page_median": 2.3,
            "native_text_warm_sec_per_page_median": 2.3,
            "unique_text_coverage": 0.9,
            "duplicate_text_ratio": 0.1,
            "table_anomaly_rate": 0.05,
            "reading_order_issue_rate": 0.05,
            "heading_presence_rate": 0.2,
            "stamp_recovery": 0.0,
            "parse_error_count": 0,
            "timeout_count": 0,
            "process_crash_count": 0,
            "cache_or_model_load_error_count": 0,
        },
    ]

    decisions = bench._evaluate_decisions(candidate_rows)

    assert decisions["mineru3_pipeline_fast"]["pass"] is True


def test_dry_run_validates_manifest_without_invoking_pipeline(tmp_path: Path) -> None:
    cfg = tmp_path / "engine.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "mineru": {
                        "service": {"container_name": "demo"},
                        "retry_profiles": [{"backend": "pipeline"}],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fake\n")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "candidates": [
                    {
                        "id": "A",
                        "label": "baseline",
                        "version": "2.7.6",
                        "backend": "pipeline",
                        "role": "baseline",
                        "engine_config": str(cfg),
                    }
                ],
                "documents": [{"id": "demo", "path": str(pdf)}],
                "runs": {"cold_runs": 1, "warm_runs": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    overview = bench.run_benchmark(
        manifest_path=manifest,
        output_dir=tmp_path / "out",
        dry_run=True,
    )

    assert overview["candidate_count"] == 1
    assert overview["document_count"] == 1
    assert overview["missing_docs"] == []
    assert overview["missing_configs"] == []
    assert not (tmp_path / "out").exists()


def test_main_dry_run_prints_json(tmp_path: Path, monkeypatch, capsys) -> None:
    manifest = tmp_path / "manifest.yaml"
    missing_pdf = tmp_path / "missing.pdf"
    missing_cfg = tmp_path / "missing.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "candidates": [
                    {
                        "id": "A",
                        "label": "baseline",
                        "version": "2.7.6",
                        "backend": "pipeline",
                        "role": "baseline",
                        "engine_config": str(missing_cfg),
                    }
                ],
                "documents": [{"id": "demo", "path": str(missing_pdf)}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "mineru_fast_upgrade_benchmark",
            "--manifest",
            str(manifest),
            "--output-dir",
            str(tmp_path / "out"),
            "--dry-run",
        ],
    )

    bench.main()

    printed = json.loads(capsys.readouterr().out)
    assert printed["missing_docs"] == [str(missing_pdf)]
    assert printed["missing_configs"] == [str(missing_cfg)]
