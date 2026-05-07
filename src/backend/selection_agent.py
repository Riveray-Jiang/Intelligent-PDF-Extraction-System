from __future__ import annotations


def _normalize_outline_title(title: str | None) -> str:
    return " ".join(str(title or "").split()).strip()


def _outline_score(item: dict) -> int:
    title = _normalize_outline_title(item.get("title"))
    level = int(item.get("level", 0))
    has_letters = any(char.isalpha() for char in title)
    alnum_chars = [char for char in title if char.isalnum()]
    mostly_numeric = bool(alnum_chars) and all(char.isdigit() for char in alnum_chars)
    return max(0, 40 - level * 8) + min(len(title), 32) + (8 if has_letters else 0) - (14 if mostly_numeric else 0)


def sanitize_outline_items(outline: list[dict] | None) -> list[dict]:
    by_page: dict[int, dict] = {}

    for raw_item in outline or []:
        title = _normalize_outline_title(raw_item.get("title"))
        if not title:
            continue

        candidate = {
            "id": int(raw_item.get("id", 0)),
            "title": title,
            "level": int(raw_item.get("level", 0)),
            "page_index": int(raw_item.get("page_index", 0)),
        }
        existing = by_page.get(candidate["page_index"])
        if existing is None or _outline_score(candidate) > _outline_score(existing):
            by_page[candidate["page_index"]] = candidate

    return sorted(
        by_page.values(),
        key=lambda item: (
            int(item.get("page_index", 0)),
            int(item.get("level", 0)),
            int(item.get("id", 0)),
        ),
    )


def _selected_outline_item(item: dict) -> dict:
    return {
        "id": int(item["id"]),
        "title": str(item["title"]),
        "page_index": int(item["page_index"]),
    }


class SelectionAgent:
    """Resolve selection mode: all / outline / pagerange."""

    @staticmethod
    def _parse_positive_selector(selection: str) -> list[int]:
        values: set[int] = set()
        for token in selection.split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                left, right = token.split("-", 1)
                start = int(left)
                end = int(right)
                if start <= 0 or end <= 0:
                    raise ValueError("Selector values must be >= 1")
                if end < start:
                    start, end = end, start
                values.update(range(start, end + 1))
            else:
                value = int(token)
                if value <= 0:
                    raise ValueError("Selector values must be >= 1")
                values.add(value)
        return sorted(values)

    @staticmethod
    def _to_ranges(sorted_indices: list[int]) -> list[list[int]]:
        if not sorted_indices:
            return []
        ranges: list[list[int]] = []
        start = prev = sorted_indices[0]
        for current in sorted_indices[1:]:
            if current == prev + 1:
                prev = current
                continue
            ranges.append([start, prev])
            start = prev = current
        ranges.append([start, prev])
        return ranges

    def run(self, ingestion_output: dict, selection_mode: str, selection: str | None = None) -> dict:
        page_count = int(ingestion_output.get("page_count", 0))
        if page_count <= 0:
            raise ValueError("ingestion_output.page_count must be > 0")

        requested_mode = selection_mode.lower()
        mode = requested_mode
        selected_pages: list[int] = []
        selected_outline: list[dict] = []
        selection_text = selection
        fallback_reason: str | None = None

        if mode == "all":
            selected_pages = list(range(page_count))
            selection_text = "all"
        elif mode == "pagerange":
            if not selection:
                selected_pages = list(range(page_count))
                selection_text = "all"
            else:
                page_numbers_1based = self._parse_positive_selector(selection)
                selected_pages = sorted(
                    {
                        min(page_count - 1, max(0, page_number - 1))
                        for page_number in page_numbers_1based
                    }
                )
        elif mode == "outline":
            outline = sanitize_outline_items(ingestion_output.get("outline", []))
            if not outline:
                mode = "all"
                fallback_reason = "no_outline"
                selected_pages = list(range(page_count))
                selection_text = "all"
                outline = []
            else:
                if not selection:
                    chosen_ids = [int(item["id"]) for item in outline]
                    selection_text = "all_outline"
                else:
                    chosen_ids = self._parse_positive_selector(selection)

                by_id = {int(item["id"]): item for item in outline}
                chosen_entries = [by_id[i] for i in chosen_ids if i in by_id]
                if not chosen_entries:
                    raise ValueError("No valid outline ids were selected")

                chosen_entries.sort(key=lambda item: int(item["page_index"]))
                selected_outline = [_selected_outline_item(item) for item in chosen_entries]

                outline_starts = [int(item["page_index"]) for item in outline]
                for entry in chosen_entries:
                    start = int(entry["page_index"])
                    following = [s for s in outline_starts if s > start]
                    end = (following[0] - 1) if following else (page_count - 1)
                    for p in range(start, end + 1):
                        selected_pages.append(p)
                selected_pages = sorted(set(selected_pages))
        else:
            raise ValueError(f"Unsupported selection_mode: {selection_mode}")

        return {
            "requested_mode": requested_mode,
            "mode": mode,
            "selection": selection_text,
            "selected_page_indices": selected_pages,
            "selected_ranges": self._to_ranges(selected_pages),
            "selected_outline": selected_outline,
            "selected_count": len(selected_pages),
            "fallback_reason": fallback_reason,
        }
