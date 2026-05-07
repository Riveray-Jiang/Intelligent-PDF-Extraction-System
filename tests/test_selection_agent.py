from backend.selection_agent import SelectionAgent


def test_selection_pagerange_mode() -> None:
    agent = SelectionAgent()
    ingestion = {"page_count": 10, "outline": []}

    selected = agent.run(ingestion, selection_mode="pagerange", selection="1,3-4,8")
    assert selected["selected_page_indices"] == [0, 2, 3, 7]
    assert selected["selected_ranges"] == [[0, 0], [2, 3], [7, 7]]


def test_selection_outline_mode() -> None:
    agent = SelectionAgent()
    ingestion = {
        "page_count": 10,
        "outline": [
            {"id": 1, "title": "A", "page_index": 0},
            {"id": 2, "title": "B", "page_index": 4},
            {"id": 3, "title": "C", "page_index": 7},
        ],
    }

    selected = agent.run(ingestion, selection_mode="outline", selection="2")
    assert selected["selected_page_indices"] == [4, 5, 6]
    assert selected["selected_outline"] == [{"id": 2, "title": "B", "page_index": 4}]


def test_selection_outline_mode_falls_back_to_all_when_outline_missing() -> None:
    agent = SelectionAgent()
    ingestion = {"page_count": 4, "outline": [], "has_outline": False}

    selected = agent.run(ingestion, selection_mode="outline", selection=None)

    assert selected["requested_mode"] == "outline"
    assert selected["mode"] == "all"
    assert selected["selection"] == "all"
    assert selected["selected_page_indices"] == [0, 1, 2, 3]
    assert selected["selected_outline"] == []
    assert selected["fallback_reason"] == "no_outline"
