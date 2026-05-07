from backend.types import Block
from backend.types import DocumentIR
from backend.types import Page
from backend.validation_agent import ValidationAgent


def test_validation_agent_metrics() -> None:
    page0 = Page(
        page_index=0,
        blocks=[
            Block(id="p0_b0", type="text", text="hello", order=0, page_index=0),
            Block(
                id="p0_t0",
                type="table",
                text="<table><tr><td>a</td></tr></table>",
                order=1,
                source={"raw": {"table_body": "<table><tr><td>a</td></tr></table>"}},
                page_index=0,
            ),
        ],
    )
    page1 = Page(page_index=1, blocks=[])
    page2 = Page(
        page_index=2,
        blocks=[
            Block(id="p2_b0", type="text", text="a", order=None, page_index=2),
            Block(id="p2_b1", type="text", text="b", order=1, page_index=2),
        ],
    )
    page3 = Page(
        page_index=3,
        blocks=[
            Block(
                id="p3_t0",
                type="table",
                text="",
                order=0,
                source={"raw": {"table_body": "<table></table>"}},
                page_index=3,
            )
        ],
    )

    doc = DocumentIR(
        doc_id="doc",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-02-24T00:00:00Z",
        pages=[page0, page1, page2, page3],
    )

    report = ValidationAgent().run(doc)
    assert report.non_blank_pages == 4
    assert report.pages_with_content == 3
    assert report.empty_pages == 1
    assert report.anomalous_order_pages == 1
    assert report.total_tables == 2
    assert report.anomalous_tables == 1
    assert report.coverage_rate == 0.75
    assert report.failed_pages == [1, 2, 3]
    assert report.pass_quality_floor is False


def test_validation_agent_loads_runtime_thresholds(tmp_path) -> None:
    cfg = tmp_path / "quality_floor.yaml"
    cfg.write_text(
        "\n".join(
            [
                "metrics:",
                "  empty_page_rate:",
                "    runtime_max: 1.0",
                "  order_anomaly_rate:",
                "    runtime_max: 1.0",
                "  table_anomaly_rate:",
                "    runtime_max: 1.0",
                "  coverage_rate:",
                "    runtime_min: 0.0",
                "algorithms:",
                "  order_inversion_anomaly_threshold: 1.0",
                "  order_missing_ratio_threshold: 1.0",
                "  table_cols_max: 999",
                "  table_rows_max: 9999",
                "  table_empty_row_ratio_max: 1.0",
            ]
        ),
        encoding="utf-8",
    )

    doc = DocumentIR(
        doc_id="doc",
        source_file="demo.pdf",
        source_engine="paddle",
        generated_at="2026-02-24T00:00:00Z",
        pages=[Page(page_index=0, blocks=[])],
    )
    report = ValidationAgent(config_path=cfg).run(doc)
    assert report.pass_quality_floor is True


def test_validation_agent_ignores_optional_order_block_types() -> None:
    page = Page(
        page_index=0,
        blocks=[
            Block(id="b0", type="header", text="Document Title", order=None, page_index=0),
            Block(id="b1", type="number", text="1", order=None, page_index=0),
            Block(id="b2", type="paragraph_title", text="目录", order=1, page_index=0),
            Block(id="b3", type="content", text="1 概述", order=2, page_index=0),
        ],
    )

    doc = DocumentIR(
        doc_id="doc",
        source_file="demo.pdf",
        source_engine="paddle",
        generated_at="2026-02-24T00:00:00Z",
        pages=[page],
    )

    report = ValidationAgent().run(doc)
    assert report.anomalous_order_pages == 0
    assert report.order_anomaly_rate == 0.0
