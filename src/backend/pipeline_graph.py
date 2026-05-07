from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from typing import Literal
from typing import TypedDict

from langgraph.graph import END
from langgraph.graph import StateGraph

from .ingestion_agent import IngestionAgent
from .ir_builder_agent import IRBuilderAgent
from .markdown_export import document_ir_to_markdown
from .parse_agent import ParseAgent
from .selection_agent import SelectionAgent
from .types import DocumentIR
from .types import ValidationReport
from .validation_agent import ValidationAgent
from .visual_agent import VisualAgent


class PipelineState(TypedDict, total=False):
    input: str
    engine: str
    selection_mode: str
    selection: str | None
    output_dir: str
    render_thumbnails: bool
    allow_mock_parse: bool
    engine_config: str | None
    max_parse_attempts: int
    max_rerun_attempts: int
    cascade_enabled: bool
    cascade_engine: str | None
    cascade_engine_config: str | None
    max_cascade_attempts: int
    parse_attempt: int
    rerun_attempt: int
    cascade_attempt: int
    parse_error: str | None
    rerun_active: bool
    cascade_active: bool
    manual_review_required: bool
    ingestion: dict[str, Any]
    selected: dict[str, Any]
    rerun_selection: dict[str, Any]
    parsed: dict[str, Any]
    document_ir: DocumentIR
    updated_page_indices: list[int]
    visual_agent: dict[str, Any]
    validation: ValidationReport
    performance: dict[str, Any]


INGESTION_AGENT = IngestionAgent()
SELECTION_AGENT = SelectionAgent()
PARSE_AGENT = ParseAgent(allow_mock_output=False)
PARSE_AGENT_MOCK = ParseAgent(allow_mock_output=True)
PARSE_AGENT_CACHE: dict[tuple[str, bool], ParseAgent] = {}
IR_BUILDER_AGENT = IRBuilderAgent()
VALIDATION_AGENT = ValidationAgent()
VISUAL_AGENT = VisualAgent()


def _resolve_parse_agent(allow_mock: bool, engine_config: str | None) -> ParseAgent:
    if not engine_config:
        return PARSE_AGENT_MOCK if allow_mock else PARSE_AGENT

    config_key = str(Path(engine_config).resolve())
    cache_key = (config_key, allow_mock)
    parse_agent = PARSE_AGENT_CACHE.get(cache_key)
    if parse_agent is None:
        parse_agent = ParseAgent(config_path=config_key, allow_mock_output=allow_mock)
        PARSE_AGENT_CACHE[cache_key] = parse_agent
    return parse_agent


def _merge_document_ir(base_ir: DocumentIR, rerun_ir: DocumentIR) -> DocumentIR:
    pages_by_index = {page.page_index: page for page in base_ir.pages}
    for page in rerun_ir.pages:
        pages_by_index[page.page_index] = page
    merged_pages = [pages_by_index[idx] for idx in sorted(pages_by_index.keys())]
    return base_ir.model_copy(update={"pages": merged_pages, "generated_at": rerun_ir.generated_at})


def _copy_performance(state: PipelineState) -> dict[str, Any]:
    raw = state.get("performance")
    if not isinstance(raw, dict):
        return {"nodes": {}}

    copied: dict[str, Any] = {key: value for key, value in raw.items() if key != "nodes"}
    nodes_raw = raw.get("nodes", {})
    nodes_out: dict[str, Any] = {}
    if isinstance(nodes_raw, dict):
        for node_name, node_value in nodes_raw.items():
            if not isinstance(node_value, dict):
                continue
            runs = node_value.get("runs", [])
            nodes_out[str(node_name)] = {
                "count": int(node_value.get("count", 0)),
                "total_sec": float(node_value.get("total_sec", 0.0)),
                "runs": list(runs) if isinstance(runs, list) else [],
            }
    copied["nodes"] = nodes_out
    return copied


def _record_node_timing(
    state: PipelineState,
    node_name: str,
    elapsed_sec: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    performance = _copy_performance(state)
    nodes = performance.setdefault("nodes", {})
    node_entry = nodes.setdefault(node_name, {"count": 0, "total_sec": 0.0, "runs": []})
    count = int(node_entry.get("count", 0)) + 1
    total_sec = float(node_entry.get("total_sec", 0.0)) + elapsed_sec
    runs = list(node_entry.get("runs", []))
    run_summary = {"index": count, "sec": round(elapsed_sec, 4)}
    if extra:
        run_summary.update(extra)
    runs.append(run_summary)
    node_entry["count"] = count
    node_entry["total_sec"] = round(total_sec, 4)
    node_entry["runs"] = runs
    nodes[node_name] = node_entry
    performance["nodes"] = nodes
    return performance


def _ingest_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    ingestion = INGESTION_AGENT.run(
        pdf_path=state["input"],
        output_dir=state["output_dir"],
        render_thumbnails=bool(state.get("render_thumbnails", False)),
    )
    return {
        "ingestion": ingestion,
        "performance": _record_node_timing(
            state,
            "ingest",
            time.perf_counter() - started,
            {
                "page_count": int(ingestion.get("page_count", 0)),
                "outline_count": len(ingestion.get("outline", [])),
            },
        ),
    }


def _select_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    selected = SELECTION_AGENT.run(
        ingestion_output=state["ingestion"],
        selection_mode=state["selection_mode"],
        selection=state.get("selection"),
    )
    selected_pages = selected.get("selected_page_indices", [])
    return {
        "selected": selected,
        "performance": _record_node_timing(
            state,
            "select",
            time.perf_counter() - started,
            {
                "selection_mode": state["selection_mode"],
                "selected_pages": len(selected_pages) if isinstance(selected_pages, list) else 0,
            },
        ),
    }


def _parse_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    attempt = int(state.get("parse_attempt", 0)) + 1
    selection_payload = state.get("rerun_selection") or state["selected"]
    parse_agent = _resolve_parse_agent(
        allow_mock=bool(state.get("allow_mock_parse", False)),
        engine_config=state.get("engine_config"),
    )
    try:
        parsed = parse_agent.run(
            engine=state["engine"],
            pdf_path=state["input"],
            selection=selection_payload,
            output_dir=state["output_dir"],
        )
        parse_meta = parsed.get("parse_meta", {}) if isinstance(parsed, dict) else {}
        parse_timings = parse_meta.get("timings", {}) if isinstance(parse_meta, dict) else {}
        return {
            "parsed": parsed,
            "parse_error": None,
            "parse_attempt": attempt,
            "performance": _record_node_timing(
                state,
                "parse",
                time.perf_counter() - started,
                {
                    "attempt": attempt,
                    "engine": state["engine"],
                    "rerun_active": bool(state.get("rerun_active", False)),
                    "cascade_active": bool(state.get("cascade_active", False)),
                    "profile": parse_meta.get("profile"),
                    "parse_meta_timings": parse_timings if isinstance(parse_timings, dict) else {},
                },
            ),
        }
    except Exception as exc:
        return {
            "parse_error": str(exc),
            "parse_attempt": attempt,
            "performance": _record_node_timing(
                state,
                "parse",
                time.perf_counter() - started,
                {
                    "attempt": attempt,
                    "engine": state["engine"],
                    "rerun_active": bool(state.get("rerun_active", False)),
                    "cascade_active": bool(state.get("cascade_active", False)),
                    "error": str(exc),
                },
            ),
        }


def _route_after_parse(
    state: PipelineState,
) -> Literal["parse", "build_ir", "mark_manual_review"]:
    if state.get("parse_error"):
        max_attempts = int(state.get("max_parse_attempts", 1))
        if int(state.get("parse_attempt", 0)) < max_attempts:
            return "parse"
        return "mark_manual_review"
    return "build_ir"


def _build_ir_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    new_ir = IR_BUILDER_AGENT.run(
        engine=state["engine"],
        raw_output=state["parsed"],
    )
    updated_page_indices = [int(page.page_index) for page in new_ir.pages]
    if bool(state.get("rerun_active")) and isinstance(state.get("document_ir"), DocumentIR):
        merged = _merge_document_ir(state["document_ir"], new_ir)
        return {
            "document_ir": merged,
            "updated_page_indices": updated_page_indices,
            "rerun_active": False,
            "rerun_selection": {},
            "parse_attempt": 0,
            "parse_error": None,
            "performance": _record_node_timing(
                state,
                "build_ir",
                time.perf_counter() - started,
                {
                    "merged": True,
                    "page_count": len(merged.pages),
                },
            ),
        }
    return {
        "document_ir": new_ir,
        "updated_page_indices": updated_page_indices,
        "parse_attempt": 0,
        "parse_error": None,
        "performance": _record_node_timing(
            state,
            "build_ir",
            time.perf_counter() - started,
            {
                "merged": False,
                "page_count": len(new_ir.pages),
            },
        ),
    }


def _enrich_visual_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    visual_stats = VISUAL_AGENT.capability_snapshot()
    return {
        "visual_agent": visual_stats,
        "performance": _record_node_timing(
            state,
            "enrich_visual",
            time.perf_counter() - started,
            {
                "enabled": bool(visual_stats.get("enabled", False)),
                "mode": "on_demand",
                "skipped": True,
            },
        ),
    }


def _validate_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    source_non_blank_pages = None
    ingestion = state.get("ingestion")
    if isinstance(ingestion, dict):
        raw_non_blank_pages = ingestion.get("non_blank_page_indices")
        if isinstance(raw_non_blank_pages, list):
            source_non_blank_pages = [int(page_index) for page_index in raw_non_blank_pages]
    try:
        validation = VALIDATION_AGENT.run(
            state["document_ir"],
            source_non_blank_pages=source_non_blank_pages,
        )
    except TypeError:
        validation = VALIDATION_AGENT.run(state["document_ir"])
    return {
        "validation": validation,
        "performance": _record_node_timing(
            state,
            "validate",
            time.perf_counter() - started,
            {
                "failed_pages": len(validation.failed_pages),
                "pass_quality_floor": bool(validation.pass_quality_floor),
            },
        ),
    }


def _route_after_validate(
    state: PipelineState,
) -> Literal["done", "prepare_rerun", "prepare_cascade", "mark_manual_review"]:
    report = state.get("validation")
    if not isinstance(report, ValidationReport):
        return "mark_manual_review"

    # Page-level failures take priority over the document-level pass flag so
    # fallback can repair localized problems such as empty tables.
    if report.failed_pages and int(state.get("rerun_attempt", 0)) < int(
        state.get("max_rerun_attempts", 1)
    ):
        return "prepare_rerun"
    cascade_enabled = bool(state.get("cascade_enabled", False))
    cascade_engine = str(state.get("cascade_engine") or "").strip()
    can_cascade = (
        bool(report.failed_pages)
        and cascade_enabled
        and cascade_engine
        and cascade_engine != str(state.get("engine", ""))
        and (not bool(state.get("cascade_active", False)))
        and int(state.get("cascade_attempt", 0)) < int(state.get("max_cascade_attempts", 1))
    )
    if can_cascade:
        return "prepare_cascade"
    if report.pass_quality_floor:
        return "done"
    return "mark_manual_review"


def _prepare_rerun_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    report = state.get("validation")
    if not isinstance(report, ValidationReport) or not report.failed_pages:
        return {
            "manual_review_required": True,
            "performance": _record_node_timing(
                state,
                "prepare_rerun",
                time.perf_counter() - started,
                {"failed_pages": 0, "manual_review_required": True},
            ),
        }

    rerun_selection = dict(state.get("selected", {}))
    rerun_selection["selected_page_indices"] = list(sorted(set(report.failed_pages)))
    return {
        "rerun_selection": rerun_selection,
        "rerun_active": True,
        "rerun_attempt": int(state.get("rerun_attempt", 0)) + 1,
        "parse_attempt": 0,
        "parse_error": None,
        "performance": _record_node_timing(
            state,
            "prepare_rerun",
            time.perf_counter() - started,
            {"failed_pages": len(report.failed_pages)},
        ),
    }


def _prepare_cascade_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    report = state.get("validation")
    cascade_engine = str(state.get("cascade_engine") or "").strip()
    if (
        not isinstance(report, ValidationReport)
        or not report.failed_pages
        or not cascade_engine
        or cascade_engine == str(state.get("engine", ""))
    ):
        return {
            "manual_review_required": True,
            "performance": _record_node_timing(
                state,
                "prepare_cascade",
                time.perf_counter() - started,
                {"failed_pages": 0, "manual_review_required": True},
            ),
        }

    rerun_selection = dict(state.get("selected", {}))
    rerun_selection["selected_page_indices"] = list(sorted(set(report.failed_pages)))
    update: PipelineState = {
        "engine": cascade_engine,
        "rerun_selection": rerun_selection,
        "rerun_active": True,
        "cascade_active": True,
        "cascade_attempt": int(state.get("cascade_attempt", 0)) + 1,
        "parse_attempt": 0,
        "parse_error": None,
    }
    cascade_engine_config = state.get("cascade_engine_config")
    if cascade_engine_config:
        update["engine_config"] = cascade_engine_config
    update["performance"] = _record_node_timing(
        state,
        "prepare_cascade",
        time.perf_counter() - started,
        {
            "failed_pages": len(report.failed_pages),
            "cascade_engine": cascade_engine,
        },
    )
    return update


def _mark_manual_review_node(state: PipelineState) -> PipelineState:
    started = time.perf_counter()
    return {
        "manual_review_required": True,
        "performance": _record_node_timing(
            state,
            "mark_manual_review",
            time.perf_counter() - started,
        ),
    }


def build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node("ingest", _ingest_node)
    graph.add_node("select", _select_node)
    graph.add_node("parse", _parse_node)
    graph.add_node("build_ir", _build_ir_node)
    graph.add_node("enrich_visual", _enrich_visual_node)
    graph.add_node("validate", _validate_node)
    graph.add_node("prepare_rerun", _prepare_rerun_node)
    graph.add_node("prepare_cascade", _prepare_cascade_node)
    graph.add_node("mark_manual_review", _mark_manual_review_node)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "select")
    graph.add_edge("select", "parse")
    graph.add_conditional_edges(
        "parse",
        _route_after_parse,
        {
            "parse": "parse",
            "build_ir": "build_ir",
            "mark_manual_review": "mark_manual_review",
        },
    )
    graph.add_edge("build_ir", "enrich_visual")
    graph.add_edge("enrich_visual", "validate")
    graph.add_conditional_edges(
        "validate",
        _route_after_validate,
        {
            "done": END,
            "prepare_rerun": "prepare_rerun",
            "prepare_cascade": "prepare_cascade",
            "mark_manual_review": "mark_manual_review",
        },
    )
    graph.add_edge("prepare_rerun", "parse")
    graph.add_edge("prepare_cascade", "parse")
    graph.add_edge("mark_manual_review", END)
    return graph.compile()


def _write_pipeline_outputs(
    *,
    output_dir: Path,
    document_ir: DocumentIR | None,
    validation: ValidationReport | None,
    summary: dict[str, Any],
    performance_profile: dict[str, Any] | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(document_ir, DocumentIR):
        (output_dir / "document_ir.json").write_text(
            document_ir.model_dump_json(indent=2),
            encoding="utf-8",
        )
        (output_dir / "document.md").write_text(
            document_ir_to_markdown(document_ir),
            encoding="utf-8",
        )
    if isinstance(validation, ValidationReport):
        (output_dir / "validation_report.json").write_text(
            validation.model_dump_json(indent=2),
            encoding="utf-8",
        )
    (output_dir / "pipeline_state.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    (output_dir / "performance_profile.json").write_text(
        json.dumps(performance_profile or {"nodes": {}}, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline entrypoint")
    parser.add_argument("--input", required=True)
    parser.add_argument("--engine", required=True, choices=["paddle", "mineru"])
    parser.add_argument("--selection-mode", default="all", choices=["all", "outline", "pagerange"])
    parser.add_argument("--selection", default=None)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--render-thumbnails", action="store_true")
    parser.add_argument("--allow-mock-parse", action="store_true")
    parser.add_argument("--engine-config", default=None)
    parser.add_argument("--max-parse-attempts", type=int, default=2)
    parser.add_argument("--max-rerun-attempts", type=int, default=1)
    parser.add_argument("--cascade-enabled", action="store_true")
    parser.add_argument("--cascade-engine", default=None, choices=["paddle", "mineru"])
    parser.add_argument("--cascade-engine-config", default=None)
    parser.add_argument("--max-cascade-attempts", type=int, default=1)
    args = parser.parse_args()

    cascade_engine = args.cascade_engine
    if args.cascade_enabled and cascade_engine is None:
        cascade_engine = "mineru" if args.engine == "paddle" else "paddle"

    app = build_graph()
    pipeline_started = time.perf_counter()
    result = app.invoke(
        {
            "input": args.input,
            "engine": args.engine,
            "selection_mode": args.selection_mode,
            "selection": args.selection,
            "output_dir": args.output_dir,
            "render_thumbnails": args.render_thumbnails,
            "allow_mock_parse": args.allow_mock_parse,
            "engine_config": args.engine_config,
            "max_parse_attempts": max(1, args.max_parse_attempts),
            "max_rerun_attempts": max(0, args.max_rerun_attempts),
            "cascade_enabled": bool(args.cascade_enabled),
            "cascade_engine": cascade_engine,
            "cascade_engine_config": args.cascade_engine_config,
            "max_cascade_attempts": max(0, args.max_cascade_attempts),
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
    pipeline_total_sec = round(time.perf_counter() - pipeline_started, 4)

    output_dir = Path(args.output_dir).resolve()

    document_ir = result.get("document_ir")
    validation = result.get("validation")

    summary = {
        "manual_review_required": bool(result.get("manual_review_required", False)),
        "parse_error": result.get("parse_error"),
        "parse_attempt": int(result.get("parse_attempt", 0)),
        "rerun_attempt": int(result.get("rerun_attempt", 0)),
        "cascade_attempt": int(result.get("cascade_attempt", 0)),
        "cascade_active": bool(result.get("cascade_active", False)),
        "engine": result.get("engine"),
        "visual_agent": result.get("visual_agent")
        if isinstance(result.get("visual_agent"), dict)
        else VISUAL_AGENT.capability_snapshot(),
    }
    performance_profile = result.get("performance") if isinstance(result.get("performance"), dict) else {"nodes": {}}
    performance_profile = dict(performance_profile)
    performance_profile["pipeline_total_sec"] = pipeline_total_sec
    performance_profile["requested_engine"] = args.engine
    performance_profile["final_engine"] = result.get("engine")
    performance_profile["selection_mode"] = args.selection_mode
    selected = result.get("selected")
    if isinstance(selected, dict):
        selected_pages = selected.get("selected_page_indices", [])
        performance_profile["selected_pages"] = (
            len(selected_pages) if isinstance(selected_pages, list) else None
        )
    _write_pipeline_outputs(
        output_dir=output_dir,
        document_ir=document_ir if isinstance(document_ir, DocumentIR) else None,
        validation=validation if isinstance(validation, ValidationReport) else None,
        summary=summary,
        performance_profile=performance_profile,
    )


if __name__ == "__main__":
    main()
