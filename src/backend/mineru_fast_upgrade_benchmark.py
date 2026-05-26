from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import yaml

from .pipeline_graph import _write_pipeline_outputs
from .pipeline_graph import build_graph
from .types import DocumentIR
from .types import ValidationReport


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Candidate:
    id: str
    label: str
    version: str
    backend: str
    role: str
    engine_config: Path


@dataclass(frozen=True)
class DocumentCase:
    id: str
    path: Path
    selection_mode: str
    selection: str | None
    tags: tuple[str, ...]
    expected_terms: tuple[str, ...]
    stamp_terms: tuple[str, ...]


@dataclass(frozen=True)
class ResourceMetrics:
    gpu_util_avg: float | None
    gpu_util_p95: float | None
    gpu_memory_peak_mb: float | None
    cpu_util_avg: float | None
    samples: int
    gpu_available: bool


class ResourceSampler:
    def __init__(self, interval_sec: float = 1.0) -> None:
        self.interval_sec = max(0.2, float(interval_sec))
        self.gpu_available = shutil.which("nvidia-smi") is not None
        self._gpu_utils: list[float] = []
        self._gpu_memories: list[float] = []
        self._cpu_utils: list[float] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _read_gpu() -> tuple[float | None, float | None]:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return None, None
        utils: list[float] = []
        memories: list[float] = []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 2:
                continue
            try:
                utils.append(float(parts[0]))
                memories.append(float(parts[1]))
            except ValueError:
                continue
        if not utils:
            return None, None
        return sum(utils) / len(utils), max(memories)

    def _loop(self) -> None:
        psutil.cpu_percent(interval=None)
        while not self._stop_event.is_set():
            self._cpu_utils.append(float(psutil.cpu_percent(interval=None)))
            if self.gpu_available:
                try:
                    gpu_util, gpu_memory = self._read_gpu()
                    if gpu_util is not None:
                        self._gpu_utils.append(float(gpu_util))
                    if gpu_memory is not None:
                        self._gpu_memories.append(float(gpu_memory))
                except Exception:
                    pass
            self._stop_event.wait(self.interval_sec)

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> ResourceMetrics:
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=self.interval_sec * 3)
            self._thread = None
        return ResourceMetrics(
            gpu_util_avg=_mean(self._gpu_utils),
            gpu_util_p95=_percentile(self._gpu_utils, 95),
            gpu_memory_peak_mb=max(self._gpu_memories) if self._gpu_memories else None,
            cpu_util_avg=_mean(self._cpu_utils),
            samples=max(len(self._cpu_utils), len(self._gpu_utils)),
            gpu_available=self.gpu_available,
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML object: {path}")
    return data


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _round(value: float | None, ndigits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (float(percentile) / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


def _resolve_path(raw_path: str | Path) -> Path:
    path = Path(str(raw_path))
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


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


def _normalize_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _text_segments(document_ir: DocumentIR | None) -> list[str]:
    if document_ir is None:
        return []
    segments: list[str] = []
    for page in document_ir.pages:
        for block in page.blocks:
            normalized = _normalize_text(block.text or "")
            if normalized:
                segments.append(normalized)
    return segments


def _looks_like_heading(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized or len(normalized) > 180:
        return False
    if re.match(r"^(\d+(?:\.\d+)*)\.?\s+\S+", normalized):
        return True
    if normalized.lower() in {
        "abstract",
        "introduction",
        "background",
        "methods",
        "methodology",
        "results",
        "discussion",
        "conclusion",
        "conclusions",
        "references",
    }:
        return True
    if re.match(r"^[一二三四五六七八九十]+[、.]\s*\S+", normalized):
        return True
    return False


def _garbled_text_rate(text: str) -> float:
    normalized = text.strip()
    if not normalized:
        return 0.0
    bad_chars = sum(1 for char in normalized if char in {"�", "□", "▯", "¤"})
    bad_chars += len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", normalized))
    suspicious_spaces = len(re.findall(r"(?:\b[A-Za-z]\s){4,}[A-Za-z]\b", normalized))
    return min(1.0, float(bad_chars + suspicious_spaces) / float(len(normalized)))


def _term_recall(text: str, terms: tuple[str, ...]) -> float | None:
    terms = tuple(term for term in terms if term.strip())
    if not terms:
        return None
    lowered = text.lower()
    found = sum(1 for term in terms if term.lower() in lowered)
    return float(found) / float(len(terms))


def _quality_metrics(
    document_ir: DocumentIR | None,
    validation: ValidationReport | None,
    doc_case: DocumentCase,
) -> dict[str, Any]:
    segments = _text_segments(document_ir)
    normalized_segments = [_normalize_text(segment).lower() for segment in segments]
    unique_segments = sorted(set(segment for segment in normalized_segments if segment))
    text = "\n".join(segments)
    unique_text = "\n".join(unique_segments)
    duplicate_ratio = 0.0
    if normalized_segments:
        duplicate_ratio = 1.0 - (float(len(unique_segments)) / float(len(normalized_segments)))

    heading_count = 0
    page_count = len(document_ir.pages) if document_ir is not None else 0
    if document_ir is not None:
        for page in document_ir.pages:
            for block in page.blocks:
                if block.heading_level is not None or block.semantic_type == "heading":
                    heading_count += 1
                elif _looks_like_heading(block.text or ""):
                    heading_count += 1

    expected_recall = _term_recall(text, doc_case.expected_terms)
    stamp_recovery = _term_recall(text, doc_case.stamp_terms)

    return {
        "text_chars": len(_normalize_text(text)),
        "unique_text_chars": len(_normalize_text(unique_text)),
        "duplicate_text_ratio": duplicate_ratio,
        "garbled_text_rate": _garbled_text_rate(text),
        "expected_terms_recall": expected_recall,
        "stamp_recovery": stamp_recovery,
        "heading_count": heading_count,
        "heading_presence_rate": _safe_ratio(float(heading_count), float(max(1, page_count))),
        "empty_page_rate": validation.empty_page_rate if validation is not None else 1.0,
        "table_anomaly_rate": validation.table_anomaly_rate if validation is not None else 1.0,
        "reading_order_issue_rate": validation.order_anomaly_rate if validation is not None else 1.0,
        "coverage_rate": validation.coverage_rate if validation is not None else 0.0,
        "validation_pass_quality_floor": validation.pass_quality_floor if validation is not None else False,
    }


def _parse_candidates(manifest: dict[str, Any]) -> list[Candidate]:
    raw_candidates = manifest.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("Manifest must include a non-empty candidates list")

    candidates: list[Candidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        candidates.append(
            Candidate(
                id=str(raw["id"]),
                label=str(raw.get("label") or raw["id"]),
                version=str(raw.get("version", "")),
                backend=str(raw.get("backend", "")),
                role=str(raw.get("role", "")),
                engine_config=_resolve_path(str(raw["engine_config"])),
            )
        )
    if not candidates:
        raise ValueError("Manifest candidates list was empty after parsing")
    return candidates


def _parse_documents(manifest: dict[str, Any]) -> list[DocumentCase]:
    raw_documents = manifest.get("documents")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise ValueError("Manifest must include a non-empty documents list")

    default_selection_mode = str(manifest.get("selection_mode", "all"))
    default_selection = manifest.get("selection")
    documents: list[DocumentCase] = []
    for raw in raw_documents:
        if not isinstance(raw, dict):
            continue
        tags = tuple(str(tag) for tag in raw.get("tags", []) if str(tag).strip())
        expected_terms = tuple(
            str(term) for term in raw.get("expected_terms", []) if str(term).strip()
        )
        stamp_terms = tuple(str(term) for term in raw.get("stamp_terms", []) if str(term).strip())
        documents.append(
            DocumentCase(
                id=str(raw["id"]),
                path=_resolve_path(str(raw["path"])),
                selection_mode=str(raw.get("selection_mode", default_selection_mode)),
                selection=(
                    str(raw.get("selection", default_selection))
                    if raw.get("selection", default_selection) is not None
                    else None
                ),
                tags=tags,
                expected_terms=expected_terms,
                stamp_terms=stamp_terms,
            )
        )
    if not documents:
        raise ValueError("Manifest documents list was empty after parsing")
    return documents


def _engine_container_name(engine_config: Path) -> str | None:
    if not engine_config.exists():
        return None
    data = _load_yaml(engine_config)
    engines = data.get("engines")
    if not isinstance(engines, dict):
        return None
    mineru = engines.get("mineru")
    if not isinstance(mineru, dict):
        return None
    service = mineru.get("service")
    if not isinstance(service, dict):
        return None
    name = str(service.get("container_name", "")).strip()
    return name or None


def _stop_container(container_name: str | None) -> None:
    if not container_name:
        return
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _parse_error_kind(error: str | None) -> str | None:
    if not error:
        return None
    lowered = error.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "returncode" in lowered or "exited" in lowered or "crash" in lowered:
        return "process_crash"
    if "model" in lowered or "cache" in lowered or "download" in lowered:
        return "cache_or_model_load"
    return "parse_error"


def _candidate_quality_score(row: dict[str, Any]) -> float:
    coverage = float(row.get("unique_text_coverage") or 0.0)
    duplicate = float(row.get("duplicate_text_ratio") or 0.0)
    garbled = float(row.get("garbled_text_rate") or 0.0)
    table = float(row.get("table_anomaly_rate") or 0.0)
    order = float(row.get("reading_order_issue_rate") or 0.0)
    expected = float(row.get("expected_terms_recall") or 0.0)
    return (coverage + expected + (1 - duplicate) + (1 - garbled) + (1 - table) + (1 - order)) / 6


def _aggregate_doc_rows(run_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in run_rows:
        grouped[(str(row["candidate_id"]), str(row["doc_id"]))].append(row)

    doc_rows: list[dict[str, Any]] = []
    for (candidate_id, doc_id), rows in sorted(grouped.items()):
        warm_rows = [row for row in rows if row["run_type"] == "warm"]
        cold_rows = [row for row in rows if row["run_type"] == "cold"]
        reference_rows = warm_rows or rows
        warm_sec_per_page = [
            float(row["sec_per_page"]) for row in warm_rows if row.get("sec_per_page") is not None
        ]

        def med(key: str) -> float | None:
            values = [float(row[key]) for row in reference_rows if row.get(key) is not None]
            return _median(values)

        expected_values = [
            float(row["expected_terms_recall"])
            for row in reference_rows
            if row.get("expected_terms_recall") is not None
        ]
        stamp_values = [
            float(row["stamp_recovery"])
            for row in reference_rows
            if row.get("stamp_recovery") is not None
        ]
        tags = tuple(reference_rows[0].get("tags", [])) if reference_rows else ()
        doc_rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_label": reference_rows[0].get("candidate_label") if reference_rows else candidate_id,
                "doc_id": doc_id,
                "tags": list(tags),
                "cold_wall_time": cold_rows[0]["wall_time_sec"] if cold_rows else None,
                "warm_wall_time_median": med("wall_time_sec"),
                "warm_wall_time_p95": _percentile(
                    [float(row["wall_time_sec"]) for row in warm_rows],
                    95,
                ),
                "warm_wall_time_max": max([float(row["wall_time_sec"]) for row in warm_rows], default=None),
                "warm_sec_per_page_median": _median(warm_sec_per_page),
                "warm_sec_per_page_p95": _percentile(warm_sec_per_page, 95),
                "warm_sec_per_page_max": max(warm_sec_per_page, default=None),
                "pages": int(reference_rows[0].get("pages", 0)) if reference_rows else 0,
                "unique_text_chars": med("unique_text_chars") or 0.0,
                "duplicate_text_ratio": med("duplicate_text_ratio") or 0.0,
                "garbled_text_rate": med("garbled_text_rate") or 0.0,
                "empty_page_rate": med("empty_page_rate") or 0.0,
                "table_anomaly_rate": med("table_anomaly_rate") or 0.0,
                "reading_order_issue_rate": med("reading_order_issue_rate") or 0.0,
                "heading_presence_rate": med("heading_presence_rate") or 0.0,
                "expected_terms_recall": _median(expected_values),
                "stamp_recovery": _median(stamp_values),
                "gpu_memory_peak": max(
                    [float(row["gpu_memory_peak_mb"]) for row in rows if row.get("gpu_memory_peak_mb")],
                    default=None,
                ),
                "gpu_util_avg": _mean(
                    [float(row["gpu_util_avg"]) for row in rows if row.get("gpu_util_avg") is not None]
                ),
                "gpu_util_p95": _percentile(
                    [float(row["gpu_util_p95"]) for row in rows if row.get("gpu_util_p95") is not None],
                    95,
                ),
                "cpu_util_avg": _mean(
                    [float(row["cpu_util_avg"]) for row in rows if row.get("cpu_util_avg") is not None]
                ),
                "timeout_count": sum(1 for row in rows if row.get("error_kind") == "timeout"),
                "process_crash_count": sum(1 for row in rows if row.get("error_kind") == "process_crash"),
                "cache_or_model_load_error_count": sum(
                    1 for row in rows if row.get("error_kind") == "cache_or_model_load"
                ),
                "parse_error_count": sum(1 for row in rows if row.get("parse_error")),
                "retry_success_rate": 1.0
                if not rows
                else 1.0 - _safe_ratio(
                    float(sum(1 for row in rows if row.get("parse_error"))),
                    float(len(rows)),
                ),
            }
        )

    denominators: dict[str, float] = defaultdict(float)
    for row in doc_rows:
        denominators[str(row["doc_id"])] = max(
            denominators[str(row["doc_id"])],
            float(row.get("unique_text_chars") or 0.0),
        )
    for row in doc_rows:
        denom = denominators[str(row["doc_id"])]
        row["unique_text_coverage"] = _safe_ratio(float(row.get("unique_text_chars") or 0.0), denom)
        row["quality_score"] = _candidate_quality_score(row)
    return doc_rows


def _aggregate_candidate_rows(doc_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in doc_rows:
        grouped[str(row["candidate_id"])].append(row)

    candidate_rows: list[dict[str, Any]] = []
    for candidate_id, rows in sorted(grouped.items()):
        all_sec = [float(row["warm_sec_per_page_median"]) for row in rows if row.get("warm_sec_per_page_median")]
        all_p95 = [float(row["warm_sec_per_page_p95"]) for row in rows if row.get("warm_sec_per_page_p95")]
        ordinary_rows = [
            row
            for row in rows
            if {"ordinary", "native_text", "text"} & {str(tag) for tag in row.get("tags", [])}
        ]
        complex_rows = [
            row
            for row in rows
            if {"complex", "table", "formula", "image", "badcase"} & {str(tag) for tag in row.get("tags", [])}
        ]
        native_rows = [row for row in rows if "native_text" in {str(tag) for tag in row.get("tags", [])}]

        def avg(key: str, source: list[dict[str, Any]] = rows) -> float | None:
            values = [float(row[key]) for row in source if row.get(key) is not None]
            return _mean(values)

        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_label": rows[0].get("candidate_label", candidate_id),
                "warm_sec_per_page_median": _median(all_sec),
                "warm_sec_per_page_p95": _percentile(all_p95 or all_sec, 95),
                "ordinary_warm_sec_per_page_median": _median(
                    [
                        float(row["warm_sec_per_page_median"])
                        for row in ordinary_rows
                        if row.get("warm_sec_per_page_median")
                    ]
                ),
                "native_text_warm_sec_per_page_median": _median(
                    [
                        float(row["warm_sec_per_page_median"])
                        for row in native_rows
                        if row.get("warm_sec_per_page_median")
                    ]
                ),
                "unique_text_coverage": avg("unique_text_coverage"),
                "duplicate_text_ratio": avg("duplicate_text_ratio"),
                "garbled_text_rate": avg("garbled_text_rate"),
                "table_anomaly_rate": avg("table_anomaly_rate"),
                "reading_order_issue_rate": avg("reading_order_issue_rate"),
                "heading_presence_rate": avg("heading_presence_rate"),
                "expected_terms_recall": avg("expected_terms_recall"),
                "stamp_recovery": avg("stamp_recovery"),
                "complex_quality_score": avg("quality_score", complex_rows),
                "quality_score": avg("quality_score"),
                "gpu_memory_peak": max(
                    [float(row["gpu_memory_peak"]) for row in rows if row.get("gpu_memory_peak")],
                    default=None,
                ),
                "gpu_util_avg": avg("gpu_util_avg"),
                "gpu_util_p95": max(
                    [float(row["gpu_util_p95"]) for row in rows if row.get("gpu_util_p95")],
                    default=None,
                ),
                "cpu_util_avg": avg("cpu_util_avg"),
                "timeout_count": sum(int(row["timeout_count"]) for row in rows),
                "process_crash_count": sum(int(row["process_crash_count"]) for row in rows),
                "cache_or_model_load_error_count": sum(
                    int(row["cache_or_model_load_error_count"]) for row in rows
                ),
                "parse_error_count": sum(int(row["parse_error_count"]) for row in rows),
                "retry_success_rate": avg("retry_success_rate"),
            }
        )
    return candidate_rows


def _candidate_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["candidate_id"]): row for row in rows}


def _metric_not_worse(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    key: str,
    tolerance: float = 0.0,
    lower_is_better: bool = True,
) -> bool:
    cand_value = candidate.get(key)
    base_value = baseline.get(key)
    if cand_value is None or base_value is None:
        return False
    cand = float(cand_value)
    base = float(base_value)
    if lower_is_better:
        return cand <= base + tolerance
    return cand + tolerance >= base


def _quality_improved(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    table_better = _metric_not_worse(candidate, baseline, "table_anomaly_rate", -0.03)
    heading_better = float(candidate.get("heading_presence_rate") or 0.0) >= (
        float(baseline.get("heading_presence_rate") or 0.0) + 0.05
    )
    stamp_candidate = candidate.get("stamp_recovery")
    stamp_baseline = baseline.get("stamp_recovery")
    stamp_better = (
        stamp_candidate is not None
        and stamp_baseline is not None
        and float(stamp_candidate) >= float(stamp_baseline) + 0.20
    )
    return bool(table_better or heading_better or stamp_better)


def _evaluate_decisions(candidate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _candidate_map(candidate_rows)
    baseline = rows.get("A")
    pipeline3 = rows.get("B")
    hybrid3 = rows.get("C")
    if baseline is None:
        return {"error": "Missing baseline candidate A"}

    decisions: dict[str, Any] = {}

    if pipeline3 is not None:
        b_checks = {
            "median_latency_lte_1_25x": (
                pipeline3.get("warm_sec_per_page_median") is not None
                and baseline.get("warm_sec_per_page_median") is not None
                and float(pipeline3["warm_sec_per_page_median"])
                <= float(baseline["warm_sec_per_page_median"]) * 1.25
            ),
            "p95_latency_lte_1_35x": (
                pipeline3.get("warm_sec_per_page_p95") is not None
                and baseline.get("warm_sec_per_page_p95") is not None
                and float(pipeline3["warm_sec_per_page_p95"])
                <= float(baseline["warm_sec_per_page_p95"]) * 1.35
            ),
            "unique_text_coverage_not_down": _metric_not_worse(
                pipeline3,
                baseline,
                "unique_text_coverage",
                lower_is_better=False,
            ),
            "duplicate_text_ratio_not_up": _metric_not_worse(
                pipeline3,
                baseline,
                "duplicate_text_ratio",
            ),
            "table_anomaly_rate_not_up": _metric_not_worse(
                pipeline3,
                baseline,
                "table_anomaly_rate",
            ),
            "reading_order_not_up": _metric_not_worse(
                pipeline3,
                baseline,
                "reading_order_issue_rate",
            ),
            "key_badcase_improved": _quality_improved(pipeline3, baseline),
            "stable": (
                int(pipeline3.get("timeout_count") or 0) == 0
                and int(pipeline3.get("process_crash_count") or 0) == 0
                and int(pipeline3.get("cache_or_model_load_error_count") or 0) == 0
            ),
        }
        decisions["mineru3_pipeline_fast"] = {
            "pass": all(b_checks.values()),
            "checks": b_checks,
            "recommendation": (
                "promote B to fast"
                if all(b_checks.values())
                else "do not promote B without more tuning"
            ),
        }

    if hybrid3 is not None:
        c_checks = {
            "ordinary_latency_lte_1_30x": (
                hybrid3.get("ordinary_warm_sec_per_page_median") is not None
                and baseline.get("ordinary_warm_sec_per_page_median") is not None
                and float(hybrid3["ordinary_warm_sec_per_page_median"])
                <= float(baseline["ordinary_warm_sec_per_page_median"]) * 1.30
            ),
            "all_p95_latency_lte_1_50x": (
                hybrid3.get("warm_sec_per_page_p95") is not None
                and baseline.get("warm_sec_per_page_p95") is not None
                and float(hybrid3["warm_sec_per_page_p95"])
                <= float(baseline["warm_sec_per_page_p95"]) * 1.50
            ),
            "native_text_stable_or_faster": (
                hybrid3.get("native_text_warm_sec_per_page_median") is not None
                and baseline.get("native_text_warm_sec_per_page_median") is not None
                and float(hybrid3["native_text_warm_sec_per_page_median"])
                <= float(baseline["native_text_warm_sec_per_page_median"]) * 1.30
            ),
            "complex_quality_beats_b": (
                pipeline3 is not None
                and hybrid3.get("complex_quality_score") is not None
                and pipeline3.get("complex_quality_score") is not None
                and float(hybrid3["complex_quality_score"])
                >= float(pipeline3["complex_quality_score"]) + 0.03
            ),
            "ordinary_no_regression": (
                float(hybrid3.get("duplicate_text_ratio") or 1.0)
                <= float(baseline.get("duplicate_text_ratio") or 0.0) + 0.02
                and float(hybrid3.get("garbled_text_rate") or 1.0)
                <= float(baseline.get("garbled_text_rate") or 0.0) + 0.01
            ),
            "failure_rate_not_higher": int(hybrid3.get("parse_error_count") or 0)
            <= int(baseline.get("parse_error_count") or 0),
        }
        decisions["mineru3_hybrid_fast"] = {
            "pass": all(c_checks.values()),
            "checks": c_checks,
            "recommendation": (
                "promote C to fast"
                if all(c_checks.values())
                else "keep C for hard-page router or repair"
            ),
        }

    decisions["fallback_policy"] = {
        "recommendation": (
            "Keep PaddleOCR-VL / seal OCR as stamp, scan, skew, illumination, and bad-image fallback."
        )
    }
    return decisions


def _write_markdown_report(
    path: Path,
    candidate_rows: list[dict[str, Any]],
    decisions: dict[str, Any],
) -> None:
    lines: list[str] = ["# MinerU Fast Upgrade Benchmark v2", ""]
    lines.append("## Candidate Summary")
    lines.append("")
    lines.append(
        "| candidate | sec/page median | sec/page p95 | quality | text coverage | dup ratio | "
        "table anomaly | stamp recovery | errors |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in sorted(candidate_rows, key=lambda item: str(item["candidate_id"])):
        errors = (
            int(row.get("timeout_count") or 0)
            + int(row.get("process_crash_count") or 0)
            + int(row.get("cache_or_model_load_error_count") or 0)
            + int(row.get("parse_error_count") or 0)
        )
        lines.append(
            f"| {row['candidate_id']} {row['candidate_label']} | "
            f"{_fmt(row.get('warm_sec_per_page_median'))} | "
            f"{_fmt(row.get('warm_sec_per_page_p95'))} | "
            f"{_fmt(row.get('quality_score'))} | "
            f"{_fmt(row.get('unique_text_coverage'))} | "
            f"{_fmt(row.get('duplicate_text_ratio'))} | "
            f"{_fmt(row.get('table_anomaly_rate'))} | "
            f"{_fmt(row.get('stamp_recovery'))} | {errors} |"
        )
    lines.append("")
    lines.append("## Decisions")
    lines.append("")
    for key, decision in decisions.items():
        if not isinstance(decision, dict):
            continue
        lines.append(f"### {key}")
        lines.append("")
        if "pass" in decision:
            lines.append(f"- pass: {str(bool(decision['pass'])).lower()}")
        if decision.get("recommendation"):
            lines.append(f"- recommendation: {decision['recommendation']}")
        checks = decision.get("checks")
        if isinstance(checks, dict):
            for check_name, value in checks.items():
                lines.append(f"- {check_name}: {str(bool(value)).lower()}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _run_candidate_document(
    *,
    app: Any,
    candidate: Candidate,
    doc_case: DocumentCase,
    run_type: str,
    run_index: int,
    output_dir: Path,
    sample_interval_sec: float,
    max_parse_attempts: int,
) -> dict[str, Any]:
    run_dir = output_dir / "runs" / candidate.id / doc_case.id / f"{run_type}_{run_index:02d}"
    sampler = ResourceSampler(sample_interval_sec)
    started = time.perf_counter()
    sampler.start()
    result: dict[str, Any]
    try:
        result = app.invoke(
            {
                "input": str(doc_case.path),
                "engine": "mineru",
                "selection_mode": doc_case.selection_mode,
                "selection": doc_case.selection,
                "output_dir": str(run_dir),
                "render_thumbnails": False,
                "allow_mock_parse": False,
                "engine_config": str(candidate.engine_config),
                "max_parse_attempts": max(1, max_parse_attempts),
                "max_rerun_attempts": 0,
                "cascade_enabled": False,
                "cascade_engine": None,
                "cascade_engine_config": None,
                "max_cascade_attempts": 0,
                "parse_attempt": 0,
                "rerun_attempt": 0,
                "cascade_attempt": 0,
                "parse_error": None,
                "rerun_active": False,
                "cascade_active": False,
                "manual_review_required": False,
                "performance": {"nodes": {}},
            }
        )
    finally:
        resources = sampler.stop()
    wall_time = time.perf_counter() - started

    document_ir = _coerce_document_ir(result.get("document_ir"))
    validation = _coerce_validation(result.get("validation"))
    performance = result.get("performance") if isinstance(result.get("performance"), dict) else None
    _write_pipeline_outputs(
        output_dir=run_dir,
        document_ir=document_ir,
        validation=validation,
        summary={
            "candidate_id": candidate.id,
            "candidate_label": candidate.label,
            "parse_error": result.get("parse_error"),
            "manual_review_required": bool(result.get("manual_review_required", False)),
        },
        performance_profile=performance,
    )

    selected = result.get("selected") if isinstance(result.get("selected"), dict) else {}
    selected_pages = selected.get("selected_page_indices", []) if isinstance(selected, dict) else []
    page_count = len(document_ir.pages) if document_ir is not None else len(selected_pages)
    quality = _quality_metrics(document_ir, validation, doc_case)
    parse_error = result.get("parse_error")
    row = {
        "candidate_id": candidate.id,
        "candidate_label": candidate.label,
        "candidate_version": candidate.version,
        "candidate_backend": candidate.backend,
        "doc_id": doc_case.id,
        "doc_path": str(doc_case.path),
        "tags": list(doc_case.tags),
        "selection_mode": doc_case.selection_mode,
        "selection": doc_case.selection,
        "run_type": run_type,
        "run_index": run_index,
        "wall_time_sec": _round(wall_time),
        "pages": page_count,
        "sec_per_page": _round(_safe_ratio(float(wall_time), float(page_count))) if page_count else None,
        "manual_review_required": bool(result.get("manual_review_required", False)),
        "parse_error": parse_error,
        "error_kind": _parse_error_kind(str(parse_error)) if parse_error else None,
        "gpu_util_avg": _round(resources.gpu_util_avg),
        "gpu_util_p95": _round(resources.gpu_util_p95),
        "gpu_memory_peak_mb": _round(resources.gpu_memory_peak_mb),
        "cpu_util_avg": _round(resources.cpu_util_avg),
        "resource_samples": resources.samples,
        "gpu_available": resources.gpu_available,
        **quality,
    }
    _write_json(run_dir / "benchmark_row.json", row)
    return row


def run_benchmark(
    *,
    manifest_path: Path,
    output_dir: Path,
    cold_runs: int | None = None,
    warm_runs: int | None = None,
    keep_services: bool = False,
    sample_interval_sec: float = 1.0,
    max_parse_attempts: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    manifest = _load_yaml(manifest_path)
    candidates = _parse_candidates(manifest)
    documents = _parse_documents(manifest)
    run_cfg = manifest.get("runs") if isinstance(manifest.get("runs"), dict) else {}
    cold_count = max(0, int(cold_runs if cold_runs is not None else run_cfg.get("cold_runs", 0)))
    warm_count = max(1, int(warm_runs if warm_runs is not None else run_cfg.get("warm_runs", 5)))

    missing_docs = [str(doc.path) for doc in documents if not doc.path.exists()]
    missing_configs = [str(candidate.engine_config) for candidate in candidates if not candidate.engine_config.exists()]
    dry_payload = {
        "manifest": str(manifest_path),
        "output_dir": str(output_dir),
        "candidate_count": len(candidates),
        "document_count": len(documents),
        "cold_runs": cold_count,
        "warm_runs": warm_count,
        "missing_docs": missing_docs,
        "missing_configs": missing_configs,
    }
    if dry_run:
        return dry_payload
    if missing_docs:
        raise FileNotFoundError(f"Benchmark manifest references missing PDFs: {missing_docs}")
    if missing_configs:
        raise FileNotFoundError(f"Benchmark manifest references missing configs: {missing_configs}")

    output_dir.mkdir(parents=True, exist_ok=True)
    app = build_graph()
    run_rows: list[dict[str, Any]] = []

    for candidate in candidates:
        container_name = _engine_container_name(candidate.engine_config)
        if not keep_services:
            _stop_container(container_name)
        for doc_case in documents:
            for run_index in range(1, cold_count + 1):
                run_rows.append(
                    _run_candidate_document(
                        app=app,
                        candidate=candidate,
                        doc_case=doc_case,
                        run_type="cold",
                        run_index=run_index,
                        output_dir=output_dir,
                        sample_interval_sec=sample_interval_sec,
                        max_parse_attempts=max_parse_attempts,
                    )
                )
            for run_index in range(1, warm_count + 1):
                run_rows.append(
                    _run_candidate_document(
                        app=app,
                        candidate=candidate,
                        doc_case=doc_case,
                        run_type="warm",
                        run_index=run_index,
                        output_dir=output_dir,
                        sample_interval_sec=sample_interval_sec,
                        max_parse_attempts=max_parse_attempts,
                    )
                )
        if not keep_services:
            _stop_container(container_name)

    doc_rows = _aggregate_doc_rows(run_rows)
    candidate_rows = _aggregate_candidate_rows(doc_rows)
    decisions = _evaluate_decisions(candidate_rows)

    overview = {
        "manifest": str(manifest_path),
        "cold_runs": cold_count,
        "warm_runs": warm_count,
        "decisions": decisions,
    }
    _write_json(output_dir / "benchmark_runs.json", run_rows)
    _write_json(output_dir / "benchmark_doc_rows.json", doc_rows)
    _write_json(output_dir / "benchmark_candidate_rows.json", candidate_rows)
    _write_json(output_dir / "benchmark_overview.json", overview)
    _write_markdown_report(output_dir / "benchmark_summary.md", candidate_rows, decisions)
    return overview


def main() -> None:
    parser = argparse.ArgumentParser(description="MinerU fast upgrade benchmark v2")
    parser.add_argument("--manifest", default="benchmarks/mineru_fast_upgrade_v2.yaml")
    parser.add_argument("--output-dir", default="reports/mineru_fast_upgrade_v2")
    parser.add_argument("--cold-runs", type=int, default=None)
    parser.add_argument("--warm-runs", type=int, default=None)
    parser.add_argument("--keep-services", action="store_true")
    parser.add_argument("--sample-interval-sec", type=float, default=1.0)
    parser.add_argument("--max-parse-attempts", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    overview = run_benchmark(
        manifest_path=_resolve_path(args.manifest),
        output_dir=_resolve_path(args.output_dir),
        cold_runs=args.cold_runs,
        warm_runs=args.warm_runs,
        keep_services=bool(args.keep_services),
        sample_interval_sec=float(args.sample_interval_sec),
        max_parse_attempts=int(args.max_parse_attempts),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(overview, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
