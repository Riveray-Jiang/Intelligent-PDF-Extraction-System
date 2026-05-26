from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Protocol

from .document_artifacts import build_page_model
from .document_artifacts import load_document_ir
from .types import Block
from .types import Page


class OutputPlanJob(Protocol):
    job_id: str
    ingestion: dict[str, Any]
    output_dir: str | None
    run_id: str | None
    default_output_dir: Path


ReadRunHistory = Callable[..., list[dict[str, Any]]]
ResolveHistoryPages = Callable[[OutputPlanJob, dict[str, Any]], list[int]]
CompletedHistoryEntries = Callable[[OutputPlanJob], list[dict[str, Any]]]
CompletedPageSetForRunMode = Callable[[OutputPlanJob, str], set[int]]
CurrentOutputPageSet = Callable[[OutputPlanJob], set[int]]
BuildEffectiveOutputPlan = Callable[[OutputPlanJob], dict[str, Any] | None]
ResolveOutputDir = Callable[[OutputPlanJob, str | None], Path]
PreviewSource = tuple[Page, dict[str, int], str | None, dict[str, Any]]

_TEXTISH_BLOCK_TYPES = {
    "text",
    "title",
    "section_title",
    "paragraph_title",
    "heading",
    "formula",
    "equation",
}
_IMAGE_BLOCK_TYPES = {"image", "figure", "image_body"}


def compress_page_numbers(page_numbers: list[int]) -> str:
    if not page_numbers:
        return ""
    sorted_pages = sorted(set(int(page_number) for page_number in page_numbers))
    ranges: list[str] = []
    start = sorted_pages[0]
    previous = sorted_pages[0]
    for current in sorted_pages[1:]:
        if current == previous + 1:
            previous = current
            continue
        ranges.append(f"{start}" if start == previous else f"{start}-{previous}")
        start = previous = current
    ranges.append(f"{start}" if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def completed_history_entries(
    job: OutputPlanJob,
    *,
    read_job_run_history: ReadRunHistory,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entry in read_job_run_history(job.job_id, limit=None):
        output_dir = entry.get("output_dir")
        if entry.get("status") != "completed" or not entry.get("run_id") or not output_dir:
            continue
        if not (Path(str(output_dir)) / "document_ir.json").exists():
            continue
        entries.append(entry)
    return entries


def completed_page_set_for_run_mode(
    job: OutputPlanJob,
    run_mode: str,
    *,
    completed_history_entries: CompletedHistoryEntries,
    resolve_history_pages: ResolveHistoryPages,
    repair_engine_version: str,
) -> set[int]:
    pages: set[int] = set()
    for entry in completed_history_entries(job):
        if str(entry.get("run_mode") or "") != run_mode:
            continue
        if (
            run_mode == "reliable"
            and entry.get("repair_engine_version") != repair_engine_version
        ):
            continue
        pages.update(resolve_history_pages(job, entry))
    return pages


def current_output_page_set(
    job: OutputPlanJob,
    *,
    build_effective_output_plan: BuildEffectiveOutputPlan,
) -> set[int]:
    plan = build_effective_output_plan(job)
    if plan is None:
        return set()
    return {int(page_number) for page_number in plan.get("page_numbers", [])}


def ensure_run_allowed(
    job: OutputPlanJob,
    requested_pages: list[int],
    run_mode: str,
    *,
    completed_page_set_for_run_mode: CompletedPageSetForRunMode,
    current_output_page_set: CurrentOutputPageSet,
) -> None:
    if not requested_pages:
        raise RuntimeError("Choose at least one page before running.")

    if run_mode == "fast":
        already_extracted = completed_page_set_for_run_mode(job, "fast")
        overlap = sorted(set(requested_pages) & already_extracted)
        if overlap:
            raise RuntimeError(
                "Fast extraction already exists for pages "
                f"{compress_page_numbers(overlap)}."
            )
        return

    if run_mode == "reliable":
        available_pages = current_output_page_set(job)
        unavailable = sorted(set(requested_pages) - available_pages)
        if unavailable:
            raise RuntimeError(
                "Repair is only available for pages already in the current output: "
                f"{compress_page_numbers(unavailable)}."
            )

        already_repaired = completed_page_set_for_run_mode(job, "reliable")
        overlap = sorted(set(requested_pages) & already_repaired)
        if overlap:
            raise RuntimeError(
                "Repair already exists for pages "
                f"{compress_page_numbers(overlap)}."
            )
        return


def build_effective_output_plan(
    job: OutputPlanJob,
    *,
    completed_history_entries: CompletedHistoryEntries,
    resolve_history_pages: ResolveHistoryPages,
) -> dict[str, Any] | None:
    completed_entries = completed_history_entries(job)
    if not completed_entries:
        return None

    page_count = int(job.ingestion.get("page_count", 0))
    latest_whole_document_run: dict[str, Any] | None = None
    for entry in completed_entries:
        pages = resolve_history_pages(job, entry)
        if page_count > 0 and len(pages) == page_count:
            latest_whole_document_run = entry
            break

    base_entry = latest_whole_document_run or completed_entries[0]
    if latest_whole_document_run is not None:
        base_pages = resolve_history_pages(job, base_entry)
    else:
        base_pages = sorted(
            {
                page_number
                for entry in completed_entries
                for page_number in resolve_history_pages(job, entry)
            }
        )
    if not base_pages and page_count > 0:
        base_pages = list(range(1, page_count + 1))

    base_page_set = set(base_pages)
    effective_page_runs: dict[int, dict[str, Any]] = {}
    latest_fast_entry_by_page: dict[int, dict[str, Any]] = {}
    page_model_cache: dict[tuple[str, int], Page | None] = {}

    def page_model_for_entry(entry: dict[str, Any], page_number: int) -> Page | None:
        output_dir = entry.get("output_dir")
        if not output_dir:
            return None
        cache_key = (str(output_dir), int(page_number))
        if cache_key in page_model_cache:
            return page_model_cache[cache_key]
        preview_source = load_page_preview_source(Path(str(output_dir)), page_number - 1)
        page_model_cache[cache_key] = (
            preview_source[0] if preview_source is not None else None
        )
        return page_model_cache[cache_key]

    for entry in completed_entries:
        if str(entry.get("run_mode") or "") != "fast":
            continue
        for page_number in resolve_history_pages(job, entry):
            latest_fast_entry_by_page.setdefault(page_number, entry)

    for entry in completed_entries:
        for page_number in resolve_history_pages(job, entry):
            if page_number not in base_page_set or page_number in effective_page_runs:
                continue
            if str(entry.get("run_mode") or "") == "reliable":
                reliable_page = page_model_for_entry(entry, page_number)
                fast_entry = latest_fast_entry_by_page.get(page_number)
                fast_page = (
                    page_model_for_entry(fast_entry, page_number)
                    if fast_entry is not None
                    else None
                )
                if (
                    reliable_page is not None
                    and looks_like_bad_reliable_override(reliable_page, fast_page)
                ):
                    continue
            effective_page_runs[page_number] = entry

    for page_number in base_pages:
        effective_page_runs.setdefault(page_number, base_entry)

    return {
        "base_entry": base_entry,
        "latest_whole_document_run": latest_whole_document_run,
        "page_numbers": sorted(base_pages),
        "effective_page_runs": effective_page_runs,
    }


def load_page_preview_source(
    output_dir: Path,
    page_index: int,
) -> PreviewSource | None:
    document_ir = load_document_ir(output_dir / "document_ir.json")
    if document_ir is None:
        return None

    page_payload = next(
        (
            page
            for page in document_ir.get("pages", [])
            if int(page.get("page_index", -1)) == page_index
        ),
        None,
    )
    if page_payload is None:
        return None

    blocks = page_payload.get("blocks", [])
    block_types: dict[str, int] = {}
    for block in blocks:
        key = str(block.get("type", "unknown"))
        block_types[key] = block_types.get(key, 0) + 1

    return (
        build_page_model(page_payload),
        block_types,
        document_ir.get("source_engine"),
        {
            "page_index": page_payload.get("page_index"),
            "width": page_payload.get("width"),
            "height": page_payload.get("height"),
            "blocks": blocks,
        },
    )


def _preview_content_blocks(page: Page) -> list[Block]:
    filtered: list[Block] = []
    for block in page.blocks:
        block_type = block.type.lower().strip()
        if block_type in {"header", "footer", "discarded", "image_interpretation"}:
            continue
        filtered.append(block)
    return filtered


def _bbox_area_ratio(block: Block, page: Page) -> float:
    if not block.bbox or len(block.bbox) < 4:
        return 0.0
    width = float(page.width or 0)
    height = float(page.height or 0)
    if width <= 0 or height <= 0:
        return 0.0
    left, top, right, bottom = [float(value) for value in block.bbox]
    block_area = max(0.0, right - left) * max(0.0, bottom - top)
    page_area = max(1.0, width * height)
    return block_area / page_area


def _page_text_tokens(page: Page) -> set[str]:
    raw = " ".join(
        block.text.strip()
        for block in _preview_content_blocks(page)
        if block.type.lower().strip() != "image" and block.text.strip()
    )
    return set(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", raw))


def looks_like_bad_reliable_override(candidate_page: Page, fast_page: Page | None) -> bool:
    if fast_page is None:
        return False

    candidate_blocks = _preview_content_blocks(candidate_page)
    fast_blocks = _preview_content_blocks(fast_page)
    if not candidate_blocks or not fast_blocks:
        return False

    candidate_tables = [
        block for block in candidate_blocks if block.type.lower().strip() == "table"
    ]
    candidate_textish = [
        block
        for block in candidate_blocks
        if block.type.lower().strip() in _TEXTISH_BLOCK_TYPES
        and block.text.strip()
    ]
    fast_tables = [block for block in fast_blocks if block.type.lower().strip() == "table"]
    fast_non_table_count = sum(
        1 for block in fast_blocks if block.type.lower().strip() != "table"
    )
    fast_has_image = any(
        block.type.lower().strip() in _IMAGE_BLOCK_TYPES for block in fast_blocks
    )

    if len(candidate_blocks) != 1 or len(candidate_tables) != 1 or candidate_textish:
        return False
    if len(fast_blocks) == 1 and fast_tables:
        return False
    if fast_non_table_count < 2 and not fast_has_image:
        return False

    candidate_table = candidate_tables[0]
    if _bbox_area_ratio(candidate_table, candidate_page) < 0.45:
        return False

    candidate_tokens = _page_text_tokens(candidate_page)
    fast_tokens = _page_text_tokens(fast_page)
    if fast_tokens and candidate_tokens:
        overlap = len(candidate_tokens & fast_tokens) / max(1, len(fast_tokens))
        if overlap >= 0.35:
            return False
    return True


def resolve_output_dir(job: OutputPlanJob, run_id: str | None = None) -> Path:
    if run_id:
        return job.default_output_dir / run_id / "output"
    if job.output_dir:
        return Path(job.output_dir)
    return job.default_output_dir


def resolve_page_preview_output(
    job: OutputPlanJob,
    page_number: int,
    run_id: str | None = None,
    *,
    resolve_output_dir: ResolveOutputDir,
    build_effective_output_plan: BuildEffectiveOutputPlan,
) -> tuple[Path, str | None]:
    if run_id:
        return resolve_output_dir(job, run_id), run_id

    plan = build_effective_output_plan(job)
    if plan is not None:
        entry = plan["effective_page_runs"].get(page_number)
        output_dir = entry.get("output_dir") if entry else None
        if output_dir:
            return Path(str(output_dir)), entry.get("run_id")

    return resolve_output_dir(job, None), job.run_id
