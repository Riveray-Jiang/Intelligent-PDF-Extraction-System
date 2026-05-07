import pytest

from backend import pipeline_graph
from backend.types import Block
from backend.types import DocumentIR
from backend.types import Page
from backend.types import ValidationReport


class _FakeIngestionAgent:
    def run(self, pdf_path, output_dir, render_thumbnails):  # noqa: ANN001
        return {"pdf_path": pdf_path, "page_count": 1, "outline": []}


class _FakeSelectionAgent:
    def run(self, ingestion_output, selection_mode, selection=None):  # noqa: ANN001
        return {"selected_page_indices": [0], "selected_count": 1}


class _FakeParseAgent:
    def run(self, engine, pdf_path, selection, output_dir):  # noqa: ANN001
        return {
            "doc_id": "demo",
            "source_file": pdf_path,
            "pages": [{"page_index": 0, "blocks": []}],
        }


class _FakeIRBuilderAgent:
    def run(self, engine, raw_output):  # noqa: ANN001
        return DocumentIR(
            doc_id="demo",
            source_file=raw_output["source_file"],
            source_engine=engine,
            generated_at="2026-02-24T00:00:00Z",
            pages=[Page(page_index=0, blocks=[])],
        )


class _FakeValidationAgent:
    def run(self, document_ir):  # noqa: ANN001
        return ValidationReport(
            empty_page_rate=1.0,
            order_anomaly_rate=0.0,
            table_anomaly_rate=0.0,
            coverage_rate=0.0,
            non_blank_pages=1,
            pages_with_content=0,
            empty_pages=1,
            anomalous_order_pages=0,
            total_tables=0,
            anomalous_tables=0,
            failed_pages=[0],
            pass_quality_floor=False,
        )


class _FakeVisualAgent:
    def capability_snapshot(self):  # noqa: ANN001
        return {
            "enabled": True,
            "name": "Visual Agent",
            "model": "gpt-4o",
            "visual_pages_detected": 0,
            "visual_pages_enriched": 0,
            "visual_pages_failed": 0,
        }

    def enrich_document(self, document_ir, pdf_path, page_indices=None):  # noqa: ANN001
        stats = self.capability_snapshot()
        stats["visual_pages_detected"] = len(page_indices or [])
        stats["visual_pages_enriched"] = len(page_indices or [])
        return document_ir, stats


@pytest.fixture(autouse=True)
def _stub_visual_agent(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(pipeline_graph, "VISUAL_AGENT", _FakeVisualAgent())


class _TrackingParseAgent:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, engine, pdf_path, selection, output_dir):  # noqa: ANN001
        selected = selection.get("selected_page_indices") if isinstance(selection, dict) else None
        if not selected:
            selected = [0, 1]
        selected_indices = [int(i) for i in selected]
        self.calls.append({"engine": engine, "selected_page_indices": selected_indices})
        pages = []
        for page_index in selected_indices:
            pages.append(
                {
                    "page_index": page_index,
                    "blocks": [
                        {
                            "id": f"{engine}_{page_index}",
                            "type": "text",
                            "text": f"{engine}:{page_index}",
                            "page_index": page_index,
                            "order": page_index,
                        }
                    ],
                }
            )
        return {
            "doc_id": "demo",
            "source_file": pdf_path,
            "pages": pages,
        }


class _TrackingIRBuilderAgent:
    def __init__(self) -> None:
        self._tick = 0

    def run(self, engine, raw_output):  # noqa: ANN001
        self._tick += 1
        pages = []
        for page_data in raw_output["pages"]:
            page_index = int(page_data["page_index"])
            blocks = []
            for block_data in page_data.get("blocks", []):
                blocks.append(
                    Block(
                        id=str(block_data["id"]),
                        type=str(block_data["type"]),
                        text=str(block_data["text"]),
                        bbox=None,
                        order=block_data.get("order"),
                        confidence=None,
                        source={},
                        page_index=page_index,
                    )
                )
            pages.append(Page(page_index=page_index, blocks=blocks))
        return DocumentIR(
            doc_id="demo",
            source_file=raw_output["source_file"],
            source_engine=engine,
            generated_at=f"2026-02-24T00:00:0{self._tick}Z",
            pages=pages,
        )


class _SequenceValidationAgent:
    def __init__(self, reports):  # noqa: ANN001
        self._reports = list(reports)
        self._idx = 0

    def run(self, document_ir):  # noqa: ANN001
        pos = min(self._idx, len(self._reports) - 1)
        self._idx += 1
        return self._reports[pos]


def _validation_report(pass_quality_floor: bool, failed_pages: list[int]) -> ValidationReport:
    return ValidationReport(
        empty_page_rate=0.0 if pass_quality_floor else 0.2,
        order_anomaly_rate=0.0,
        table_anomaly_rate=0.0 if pass_quality_floor else 0.2,
        coverage_rate=1.0 if pass_quality_floor else 0.8,
        non_blank_pages=2,
        pages_with_content=2,
        empty_pages=0,
        anomalous_order_pages=0,
        total_tables=1,
        anomalous_tables=0 if pass_quality_floor else 1,
        failed_pages=failed_pages,
        pass_quality_floor=pass_quality_floor,
    )


def test_pipeline_graph_invocation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pipeline_graph, "INGESTION_AGENT", _FakeIngestionAgent())
    monkeypatch.setattr(pipeline_graph, "SELECTION_AGENT", _FakeSelectionAgent())
    monkeypatch.setattr(pipeline_graph, "PARSE_AGENT", _FakeParseAgent())
    monkeypatch.setattr(pipeline_graph, "IR_BUILDER_AGENT", _FakeIRBuilderAgent())
    monkeypatch.setattr(pipeline_graph, "VALIDATION_AGENT", _FakeValidationAgent())

    app = pipeline_graph.build_graph()
    result = app.invoke(
        {
            "input": "demo.pdf",
            "engine": "paddle",
            "selection_mode": "all",
            "selection": None,
            "output_dir": str(tmp_path),
            "render_thumbnails": False,
        }
    )

    assert isinstance(result["document_ir"], DocumentIR)
    assert isinstance(result["validation"], ValidationReport)
    assert result["visual_agent"]["enabled"] is True


def test_pipeline_graph_cascade_triggers_and_merges_failed_pages(monkeypatch, tmp_path) -> None:
    parse_agent = _TrackingParseAgent()
    ir_builder = _TrackingIRBuilderAgent()
    validation_agent = _SequenceValidationAgent(
        [
            _validation_report(False, [1]),
            _validation_report(True, []),
        ]
    )
    monkeypatch.setattr(pipeline_graph, "INGESTION_AGENT", _FakeIngestionAgent())
    monkeypatch.setattr(pipeline_graph, "SELECTION_AGENT", _FakeSelectionAgent())
    monkeypatch.setattr(pipeline_graph, "PARSE_AGENT", parse_agent)
    monkeypatch.setattr(pipeline_graph, "IR_BUILDER_AGENT", ir_builder)
    monkeypatch.setattr(pipeline_graph, "VALIDATION_AGENT", validation_agent)

    app = pipeline_graph.build_graph()
    result = app.invoke(
        {
            "input": "demo.pdf",
            "engine": "mineru",
            "selection_mode": "all",
            "selection": None,
            "output_dir": str(tmp_path),
            "render_thumbnails": False,
            "max_rerun_attempts": 0,
            "cascade_enabled": True,
            "cascade_engine": "paddle",
            "max_cascade_attempts": 1,
            "parse_attempt": 0,
            "rerun_attempt": 0,
            "cascade_attempt": 0,
            "rerun_active": False,
            "cascade_active": False,
            "manual_review_required": False,
            "parse_error": None,
        }
    )

    assert [c["engine"] for c in parse_agent.calls] == ["mineru", "paddle"]
    assert parse_agent.calls[1]["selected_page_indices"] == [1]
    assert bool(result.get("manual_review_required", False)) is False
    assert int(result.get("cascade_attempt", 0)) == 1

    document_ir = result["document_ir"]
    page_map = {p.page_index: p for p in document_ir.pages}
    assert page_map[0].blocks[0].text == "mineru:0"
    assert page_map[1].blocks[0].text == "paddle:1"


def test_pipeline_graph_cascade_triggers_when_quality_floor_passes_but_failed_pages_exist(
    monkeypatch, tmp_path
) -> None:
    parse_agent = _TrackingParseAgent()
    ir_builder = _TrackingIRBuilderAgent()
    validation_agent = _SequenceValidationAgent(
        [
            _validation_report(True, [1]),
            _validation_report(True, []),
        ]
    )
    monkeypatch.setattr(pipeline_graph, "INGESTION_AGENT", _FakeIngestionAgent())
    monkeypatch.setattr(pipeline_graph, "SELECTION_AGENT", _FakeSelectionAgent())
    monkeypatch.setattr(pipeline_graph, "PARSE_AGENT", parse_agent)
    monkeypatch.setattr(pipeline_graph, "IR_BUILDER_AGENT", ir_builder)
    monkeypatch.setattr(pipeline_graph, "VALIDATION_AGENT", validation_agent)

    app = pipeline_graph.build_graph()
    result = app.invoke(
        {
            "input": "demo.pdf",
            "engine": "mineru",
            "selection_mode": "all",
            "selection": None,
            "output_dir": str(tmp_path),
            "render_thumbnails": False,
            "max_rerun_attempts": 0,
            "cascade_enabled": True,
            "cascade_engine": "paddle",
            "max_cascade_attempts": 1,
            "parse_attempt": 0,
            "rerun_attempt": 0,
            "cascade_attempt": 0,
            "rerun_active": False,
            "cascade_active": False,
            "manual_review_required": False,
            "parse_error": None,
        }
    )

    assert [c["engine"] for c in parse_agent.calls] == ["mineru", "paddle"]
    assert parse_agent.calls[1]["selected_page_indices"] == [1]
    assert bool(result.get("manual_review_required", False)) is False
    assert int(result.get("cascade_attempt", 0)) == 1


def test_pipeline_graph_cascade_not_triggered_when_disabled(monkeypatch, tmp_path) -> None:
    parse_agent = _TrackingParseAgent()
    ir_builder = _TrackingIRBuilderAgent()
    validation_agent = _SequenceValidationAgent([_validation_report(False, [1])])
    monkeypatch.setattr(pipeline_graph, "INGESTION_AGENT", _FakeIngestionAgent())
    monkeypatch.setattr(pipeline_graph, "SELECTION_AGENT", _FakeSelectionAgent())
    monkeypatch.setattr(pipeline_graph, "PARSE_AGENT", parse_agent)
    monkeypatch.setattr(pipeline_graph, "IR_BUILDER_AGENT", ir_builder)
    monkeypatch.setattr(pipeline_graph, "VALIDATION_AGENT", validation_agent)

    app = pipeline_graph.build_graph()
    result = app.invoke(
        {
            "input": "demo.pdf",
            "engine": "mineru",
            "selection_mode": "all",
            "selection": None,
            "output_dir": str(tmp_path),
            "render_thumbnails": False,
            "max_rerun_attempts": 0,
            "cascade_enabled": False,
            "cascade_engine": "paddle",
            "max_cascade_attempts": 1,
            "parse_attempt": 0,
            "rerun_attempt": 0,
            "cascade_attempt": 0,
            "rerun_active": False,
            "cascade_active": False,
            "manual_review_required": False,
            "parse_error": None,
        }
    )

    assert [c["engine"] for c in parse_agent.calls] == ["mineru"]
    assert bool(result.get("manual_review_required", False)) is True
    assert int(result.get("cascade_attempt", 0)) == 0


def test_pipeline_graph_cascade_prevents_infinite_loop(monkeypatch, tmp_path) -> None:
    parse_agent = _TrackingParseAgent()
    ir_builder = _TrackingIRBuilderAgent()
    validation_agent = _SequenceValidationAgent([_validation_report(False, [1])])
    monkeypatch.setattr(pipeline_graph, "INGESTION_AGENT", _FakeIngestionAgent())
    monkeypatch.setattr(pipeline_graph, "SELECTION_AGENT", _FakeSelectionAgent())
    monkeypatch.setattr(pipeline_graph, "PARSE_AGENT", parse_agent)
    monkeypatch.setattr(pipeline_graph, "IR_BUILDER_AGENT", ir_builder)
    monkeypatch.setattr(pipeline_graph, "VALIDATION_AGENT", validation_agent)

    app = pipeline_graph.build_graph()
    result = app.invoke(
        {
            "input": "demo.pdf",
            "engine": "mineru",
            "selection_mode": "all",
            "selection": None,
            "output_dir": str(tmp_path),
            "render_thumbnails": False,
            "max_rerun_attempts": 0,
            "cascade_enabled": True,
            "cascade_engine": "paddle",
            "max_cascade_attempts": 1,
            "parse_attempt": 0,
            "rerun_attempt": 0,
            "cascade_attempt": 0,
            "rerun_active": False,
            "cascade_active": False,
            "manual_review_required": False,
            "parse_error": None,
        }
    )

    assert [c["engine"] for c in parse_agent.calls] == ["mineru", "paddle"]
    assert int(result.get("cascade_attempt", 0)) == 1
    assert bool(result.get("manual_review_required", False)) is True


def test_write_pipeline_outputs_includes_markdown(tmp_path) -> None:
    document_ir = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T00:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(
                        id="p0_b0",
                        type="text",
                        text="hello markdown",
                        bbox=None,
                        order=0,
                        confidence=None,
                        source={},
                        page_index=0,
                    )
                ],
            )
        ],
    )
    validation = ValidationReport(
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

    pipeline_graph._write_pipeline_outputs(
        output_dir=tmp_path,
        document_ir=document_ir,
        validation=validation,
        summary={"engine": "mineru"},
    )

    assert (tmp_path / "document_ir.json").exists()
    assert (tmp_path / "validation_report.json").exists()
    assert (tmp_path / "pipeline_state.json").exists()
    markdown_path = tmp_path / "document.md"
    assert markdown_path.exists()
    assert "## Page 1" in markdown_path.read_text(encoding="utf-8")
