from backend.ir_builder_agent import IRBuilderAgent


def test_ir_builder_from_paddle() -> None:
    raw = {
        "doc_id": "doc_paddle",
        "source_file": "demo.pdf",
        "pages": [
            {
                "page_index": 0,
                "blocks": [
                    {
                        "id": "0",
                        "type": "text",
                        "text": "hello",
                        "bbox": [1, 2, 3, 4],
                        "order": 0,
                        "confidence": 0.99,
                    }
                ],
            }
        ],
    }

    doc = IRBuilderAgent().run("paddle", raw)
    assert doc.source_engine == "paddle"
    assert doc.pages[0].blocks[0].text == "hello"
    assert doc.pages[0].blocks[0].confidence == 0.99
    assert doc.pages[0].blocks[0].heading_level is None


def test_ir_builder_from_mineru_with_selected_pages() -> None:
    raw = {
        "doc_id": "doc_mineru",
        "source_file": "demo.pdf",
        "selected_page_indices": [0, 1, 2],
        "mineru_content_list": [
            {"page_idx": 0, "type": "text", "content": "A", "bbox": [0, 0, 10, 10]},
            {
                "page_idx": 2,
                "type": "table",
                "table_body": "<table><tr><td>x</td></tr></table>",
                "bbox": [0, 0, 20, 20],
            },
        ],
    }

    doc = IRBuilderAgent().run("mineru", raw)
    assert doc.source_engine == "mineru"
    assert [page.page_index for page in doc.pages] == [0, 1, 2]
    assert doc.pages[1].blocks == []


def test_ir_builder_promotes_raw_text_level_to_heading() -> None:
    raw = {
        "doc_id": "doc_paddle",
        "source_file": "demo.pdf",
        "pages": [
            {
                "page_index": 0,
                "blocks": [
                    {
                        "id": "0",
                        "type": "text",
                        "text": "5. Conclusion",
                        "text_level": 1,
                        "bbox": [1, 2, 3, 4],
                        "order": 0,
                    }
                ],
            }
        ],
    }

    doc = IRBuilderAgent().run("paddle", raw)
    block = doc.pages[0].blocks[0]

    assert block.semantic_type == "heading"
    assert block.heading_level == 3
    assert block.source["heading_level_inferred"] == 3


def test_ir_builder_infers_nested_heading_level_from_numbering() -> None:
    raw = {
        "doc_id": "doc_mineru",
        "source_file": "demo.pdf",
        "mineru_content_list": [
            {
                "page_idx": 0,
                "type": "text",
                "content": "4.5. Lithium production impacts on batteries",
                "bbox": [0, 0, 10, 10],
            }
        ],
    }

    doc = IRBuilderAgent().run("mineru", raw)
    block = doc.pages[0].blocks[0]

    assert block.semantic_type == "heading"
    assert block.heading_level == 4
