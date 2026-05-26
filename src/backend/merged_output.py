from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Protocol

from .document_artifacts import build_page_model
from .document_artifacts import format_merged_page_markdown
from .document_artifacts import load_document_ir
from .document_artifacts import page_model_to_payload
from .markdown_export import page_to_preview_markdown
from .types import Page


class MergedOutputJob(Protocol):
    job_id: str
    document_id: str
    file_version: int
    original_filename: str
    ingestion: dict[str, Any]


BuildEffectiveOutputPlan = Callable[[MergedOutputJob], dict[str, Any] | None]
ApplyLocalImageFallback = Callable[[MergedOutputJob, Path, int, Page], Page]
BuildMergedOutput = Callable[[MergedOutputJob], tuple[dict[str, Any], str] | None]
SanitizeFilename = Callable[[str], str]
UtcNow = Callable[[], str]


def build_merged_output(
    job: MergedOutputJob,
    *,
    build_effective_output_plan: BuildEffectiveOutputPlan,
    apply_local_image_fallback: ApplyLocalImageFallback,
) -> tuple[dict[str, Any], str] | None:
    plan = build_effective_output_plan(job)
    if plan is None:
        return None

    merged_pages: list[dict[str, Any]] = []
    merged_markdown_parts: list[str] = []
    base_metadata: dict[str, Any] | None = None

    for page_number in plan["page_numbers"]:
        entry = plan["effective_page_runs"].get(page_number)
        output_dir = entry.get("output_dir") if entry else None
        if not output_dir:
            continue

        document_ir = load_document_ir(Path(str(output_dir)) / "document_ir.json")
        if document_ir is None:
            continue
        if base_metadata is None:
            base_metadata = {
                key: value for key, value in document_ir.items() if key != "pages"
            }

        page_index = page_number - 1
        page_payload = next(
            (
                page
                for page in document_ir.get("pages", [])
                if int(page.get("page_index", -1)) == page_index
            ),
            None,
        )
        if page_payload is None:
            continue

        page_model = apply_local_image_fallback(
            job,
            Path(str(output_dir)),
            page_number,
            build_page_model(page_payload),
        )
        merged_pages.append(page_model_to_payload(page_model))
        markdown = page_to_preview_markdown(page_model).strip()
        if markdown:
            merged_markdown_parts.append(
                format_merged_page_markdown(page_number, markdown)
            )

    if not merged_pages:
        return None

    merged_pages.sort(key=lambda page: int(page.get("page_index", 0)))
    merged_document_ir = dict(base_metadata or {})
    merged_document_ir["pages"] = merged_pages
    merged_document_ir["source_engine"] = "merged"
    merged_markdown = "\n\n".join(merged_markdown_parts)
    return merged_document_ir, merged_markdown


def build_merged_output_bundle(
    job: MergedOutputJob,
    *,
    build_merged_output_for_job: BuildMergedOutput,
    sanitize_filename: SanitizeFilename,
    utc_now: UtcNow,
) -> tuple[bytes, str] | None:
    merged_output = build_merged_output_for_job(job)
    if merged_output is None:
        return None

    merged_document_ir, merged_markdown = merged_output
    pages = (
        merged_document_ir.get("pages")
        if isinstance(merged_document_ir.get("pages"), list)
        else []
    )
    page_numbers = sorted(
        {
            int(page.get("page_index", 0)) + 1
            for page in pages
            if isinstance(page, dict)
        }
    )
    metadata = {
        "source_file": job.original_filename,
        "job_id": job.job_id,
        "document_id": job.document_id,
        "file_version": int(job.file_version),
        "page_count": int(job.ingestion.get("page_count", 0)),
        "output_pages": page_numbers,
        "generated_at": utc_now(),
        "contents": [
            "document.md",
            "document_ir.json",
            "metadata.json",
            "pages/page_XXXX.md",
        ],
    }

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("document.md", merged_markdown)
        handle.writestr(
            "document_ir.json",
            json.dumps(merged_document_ir, ensure_ascii=False, indent=2),
        )
        handle.writestr(
            "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
        for page_payload in pages:
            if not isinstance(page_payload, dict):
                continue
            try:
                page_model = build_page_model(page_payload)
                page_markdown = page_to_preview_markdown(page_model).strip()
                page_number = int(page_model.page_index) + 1
            except Exception:
                continue
            if page_markdown:
                handle.writestr(f"pages/page_{page_number:04d}.md", page_markdown)

    stem = Path(sanitize_filename(job.original_filename)).stem or "document"
    return archive.getvalue(), f"{stem}_output.zip"
