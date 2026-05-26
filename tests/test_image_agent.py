from backend.types import Block
from backend.types import DocumentIR
from backend.types import Page
from backend.image_agent import ImageAgent
from backend.image_agent import IMAGE_AGENT_KIND_MAP
from backend.image_agent import IMAGE_AGENT_KIND_WORKFLOW
from backend.image_agent import ImageInterpretationPayload
from backend.image_agent import page_has_image_content


class _FakePdfDocument:
    def __getitem__(self, page_index):  # noqa: ANN001
        return object()

    def close(self) -> None:
        return None


def test_page_has_image_content_detects_image_blocks() -> None:
    page = Page(
        page_index=0,
        blocks=[
            Block(id="b1", type="text", text="hello", page_index=0),
            Block(id="b2", type="figure", text="", page_index=0),
        ],
    )

    assert page_has_image_content(page) is True


def test_page_has_image_content_ignores_caption_only_blocks() -> None:
    page = Page(
        page_index=0,
        blocks=[
            Block(id="b1", type="figure_title", text="Figure 1. Process flow", page_index=0),
            Block(id="b2", type="image_caption", text="Factory site photo", page_index=0),
            Block(id="b3", type="text", text="The report discusses the image in prose.", page_index=0),
        ],
    )

    assert page_has_image_content(page) is False


def test_image_agent_enriches_image_pages(monkeypatch, tmp_path) -> None:
    agent = ImageAgent(api_key="test-key")
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-21T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(id="b1", type="figure", text="", page_index=0, order=0),
                    Block(id="b2", type="image_caption", text="Process flow", page_index=0, order=1),
                ],
            )
        ],
    )

    monkeypatch.setattr("backend.image_agent.pdfium.PdfDocument", lambda path: _FakePdfDocument())
    monkeypatch.setattr(agent, "_render_page_data_url", lambda pdf_doc, page_index: "data:image/jpeg;base64,abc")
    monkeypatch.setattr(
        agent,
        "_request_image_interpretation",
        lambda image_data_url, page: ImageInterpretationPayload(
            has_meaningful_image=True,
            summary="This diagram shows a linear review process.",
            key_elements=["Input", "Validation", "Approval"],
            relationships_or_flow=["Input flows to validation, then approval."],
            notes_or_uncertainty=[],
        ),
    )

    enriched, stats = agent.enrich_document(document, pdf_path=tmp_path / "demo.pdf")

    assert stats["enabled"] is True
    assert stats["image_pages_detected"] == 1
    assert stats["image_pages_enriched"] == 1
    image_blocks = [block for block in enriched.pages[0].blocks if block.type == "image_interpretation"]
    assert len(image_blocks) == 1
    assert "linear review process" in image_blocks[0].text


def test_image_agent_skips_enrichment_without_key(tmp_path) -> None:
    agent = ImageAgent(api_key="")
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-21T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[Block(id="b1", type="figure", text="", page_index=0, order=0)],
            )
        ],
    )

    enriched, stats = agent.enrich_document(document, pdf_path=tmp_path / "demo.pdf")

    assert stats["enabled"] is False
    assert stats["image_pages_detected"] == 1
    assert stats["image_pages_enriched"] == 0
    assert all(block.type != "image_interpretation" for block in enriched.pages[0].blocks)


def test_image_agent_does_not_generate_for_caption_only_page(monkeypatch, tmp_path) -> None:
    agent = ImageAgent(api_key="test-key")
    page = Page(
        page_index=0,
        blocks=[
            Block(id="b1", type="figure_title", text="Figure 1. Process flow", page_index=0, order=0),
            Block(id="b2", type="image_caption", text="Factory site photo", page_index=0, order=1),
        ],
    )

    def fail_if_rendered(path):  # noqa: ANN001
        raise AssertionError("caption-only pages must not be rendered for Image Agent")

    monkeypatch.setattr("backend.image_agent.pdfium.PdfDocument", fail_if_rendered)

    record, stats = agent.generate_page_record(page, pdf_path=tmp_path / "demo.pdf")

    assert stats["image_pages_detected"] == 0
    assert stats["image_pages_enriched"] == 0
    assert record["generated"] is False
    assert record["has_meaningful_image"] is False


def test_image_agent_generates_single_page_record(monkeypatch, tmp_path) -> None:
    agent = ImageAgent(api_key="test-key")
    page = Page(
        page_index=0,
        blocks=[
            Block(id="b1", type="figure", text="", page_index=0, order=0),
            Block(id="b2", type="image_caption", text="Process flow", page_index=0, order=1),
        ],
    )

    monkeypatch.setattr("backend.image_agent.pdfium.PdfDocument", lambda path: _FakePdfDocument())
    monkeypatch.setattr(agent, "_render_page_data_url", lambda pdf_doc, page_index: "data:image/jpeg;base64,abc")
    monkeypatch.setattr(
        agent,
        "_request_image_interpretation",
        lambda image_data_url, current_page: ImageInterpretationPayload(
            has_meaningful_image=True,
            summary="This diagram shows a linear review process.",
            key_elements=["Input", "Validation", "Approval"],
            relationships_or_flow=["Input flows to validation, then approval."],
            notes_or_uncertainty=[],
        ),
    )

    record, stats = agent.generate_page_record(page, pdf_path=tmp_path / "demo.pdf")

    assert stats["image_pages_detected"] == 1
    assert stats["image_pages_enriched"] == 1
    assert record["generated"] is True
    assert record["has_meaningful_image"] is True
    assert "linear review process" in str(record["summary"])
    assert "Key elements:" in str(record["markdown"])


def test_image_agent_generates_empty_record_when_nothing_meaningful(monkeypatch, tmp_path) -> None:
    agent = ImageAgent(api_key="test-key")
    page = Page(
        page_index=0,
        blocks=[Block(id="b1", type="figure", text="", page_index=0, order=0)],
    )

    monkeypatch.setattr("backend.image_agent.pdfium.PdfDocument", lambda path: _FakePdfDocument())
    monkeypatch.setattr(agent, "_render_page_data_url", lambda pdf_doc, page_index: "data:image/jpeg;base64,abc")
    monkeypatch.setattr(
        agent,
        "_request_image_interpretation",
        lambda image_data_url, current_page: ImageInterpretationPayload(
            has_meaningful_image=False,
            summary="",
            key_elements=[],
            relationships_or_flow=[],
            notes_or_uncertainty=[],
        ),
    )

    record, stats = agent.generate_page_record(page, pdf_path=tmp_path / "demo.pdf")

    assert stats["image_pages_detected"] == 1
    assert stats["image_pages_enriched"] == 0
    assert record["generated"] is True
    assert record["has_meaningful_image"] is False
    assert record["summary"] is None
    assert record["markdown"] is None


def test_image_agent_infers_chinese_map_context() -> None:
    agent = ImageAgent(api_key="test-key")
    page = Page(
        page_index=6,
        blocks=[
            Block(id="b1", type="figure_title", text="图1-1 项目所在地区红线图", page_index=6, order=0),
            Block(
                id="b2",
                type="text",
                text="项目所在区域与生态保护红线、一般生态空间的关系见下图。",
                page_index=6,
                order=1,
            ),
        ],
    )

    assert agent._infer_output_language(page) == "zh"
    assert agent._infer_image_kind(page) == "map"

    prompt = agent._build_prompt(page)

    assert "Page language: Simplified Chinese." in prompt
    assert "entirely in Simplified Chinese" in prompt
    assert "Likely image type: map." in prompt
    assert "Output format rules are strict:" in prompt
    assert "summary: exactly 1 short sentence." in prompt
    assert "Empty arrays are preferred over repetitive filler." in prompt
    assert "Photos, site photos, factory photos" in prompt
    assert "Return has_meaningful_image=false only for blank" in prompt


def test_image_agent_prefers_page_language_over_english_filename() -> None:
    agent = ImageAgent(api_key="test-key")
    page = Page(
        page_index=0,
        blocks=[
            Block(
                id="b1",
                type="text",
                text="本项目总平面布置图展示了厂区道路、仓库、办公区和生产区域的位置关系。",
                page_index=0,
                order=0,
            )
        ],
    )

    assert agent._infer_output_language(page, source_name="Permit_1912.2003.pdf") == "zh"


def test_image_agent_retries_empty_reading_for_image_pages() -> None:
    agent = ImageAgent(api_key="test-key")
    page = Page(
        page_index=0,
        blocks=[
            Block(id="b1", type="figure", text="", page_index=0, order=0),
            Block(id="b2", type="image_caption", text="Factory site photo", page_index=0, order=1),
        ],
    )

    assert agent._should_retry_empty_image_reading(page) is True


def test_image_agent_detects_mixed_language_payload() -> None:
    agent = ImageAgent(api_key="test-key")
    mixed_payload = ImageInterpretationPayload(
        has_meaningful_image=True,
        summary="This map shows the project layout and warehouse areas.",
        key_elements=["钽铌精矿仓库", "14#原矿仓库", "行政及人员生活区"],
        relationships_or_flow=["仓库位于厂区北侧。"],
        notes_or_uncertainty=[],
    )
    chinese_payload = ImageInterpretationPayload(
        has_meaningful_image=True,
        summary="该图展示了项目厂区总平面布置及仓库、办公区的位置关系。",
        key_elements=["钽铌精矿仓库", "14#原矿仓库", "行政及人员生活区"],
        relationships_or_flow=["仓库位于厂区北侧。"],
        notes_or_uncertainty=[],
    )

    assert agent._payload_matches_language(mixed_payload, "zh") is False
    assert agent._payload_matches_language(chinese_payload, "zh") is True


def test_image_agent_formats_markdown_with_localized_headings() -> None:
    agent = ImageAgent(api_key="test-key")
    payload = ImageInterpretationPayload(
        has_meaningful_image=True,
        summary="该图主要说明项目位置与周边生态管控区域的空间关系。",
        key_elements=["新余市渝水区", "生态保护红线"],
        relationships_or_flow=["项目位置位于生态空间分区图的标注位置。"],
        notes_or_uncertainty=["未从图中直接读出精确距离。"],
    )

    markdown = agent._format_interpretation_markdown(
        payload,
        language="zh",
        image_kind=IMAGE_AGENT_KIND_WORKFLOW,
    )

    assert "关键内容：" in markdown
    assert "关系/流程：" in markdown
    assert "说明：" in markdown


def test_image_agent_inserts_interpretation_after_image_block_not_page_end() -> None:
    agent = ImageAgent(api_key="test-key")
    page = Page(
        page_index=4,
        blocks=[
            Block(id="b1", type="table", text="<table><tr><td>top</td></tr></table>", page_index=4, order=0),
            Block(id="b2", type="text", text="source note", page_index=4, order=1),
            Block(id="b3", type="image", text="", page_index=4, order=2),
            Block(id="b4", type="table", text="<table><tr><td>bottom</td></tr></table>", page_index=4, order=3),
        ],
    )

    payload = ImageInterpretationPayload(
        has_meaningful_image=True,
        summary="This chart shows a trend.",
        key_elements=[],
        relationships_or_flow=[],
        notes_or_uncertainty=[],
    )

    updated = agent._append_image_agent_block(page, payload)
    ordered_types = [block.type for block in sorted(updated.blocks, key=lambda block: block.order or 0)]

    assert ordered_types == ["table", "text", "image", "image_interpretation", "table"]


def test_image_agent_formatter_drops_redundant_sections() -> None:
    agent = ImageAgent(api_key="test-key")
    payload = ImageInterpretationPayload(
        has_meaningful_image=True,
        summary="The map shows the project site within the regulated zone.",
        key_elements=["project site", "regulated zone", "project site"],
        relationships_or_flow=[
            "The map shows the project site within the regulated zone.",
            "The project site sits inside the regulated zone.",
            "A third redundant relation.",
        ],
        notes_or_uncertainty=["No exact distance is readable.", "No exact distance is readable."],
    )

    markdown = agent._format_interpretation_markdown(
        payload,
        language="en",
        image_kind=IMAGE_AGENT_KIND_MAP,
    )

    assert markdown.count("The map shows the project site within the regulated zone.") == 1
    assert "A third redundant relation." not in markdown
    assert markdown.count("No exact distance is readable.") == 1
