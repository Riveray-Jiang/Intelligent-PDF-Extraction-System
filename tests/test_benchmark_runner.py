from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from backend import benchmark_runner
from backend.benchmark_runner import _build_thresholds
from backend.benchmark_runner import _calibration_candidates
from backend.benchmark_runner import _default_if_none
from backend.benchmark_runner import _passes_quality
from backend.benchmark_runner import _select_engine
from backend.benchmark_runner import _update_quality_floor_config
from backend.types import DocumentIR
from backend.types import Page
from backend.types import ValidationReport


def test_build_thresholds_formula() -> None:
    thresholds = _build_thresholds(
        {
            "empty_page_rate": 0.01,
            "order_anomaly_rate": 0.02,
            "table_anomaly_rate": 0.03,
            "coverage_rate": 0.99,
        }
    )
    assert thresholds["empty_page_rate_max"] == 0.03
    assert thresholds["order_anomaly_rate_max"] == 0.05
    assert thresholds["table_anomaly_rate_max"] == 0.08
    assert thresholds["coverage_rate_min"] == 0.97


def test_passes_quality() -> None:
    thresholds = {
        "empty_page_rate_max": 0.06,
        "order_anomaly_rate_max": 0.10,
        "table_anomaly_rate_max": 0.20,
        "coverage_rate_min": 0.94,
    }
    assert _passes_quality(
        {
            "empty_page_rate": 0.02,
            "order_anomaly_rate": 0.03,
            "table_anomaly_rate": 0.05,
            "coverage_rate": 0.96,
        },
        thresholds,
    )
    assert not _passes_quality(
        {
            "empty_page_rate": 0.07,
            "order_anomaly_rate": 0.03,
            "table_anomaly_rate": 0.05,
            "coverage_rate": 0.96,
        },
        thresholds,
    )


def test_select_engine_quality_tiebreak() -> None:
    chosen, reason = _select_engine(
        [
            {
                "engine": "paddle",
                "pass_quality_floor": True,
                "weighted_pages_per_sec": 2.0,
                "quality_score": 0.70,
            },
            {
                "engine": "mineru",
                "pass_quality_floor": True,
                "weighted_pages_per_sec": 1.9,
                "quality_score": 0.90,
            },
        ]
    )
    assert chosen == "mineru"
    assert "Speed gap < 10%" in reason


def test_update_quality_floor_config(tmp_path: Path) -> None:
    cfg = tmp_path / "quality_floor.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "policy": "calibrate_then_freeze",
                "metrics": {
                    "empty_page_rate": {},
                    "order_anomaly_rate": {},
                    "table_anomaly_rate": {},
                    "coverage_rate": {},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _update_quality_floor_config(
        cfg,
        {
            "empty_page_rate_max": 0.05123456,
            "order_anomaly_rate_max": 0.09123456,
            "table_anomaly_rate_max": 0.10123456,
            "coverage_rate_min": 0.95123456,
        },
    )
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    metrics = data["metrics"]
    assert metrics["empty_page_rate"]["runtime_max"] == 0.051235
    assert metrics["order_anomaly_rate"]["runtime_max"] == 0.091235
    assert metrics["table_anomaly_rate"]["runtime_max"] == 0.101235
    assert metrics["coverage_rate"]["runtime_min"] == 0.951235


def test_calibration_candidates_filter() -> None:
    rows = [
        {"parse_error_count": 0, "manual_review_required": False},
        {"parse_error_count": 1, "manual_review_required": False},
        {"parse_error_count": 0, "manual_review_required": True},
    ]
    candidates = _calibration_candidates(rows)
    assert candidates == [rows[0]]


def test_default_if_none_preserves_zero() -> None:
    assert _default_if_none(None, 1.0) == 1.0
    assert _default_if_none(0.0, 1.0) == 0.0


def test_benchmark_main_freeze_guard_skips_config_update(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "demo.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "documents": [{"id": "demo", "path": str(pdf_path)}],
                "runs": {"cold_runs": 1, "warm_runs": 1},
                "selection_mode": "all",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    quality_cfg = tmp_path / "quality_floor.yaml"
    quality_cfg.write_text(
        yaml.safe_dump(
            {
                "metrics": {
                    "empty_page_rate": {"runtime_max": 0.06},
                    "order_anomaly_rate": {"runtime_max": 0.10},
                    "table_anomaly_rate": {"runtime_max": 0.20},
                    "coverage_rate": {"runtime_min": 0.94},
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    before = quality_cfg.read_text(encoding="utf-8")

    class _FakeApp:
        def invoke(self, state):  # noqa: ANN001
            return {
                "selected": {"selected_count": 2},
                "document_ir": DocumentIR(
                    doc_id="demo",
                    source_file=str(pdf_path),
                    source_engine=state["engine"],
                    generated_at="2026-02-24T00:00:00Z",
                    pages=[Page(page_index=0, blocks=[]), Page(page_index=1, blocks=[])],
                ),
                "validation": ValidationReport(
                    empty_page_rate=1.0,
                    order_anomaly_rate=1.0,
                    table_anomaly_rate=1.0,
                    coverage_rate=0.0,
                    non_blank_pages=2,
                    pages_with_content=0,
                    empty_pages=2,
                    anomalous_order_pages=0,
                    total_tables=0,
                    anomalous_tables=0,
                    failed_pages=[0, 1],
                    pass_quality_floor=False,
                ),
                "manual_review_required": True,
                "parse_error": None,
            }

    class _FakeSampler:
        def __init__(self, interval_sec=1.0):  # noqa: ANN001
            self.interval_sec = interval_sec

        def start(self) -> None:
            return None

        def stop(self):  # noqa: ANN201
            return benchmark_runner.GpuMetrics(
                avg_utilization=None,
                peak_memory_mb=None,
                samples=0,
                available=False,
            )

    monkeypatch.setattr(benchmark_runner, "build_graph", lambda: _FakeApp())
    monkeypatch.setattr(benchmark_runner, "GpuSampler", _FakeSampler)

    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_runner",
            "--manifest",
            str(manifest_path),
            "--engines",
            "paddle",
            "--cold-runs",
            "1",
            "--warm-runs",
            "1",
            "--output-dir",
            str(out_dir),
            "--freeze-thresholds",
            "--quality-config",
            str(quality_cfg),
        ],
    )
    benchmark_runner.main()

    after = quality_cfg.read_text(encoding="utf-8")
    assert after == before
    overview = json.loads((out_dir / "benchmark_overview.json").read_text(encoding="utf-8"))
    assert overview["freeze_applied"] is False
    assert "Freeze skipped" in overview["freeze_warning"]
    assert (out_dir / "benchmark_summary.md").exists()


def test_benchmark_main_passes_cascade_flags(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "demo.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "documents": [{"id": "demo", "path": str(pdf_path)}],
                "runs": {"cold_runs": 1, "warm_runs": 1},
                "selection_mode": "all",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    class _FakeApp:
        def __init__(self) -> None:
            self.states: list[dict] = []

        def invoke(self, state):  # noqa: ANN001
            self.states.append(state)
            return {
                "selected": {"selected_count": 2},
                "document_ir": DocumentIR(
                    doc_id="demo",
                    source_file=str(pdf_path),
                    source_engine=state["engine"],
                    generated_at="2026-02-24T00:00:00Z",
                    pages=[Page(page_index=0, blocks=[]), Page(page_index=1, blocks=[])],
                ),
                "validation": ValidationReport(
                    empty_page_rate=0.0,
                    order_anomaly_rate=0.0,
                    table_anomaly_rate=0.0,
                    coverage_rate=1.0,
                    non_blank_pages=2,
                    pages_with_content=2,
                    empty_pages=0,
                    anomalous_order_pages=0,
                    total_tables=0,
                    anomalous_tables=0,
                    failed_pages=[],
                    pass_quality_floor=True,
                ),
                "manual_review_required": False,
                "parse_error": None,
                "cascade_active": True,
                "cascade_attempt": 1,
            }

    class _FakeSampler:
        def __init__(self, interval_sec=1.0):  # noqa: ANN001
            self.interval_sec = interval_sec

        def start(self) -> None:
            return None

        def stop(self):  # noqa: ANN201
            return benchmark_runner.GpuMetrics(
                avg_utilization=None,
                peak_memory_mb=None,
                samples=0,
                available=False,
            )

    fake_app = _FakeApp()
    monkeypatch.setattr(benchmark_runner, "build_graph", lambda: fake_app)
    monkeypatch.setattr(benchmark_runner, "GpuSampler", _FakeSampler)

    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_runner",
            "--manifest",
            str(manifest_path),
            "--engines",
            "paddle",
            "--cold-runs",
            "1",
            "--warm-runs",
            "1",
            "--output-dir",
            str(out_dir),
            "--cascade-enabled",
            "--cascade-engine",
            "mineru",
            "--cascade-engine-config",
            "configs/engines_cascade.yaml",
            "--max-cascade-attempts",
            "2",
        ],
    )
    benchmark_runner.main()

    assert fake_app.states, "benchmark runner did not invoke graph"
    first_state = fake_app.states[0]
    assert first_state["cascade_enabled"] is True
    assert first_state["cascade_engine"] == "mineru"
    assert first_state["cascade_engine_config"] == "configs/engines_cascade.yaml"
    assert first_state["max_cascade_attempts"] == 2

    run_rows = json.loads((out_dir / "benchmark_runs.json").read_text(encoding="utf-8"))
    assert run_rows
    assert run_rows[0]["cascade_triggered"] is True
    assert run_rows[0]["cascade_attempt"] == 1
