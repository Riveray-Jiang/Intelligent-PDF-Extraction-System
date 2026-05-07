from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .pipeline_graph import build_graph
from .types import DocumentIR
from .types import ValidationReport


@dataclass
class GpuMetrics:
    avg_utilization: float | None
    peak_memory_mb: float | None
    samples: int
    available: bool


class GpuSampler:
    def __init__(self, interval_sec: float = 1.0) -> None:
        self.interval_sec = max(0.2, float(interval_sec))
        self._samples: list[float] = []
        self._peak_memory: float = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.available = shutil.which("nvidia-smi") is not None

    @staticmethod
    def _read_sample() -> tuple[float | None, float | None]:
        cmd = [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=5)
        if result.returncode != 0:
            return None, None
        util_values: list[float] = []
        mem_values: list[float] = []
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 2:
                continue
            try:
                util_values.append(float(parts[0]))
                mem_values.append(float(parts[1]))
            except ValueError:
                continue
        if not util_values:
            return None, None
        return float(sum(util_values) / len(util_values)), float(max(mem_values))

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                util, mem = self._read_sample()
                if util is not None:
                    self._samples.append(util)
                if mem is not None:
                    self._peak_memory = max(self._peak_memory, mem)
            except Exception:
                pass
            self._stop_event.wait(self.interval_sec)

    def start(self) -> None:
        if not self.available:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> GpuMetrics:
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=self.interval_sec * 3)
            self._thread = None
        avg_util = float(sum(self._samples) / len(self._samples)) if self._samples else None
        peak_mem = self._peak_memory if self._peak_memory > 0 else None
        return GpuMetrics(
            avg_utilization=avg_util,
            peak_memory_mb=peak_mem,
            samples=len(self._samples),
            available=self.available,
        )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _default_if_none(value: float | int | None, default: float | int) -> float | int:
    return default if value is None else value


def _safe_ratio(num: float, den: float) -> float:
    if den <= 0:
        return 0.0
    return float(num) / float(den)


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML object: {path}")
    return data


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _round_or_none(value: float | None, ndigits: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, ndigits)


def _coerce_document_ir(value: Any) -> DocumentIR | None:
    if isinstance(value, DocumentIR):
        return value
    if isinstance(value, dict):
        try:
            return DocumentIR.model_validate(value)
        except Exception:
            return None
    return None


def _coerce_validation(value: Any) -> ValidationReport | None:
    if isinstance(value, ValidationReport):
        return value
    if isinstance(value, dict):
        try:
            return ValidationReport.model_validate(value)
        except Exception:
            return None
    return None


def _quality_score(metrics: dict[str, float]) -> float:
    empty = max(0.0, min(1.0, metrics.get("empty_page_rate", 1.0)))
    order = max(0.0, min(1.0, metrics.get("order_anomaly_rate", 1.0)))
    table = max(0.0, min(1.0, metrics.get("table_anomaly_rate", 1.0)))
    coverage = max(0.0, min(1.0, metrics.get("coverage_rate", 0.0)))
    return float(((1.0 - empty) + (1.0 - order) + (1.0 - table) + coverage) / 4.0)


def _build_thresholds(best: dict[str, float]) -> dict[str, float]:
    return {
        "empty_page_rate_max": min(0.06, best["empty_page_rate"] + 0.02),
        "order_anomaly_rate_max": min(0.10, best["order_anomaly_rate"] + 0.03),
        "table_anomaly_rate_max": min(0.20, best["table_anomaly_rate"] + 0.05),
        "coverage_rate_min": max(0.94, best["coverage_rate"] - 0.02),
    }


def _passes_quality(metrics: dict[str, float], thresholds: dict[str, float]) -> bool:
    return (
        metrics.get("empty_page_rate", 1.0) <= thresholds["empty_page_rate_max"]
        and metrics.get("order_anomaly_rate", 1.0) <= thresholds["order_anomaly_rate_max"]
        and metrics.get("table_anomaly_rate", 1.0) <= thresholds["table_anomaly_rate_max"]
        and metrics.get("coverage_rate", 0.0) >= thresholds["coverage_rate_min"]
    )


def _select_engine(engine_rows: list[dict[str, Any]]) -> tuple[str | None, str]:
    if not engine_rows:
        return None, "No engine rows available."

    passed = [row for row in engine_rows if row["pass_quality_floor"]]
    if not passed:
        best = sorted(
            engine_rows,
            key=lambda row: (row["quality_score"], row["weighted_pages_per_sec"]),
            reverse=True,
        )[0]
        return (
            best["engine"],
            "No engine passed quality floor; selected best quality score as fallback.",
        )

    if len(passed) == 1:
        winner = passed[0]
        return winner["engine"], "Only one engine passed quality floor."

    ordered = sorted(passed, key=lambda row: row["weighted_pages_per_sec"], reverse=True)
    top = ordered[0]
    second = ordered[1]
    speed_gap_ratio = _safe_ratio(
        top["weighted_pages_per_sec"] - second["weighted_pages_per_sec"],
        max(top["weighted_pages_per_sec"], 1e-9),
    )
    if speed_gap_ratio < 0.10 and second["quality_score"] > top["quality_score"]:
        return second["engine"], "Speed gap < 10%; selected higher quality score."
    return top["engine"], "Selected highest weighted pages/s among quality-passing engines."


def _calibration_candidates(doc_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in doc_rows
        if int(row.get("parse_error_count", 0)) == 0 and not bool(row.get("manual_review_required", False))
    ]


def _update_quality_floor_config(config_path: Path, thresholds: dict[str, float]) -> None:
    data = _load_yaml(config_path)
    metrics = data.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("quality_floor.yaml: metrics must be an object")

    metric_targets = {
        "empty_page_rate": ("runtime_max", thresholds["empty_page_rate_max"]),
        "order_anomaly_rate": ("runtime_max", thresholds["order_anomaly_rate_max"]),
        "table_anomaly_rate": ("runtime_max", thresholds["table_anomaly_rate_max"]),
        "coverage_rate": ("runtime_min", thresholds["coverage_rate_min"]),
    }
    for metric_name, (field_name, value) in metric_targets.items():
        metric_item = metrics.setdefault(metric_name, {})
        if not isinstance(metric_item, dict):
            metric_item = {}
            metrics[metric_name] = metric_item
        metric_item[field_name] = round(float(value), 6)

    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _fmt(value: Any, ndigits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{ndigits}f}"
    return str(value)


def _write_benchmark_summary(path: Path, run_rows: list[dict[str, Any]], engine_rows: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    lines.append("# benchmark_summary")
    lines.append("")
    lines.append("## Engine Aggregate")
    lines.append("")
    lines.append("| engine | pass_quality_floor | weighted_pages_per_sec | quality_score | gpu_util_avg_pct | gpu_mem_peak_mb |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for row in sorted(engine_rows, key=lambda x: x["engine"]):
        lines.append(
            f"| {row['engine']} | {str(row['pass_quality_floor']).lower()} | "
            f"{_fmt(row['weighted_pages_per_sec'])} | {_fmt(row['quality_score'])} | "
            f"{_fmt(row.get('gpu_util_avg_pct'))} | {_fmt(row.get('gpu_mem_peak_mb'))} |"
        )

    lines.append("")
    lines.append("## Run Details")
    lines.append("")
    lines.append(
        "| engine | doc_id | run_type | run_idx | duration_sec | pages | pages_per_sec | "
        "manual_review | cascade_triggered | cascade_attempt | parse_error |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for row in run_rows:
        lines.append(
            f"| {row['engine']} | {row['doc_id']} | {row['run_type']} | {row['run_index']} | "
            f"{_fmt(row['duration_sec'])} | {row['pages']} | {_fmt(row['pages_per_sec'])} | "
            f"{str(bool(row['manual_review_required'])).lower()} | "
            f"{str(bool(row.get('cascade_triggered', False))).lower()} | "
            f"{int(row.get('cascade_attempt', 0))} | {row['parse_error'] or ''} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_quality_baseline(path: Path, thresholds: dict[str, float], best: dict[str, float], doc_rows: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    lines.append("# quality_floor_baseline")
    lines.append("")
    lines.append("## Calibrated Thresholds")
    lines.append("")
    lines.append(f"- empty_page_rate <= {thresholds['empty_page_rate_max']:.6f}")
    lines.append(f"- order_anomaly_rate <= {thresholds['order_anomaly_rate_max']:.6f}")
    lines.append(f"- table_anomaly_rate <= {thresholds['table_anomaly_rate_max']:.6f}")
    lines.append(f"- coverage_rate >= {thresholds['coverage_rate_min']:.6f}")
    lines.append("")
    lines.append("## Best Observed")
    lines.append("")
    lines.append(f"- best_empty_page_rate = {best['empty_page_rate']:.6f}")
    lines.append(f"- best_order_anomaly_rate = {best['order_anomaly_rate']:.6f}")
    lines.append(f"- best_table_anomaly_rate = {best['table_anomaly_rate']:.6f}")
    lines.append(f"- best_coverage_rate = {best['coverage_rate']:.6f}")
    lines.append("")
    lines.append("## Doc Metrics (Warm Median)")
    lines.append("")
    lines.append("| engine | doc_id | empty_page_rate | order_anomaly_rate | table_anomaly_rate | coverage_rate |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for row in doc_rows:
        lines.append(
            f"| {row['engine']} | {row['doc_id']} | {_fmt(row['empty_page_rate'])} | "
            f"{_fmt(row['order_anomaly_rate'])} | {_fmt(row['table_anomaly_rate'])} | {_fmt(row['coverage_rate'])} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_engine_decision(path: Path, chosen_engine: str | None, reason: str, engine_rows: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    lines.append("# engine_decision")
    lines.append("")
    lines.append(f"- selected_engine: {chosen_engine or 'none'}")
    lines.append(f"- reason: {reason}")
    lines.append("")
    lines.append("## Candidates")
    lines.append("")
    lines.append("| engine | pass_quality_floor | weighted_pages_per_sec | quality_score |")
    lines.append("|---|---:|---:|---:|")
    for row in sorted(engine_rows, key=lambda x: x["engine"]):
        lines.append(
            f"| {row['engine']} | {str(row['pass_quality_floor']).lower()} | "
            f"{_fmt(row['weighted_pages_per_sec'])} | {_fmt(row['quality_score'])} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark runner")
    parser.add_argument("--manifest", default="benchmarks/benchmark_set.yaml")
    parser.add_argument("--engines", default="paddle,mineru")
    parser.add_argument("--cold-runs", type=int, default=None)
    parser.add_argument("--warm-runs", type=int, default=None)
    parser.add_argument("--output-dir", default="benchmarks/runs")
    parser.add_argument("--allow-mock-parse", action="store_true")
    parser.add_argument("--engine-config", default=None)
    parser.add_argument("--max-parse-attempts", type=int, default=2)
    parser.add_argument("--max-rerun-attempts", type=int, default=1)
    parser.add_argument("--cascade-enabled", action="store_true")
    parser.add_argument("--cascade-engine", default=None, choices=["paddle", "mineru"])
    parser.add_argument("--cascade-engine-config", default=None)
    parser.add_argument("--max-cascade-attempts", type=int, default=1)
    parser.add_argument("--gpu-sample-interval-sec", type=float, default=1.0)
    parser.add_argument("--quality-config", default="configs/quality_floor.yaml")
    parser.add_argument("--freeze-thresholds", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    manifest = _load_yaml(manifest_path)

    docs = manifest.get("documents", [])
    if not isinstance(docs, list) or not docs:
        raise ValueError("Manifest must include non-empty documents list")

    run_cfg = manifest.get("runs", {})
    if not isinstance(run_cfg, dict):
        run_cfg = {}
    cold_runs = max(1, int(args.cold_runs if args.cold_runs is not None else run_cfg.get("cold_runs", 1)))
    warm_runs = max(1, int(args.warm_runs if args.warm_runs is not None else run_cfg.get("warm_runs", 3)))
    selection_mode = str(manifest.get("selection_mode", "all"))
    selection_expr = manifest.get("selection")

    engines = [e.strip() for e in str(args.engines).split(",") if e.strip()]
    if not engines:
        raise ValueError("No engines selected")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    app = build_graph()

    run_rows: list[dict[str, Any]] = []
    doc_rows: list[dict[str, Any]] = []

    for engine in engines:
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            doc_id = str(doc.get("id", "unknown"))
            pdf_path = Path(str(doc.get("path", ""))).resolve()
            if not pdf_path.exists():
                run_rows.append(
                    {
                        "engine": engine,
                        "doc_id": doc_id,
                        "run_type": "skip",
                        "run_index": 0,
                        "duration_sec": 0.0,
                        "pages": 0,
                        "pages_per_sec": 0.0,
                        "manual_review_required": True,
                        "cascade_triggered": False,
                        "cascade_attempt": 0,
                        "parse_error": f"missing file: {pdf_path}",
                        "gpu_util_avg_pct": None,
                        "gpu_mem_peak_mb": None,
                        "empty_page_rate": 1.0,
                        "order_anomaly_rate": 1.0,
                        "table_anomaly_rate": 1.0,
                        "coverage_rate": 0.0,
                    }
                )
                continue

            run_group_rows: list[dict[str, Any]] = []
            total_runs = cold_runs + warm_runs
            for idx in range(total_runs):
                run_type = "cold" if idx < cold_runs else "warm"
                run_index = (idx + 1) if run_type == "cold" else (idx - cold_runs + 1)
                run_dir = output_dir / "runs" / engine / doc_id / f"{run_type}_{run_index:02d}"
                run_dir.mkdir(parents=True, exist_ok=True)

                sampler = GpuSampler(interval_sec=args.gpu_sample_interval_sec)
                start_ts = time.perf_counter()
                sampler.start()
                cascade_engine = args.cascade_engine
                if args.cascade_enabled and cascade_engine is None:
                    cascade_engine = "mineru" if engine == "paddle" else "paddle"
                result = app.invoke(
                    {
                        "input": str(pdf_path),
                        "engine": engine,
                        "selection_mode": selection_mode,
                        "selection": selection_expr,
                        "output_dir": str(run_dir),
                        "render_thumbnails": False,
                        "allow_mock_parse": bool(args.allow_mock_parse),
                        "engine_config": args.engine_config,
                        "max_parse_attempts": max(1, int(args.max_parse_attempts)),
                        "max_rerun_attempts": max(0, int(args.max_rerun_attempts)),
                        "cascade_enabled": bool(args.cascade_enabled),
                        "cascade_engine": cascade_engine,
                        "cascade_engine_config": args.cascade_engine_config,
                        "max_cascade_attempts": max(0, int(args.max_cascade_attempts)),
                        "parse_attempt": 0,
                        "rerun_attempt": 0,
                        "cascade_attempt": 0,
                        "parse_error": None,
                        "rerun_active": False,
                        "cascade_active": False,
                        "manual_review_required": False,
                    }
                )
                gpu_metrics = sampler.stop()
                duration_sec = max(1e-6, time.perf_counter() - start_ts)

                document_ir = _coerce_document_ir(result.get("document_ir"))
                validation = _coerce_validation(result.get("validation"))
                pages = 0
                if document_ir is not None:
                    pages = len(document_ir.pages)
                if pages <= 0:
                    selected = result.get("selected", {})
                    if isinstance(selected, dict):
                        pages = int(selected.get("selected_count", 0))

                has_parse_error = bool(result.get("parse_error"))
                effective_pages = int(max(0, pages))
                pages_per_sec: float | None = (
                    None if has_parse_error or effective_pages == 0
                    else float(_safe_ratio(float(effective_pages), float(duration_sec)))
                )
                row = {
                    "engine": engine,
                    "doc_id": doc_id,
                    "run_type": run_type,
                    "run_index": run_index,
                    "duration_sec": float(duration_sec),
                    "pages": effective_pages,
                    "pages_per_sec": pages_per_sec,
                    "manual_review_required": bool(result.get("manual_review_required", False)),
                    "cascade_triggered": bool(result.get("cascade_active", False)),
                    "cascade_attempt": int(result.get("cascade_attempt", 0)),
                    "parse_error": result.get("parse_error"),
                    "gpu_util_avg_pct": _round_or_none(gpu_metrics.avg_utilization, 4),
                    "gpu_mem_peak_mb": _round_or_none(gpu_metrics.peak_memory_mb, 3),
                    "gpu_samples": gpu_metrics.samples,
                    "gpu_available": gpu_metrics.available,
                    "empty_page_rate": float(validation.empty_page_rate) if validation is not None else 1.0,
                    "order_anomaly_rate": float(validation.order_anomaly_rate) if validation is not None else 1.0,
                    "table_anomaly_rate": float(validation.table_anomaly_rate) if validation is not None else 1.0,
                    "coverage_rate": float(validation.coverage_rate) if validation is not None else 0.0,
                }
                _write_json(run_dir / "run_result.json", row)
                run_rows.append(row)
                run_group_rows.append(row)

            warm_group = [r for r in run_group_rows if r["run_type"] == "warm"]
            if not warm_group:
                continue
            warm_pages = [float(r["pages"]) for r in warm_group]
            warm_pps = [float(r["pages_per_sec"]) for r in warm_group if r["pages_per_sec"] is not None]
            warm_empty = [float(r["empty_page_rate"]) for r in warm_group]
            warm_order = [float(r["order_anomaly_rate"]) for r in warm_group]
            warm_table = [float(r["table_anomaly_rate"]) for r in warm_group]
            warm_coverage = [float(r["coverage_rate"]) for r in warm_group]
            warm_gpu_util = [float(r["gpu_util_avg_pct"]) for r in warm_group if r["gpu_util_avg_pct"] is not None]
            warm_gpu_mem = [float(r["gpu_mem_peak_mb"]) for r in warm_group if r["gpu_mem_peak_mb"] is not None]
            doc_rows.append(
                {
                    "engine": engine,
                    "doc_id": doc_id,
                    "pages": int(_default_if_none(_median(warm_pages), 0)),
                    "pages_per_sec": float(_default_if_none(_median(warm_pps), 0.0)),
                    "empty_page_rate": float(_default_if_none(_median(warm_empty), 1.0)),
                    "order_anomaly_rate": float(_default_if_none(_median(warm_order), 1.0)),
                    "table_anomaly_rate": float(_default_if_none(_median(warm_table), 1.0)),
                    "coverage_rate": float(_default_if_none(_median(warm_coverage), 0.0)),
                    "gpu_util_avg_pct": _mean(warm_gpu_util),
                    "gpu_mem_peak_mb": max(warm_gpu_mem) if warm_gpu_mem else None,
                    "manual_review_required": any(bool(r["manual_review_required"]) for r in warm_group),
                    "parse_error_count": sum(1 for r in warm_group if r["parse_error"]),
                }
            )

    if not doc_rows:
        raise RuntimeError("No benchmark doc rows were produced.")

    metric_candidates = _calibration_candidates(doc_rows)
    calibration_warning: str | None = None
    if not metric_candidates:
        calibration_warning = (
            "No successful warm runs were found (all rows had parse errors or manual review); "
            "threshold calibration used fallback rows and should not be frozen."
        )
        metric_candidates = doc_rows

    best = {
        "empty_page_rate": min(float(row["empty_page_rate"]) for row in metric_candidates),
        "order_anomaly_rate": min(float(row["order_anomaly_rate"]) for row in metric_candidates),
        "table_anomaly_rate": min(float(row["table_anomaly_rate"]) for row in metric_candidates),
        "coverage_rate": max(float(row["coverage_rate"]) for row in metric_candidates),
    }
    thresholds = _build_thresholds(best)

    engine_rows: list[dict[str, Any]] = []
    for engine in engines:
        engine_docs = [row for row in doc_rows if row["engine"] == engine]
        if not engine_docs:
            continue
        total_pages = float(sum(max(0, int(row["pages"])) for row in engine_docs))
        weighted_pps_num = float(sum(float(row["pages_per_sec"]) * float(row["pages"]) for row in engine_docs))
        weighted_pps = _safe_ratio(weighted_pps_num, total_pages) if total_pages > 0 else 0.0

        avg_metrics = {
            "empty_page_rate": float(
                _default_if_none(_mean([float(r["empty_page_rate"]) for r in engine_docs]), 1.0)
            ),
            "order_anomaly_rate": float(
                _default_if_none(_mean([float(r["order_anomaly_rate"]) for r in engine_docs]), 1.0)
            ),
            "table_anomaly_rate": float(
                _default_if_none(_mean([float(r["table_anomaly_rate"]) for r in engine_docs]), 1.0)
            ),
            "coverage_rate": float(
                _default_if_none(_mean([float(r["coverage_rate"]) for r in engine_docs]), 0.0)
            ),
        }
        has_errors = any(int(r["parse_error_count"]) > 0 or bool(r["manual_review_required"]) for r in engine_docs)
        pass_floor = (not has_errors) and _passes_quality(avg_metrics, thresholds)
        gpu_util_values = [float(r["gpu_util_avg_pct"]) for r in engine_docs if r["gpu_util_avg_pct"] is not None]
        gpu_mem_values = [float(r["gpu_mem_peak_mb"]) for r in engine_docs if r["gpu_mem_peak_mb"] is not None]
        engine_rows.append(
            {
                "engine": engine,
                "weighted_pages_per_sec": float(weighted_pps),
                "quality_score": float(_quality_score(avg_metrics)),
                "pass_quality_floor": bool(pass_floor),
                "gpu_util_avg_pct": _mean(gpu_util_values),
                "gpu_mem_peak_mb": max(gpu_mem_values) if gpu_mem_values else None,
                **avg_metrics,
            }
        )

    chosen_engine, reason = _select_engine(engine_rows)

    freeze_applied = False
    freeze_warning: str | None = None
    if args.freeze_thresholds:
        if calibration_warning is not None:
            freeze_warning = (
                "Freeze skipped: no successful warm runs were available for reliable threshold calibration."
            )
            print(f"[WARN] {freeze_warning}")
        else:
            quality_cfg = Path(args.quality_config).resolve()
            _update_quality_floor_config(quality_cfg, thresholds)
            freeze_applied = True

    _write_json(output_dir / "benchmark_runs.json", run_rows)
    _write_json(output_dir / "benchmark_doc_rows.json", doc_rows)
    _write_json(output_dir / "benchmark_engine_rows.json", engine_rows)
    _write_json(
        output_dir / "benchmark_overview.json",
        {
            "chosen_engine": chosen_engine,
            "reason": reason,
            "thresholds": thresholds,
            "best": best,
            "calibration_warning": calibration_warning,
            "freeze_applied": freeze_applied,
            "freeze_warning": freeze_warning,
            "cold_runs": cold_runs,
            "warm_runs": warm_runs,
            "cascade_enabled": bool(args.cascade_enabled),
            "cascade_engine": args.cascade_engine,
            "cascade_engine_config": args.cascade_engine_config,
            "max_cascade_attempts": max(0, int(args.max_cascade_attempts)),
            "engines": engines,
        },
    )

    _write_benchmark_summary(output_dir / "benchmark_summary.md", run_rows, engine_rows)
    _write_quality_baseline(output_dir / "quality_floor_baseline.md", thresholds, best, doc_rows)
    _write_engine_decision(output_dir / "engine_decision.md", chosen_engine, reason, engine_rows)


if __name__ == "__main__":
    main()
