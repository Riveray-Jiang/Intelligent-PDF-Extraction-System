from backend.types import Block, DocumentIR, Page, ValidationReport


def test_types_constructable() -> None:
    block = Block(id="p0_b0", type="text", text="hello", page_index=0)
    page = Page(page_index=0, blocks=[block])
    doc = DocumentIR(
        doc_id="doc",
        source_file="demo.pdf",
        source_engine="paddle",
        generated_at="2026-02-24T00:00:00Z",
        pages=[page],
    )
    assert doc.pages[0].blocks[0].text == "hello"


def test_validation_report_constructable() -> None:
    report = ValidationReport(
        empty_page_rate=0.01,
        order_anomaly_rate=0.02,
        table_anomaly_rate=0.03,
        coverage_rate=0.99,
        non_blank_pages=100,
        pages_with_content=99,
        empty_pages=1,
        anomalous_order_pages=2,
        total_tables=10,
        anomalous_tables=1,
        failed_pages=[],
        pass_quality_floor=True,
    )
    assert report.pass_quality_floor is True