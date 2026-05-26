from backend.markdown_export import document_ir_to_markdown
from backend.markdown_export import page_to_preview_markdown
from backend.types import Block
from backend.types import DocumentIR
from backend.types import Page


def test_document_ir_to_markdown_renders_page_sections_and_tables() -> None:
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(id="b1", type="paragraph_title", text="Overview", page_index=0, order=0),
                    Block(id="b2", type="text", text="First paragraph.", page_index=0, order=1),
                    Block(id="b3", type="table", text="<table><tr><td>A</td></tr></table>", page_index=0, order=2),
                    Block(
                        id="b4",
                        type="image_interpretation",
                        text="This flowchart shows a three-step approval process.",
                        page_index=0,
                        order=3,
                    ),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "# demo" in markdown
    assert "## Page 1" in markdown
    assert "### Overview" in markdown
    assert "First paragraph." in markdown
    assert "<table><tr><td>A</td></tr></table>" in markdown
    assert "> **Image Agent interpretation**" in markdown
    assert "three-step approval process" in markdown


def test_document_ir_to_markdown_localizes_image_agent_labels_for_chinese_pages() -> None:
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(
                        id="b1",
                        type="image_interpretation",
                        text="该图主要说明项目位置与周边生态保护区域的空间关系。",
                        page_index=0,
                        order=0,
                        source={"agent": "image-agent", "language": "zh"},
                    ),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "> **Image Agent 解读**" in markdown
    assert "空间关系" in markdown


def test_document_ir_to_markdown_preserves_math_blocks() -> None:
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(
                        id="b1",
                        type="equation",
                        text="$$\nS + O_{2} = SO_{2}\n$$",
                        page_index=0,
                        order=0,
                    ),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "```text" not in markdown
    assert "$$\nS + O_{2} = SO_{2}\n$$" in markdown


def test_document_ir_to_markdown_promotes_numbered_text_headings() -> None:
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(id="b1", type="text", text="5. Conclusion", page_index=0, order=0),
                    Block(id="b2", type="text", text="Body paragraph.", page_index=0, order=1),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "### 5. Conclusion" in markdown
    assert "Body paragraph." in markdown


def test_document_ir_to_markdown_promotes_numbered_headings_without_space() -> None:
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(id="b1", type="text", text="3Records", page_index=0, order=0),
                    Block(id="b2", type="text", text="Body paragraph.", page_index=0, order=1),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "### 3 Records" in markdown
    assert "Body paragraph." in markdown


def test_document_ir_to_markdown_respects_ir_heading_levels() -> None:
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(
                        id="b1",
                        type="text",
                        text="4.5. Lithium production impacts on batteries",
                        page_index=0,
                        order=0,
                        semantic_type="heading",
                        heading_level=4,
                    ),
                    Block(id="b2", type="text", text="Body paragraph.", page_index=0, order=1),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "#### 4.5. Lithium production impacts on batteries" in markdown
    assert "Body paragraph." in markdown


def test_document_ir_to_markdown_promotes_plain_conclusion_heading() -> None:
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(id="b1", type="text", text="Conclusion", page_index=0, order=0),
                    Block(id="b2", type="text", text="Body paragraph.", page_index=0, order=1),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "### Conclusion" in markdown


def test_document_ir_to_markdown_preserves_definition_like_line_breaks() -> None:
    definition_text = (
        "式中：Lp（r）——预测点（r）处，第 i 倍频带声压级，dB；LA（r）——距声源 r 处的 A 声级，dB（A）；"
        "Adiv——几何发散引起的衰减，dB；\n"
        "Aatm——大气吸收引起的衰减，dB。"
    )
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(id="b1", type="text", text=definition_text, page_index=0, order=0),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "式中：" in markdown
    assert "式中：  \nLp（r）——预测点（r）处，第 i 倍频带声压级，dB；" in markdown
    assert "Lp（r）——预测点（r）处，第 i 倍频带声压级，dB；  \nLA（r）——距声源 r 处的 A 声级，dB（A）；" in markdown
    assert "Adiv——几何发散引起的衰减，dB；  \nAatm——大气吸收引起的衰减，dB。" in markdown


def test_document_ir_to_markdown_keeps_regular_prose_as_paragraphs() -> None:
    prose_text = (
        "Sedimentary rock deposits have gained significant attention in recent years\n"
        "as a new type of lithium deposit because they are abundant in the western USA\n"
        "and processing them presents several practical challenges."
    )
    document = DocumentIR(
        doc_id="demo",
        source_file="demo.pdf",
        source_engine="mineru",
        generated_at="2026-04-02T12:00:00Z",
        pages=[
            Page(
                page_index=0,
                blocks=[
                    Block(id="b1", type="text", text=prose_text, page_index=0, order=0),
                ],
            )
        ],
    )

    markdown = document_ir_to_markdown(document)

    assert "significant attention in recent years\nas a new type of lithium deposit" in markdown
    assert "significant attention in recent years  \nas a new type of lithium deposit" not in markdown


def test_page_preview_markdown_hides_image_agent_block() -> None:
    page = Page(
        page_index=0,
        blocks=[
            Block(id="b1", type="figure", text="", page_index=0, order=0, source={"raw": {"image_caption": ["Fig. 1"]}}),
            Block(
                id="b2",
                type="image_interpretation",
                text="**Image Agent interpretation**\n\nThis figure highlights the process flow.",
                page_index=0,
                order=1,
            ),
            Block(id="b3", type="text", text="Body paragraph.", page_index=0, order=2),
        ],
    )

    markdown = page_to_preview_markdown(page)

    assert "Image Agent interpretation" not in markdown
    assert "Body paragraph." in markdown


def test_page_preview_markdown_uses_local_image_fallback_text_before_placeholder() -> None:
    page = Page(
        page_index=0,
        blocks=[
            Block(
                id="b1",
                type="image",
                text="",
                page_index=0,
                order=0,
                source={"fallback_extracted_markdown": "Recovered text from stamped region"},
            ),
        ],
    )

    markdown = page_to_preview_markdown(page)

    assert "Recovered text from stamped region" in markdown
    assert "[Image content present]" not in markdown


def test_page_preview_markdown_keeps_empty_table_shell_visible() -> None:
    page = Page(
        page_index=0,
        width=1000,
        height=1400,
        blocks=[
            Block(
                id="b1",
                type="table",
                text="",
                bbox=[100, 20, 900, 240],
                page_index=0,
                order=0,
                source={"raw": {"bbox": [100, 20, 900, 240]}},
            ),
        ],
    )

    markdown = page_to_preview_markdown(page)

    assert "[Table detected]" in markdown
