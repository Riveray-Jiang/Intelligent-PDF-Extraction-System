from __future__ import annotations

from backend.adapters.paddle_adapter import PaddleAdapter


def test_paddle_adapter_fills_missing_block_orders_monotonically() -> None:
    adapter = PaddleAdapter()
    parsed = adapter.parse(
        {
            "source_file": "demo.pdf",
            "pages": [
                {
                    "page_index": 0,
                    "parsing_res_list": [
                        {"block_label": "header", "block_order": None, "block_content": "hdr"},
                        {"block_label": "text", "block_order": 1, "block_content": "a"},
                        {"block_label": "paragraph_title", "block_order": 2, "block_content": "b"},
                        {"block_label": "text", "block_order": 3, "block_content": "c"},
                        {"block_label": "figure_title", "block_order": None, "block_content": "fig"},
                        {"block_label": "table", "block_order": None, "block_content": "<table></table>"},
                    ],
                }
            ],
        }
    )

    blocks = parsed["pages"][0]["blocks"]
    orders = [b.get("block_order") for b in blocks]

    assert orders == [0, 1, 2, 3, 4, 5]
