from __future__ import annotations

from typing import Any
from typing import Callable
from typing import Protocol


class FileHistoryJob(Protocol):
    job_id: str
    document_id: str
    file_version: int
    replaces_job_id: str | None
    original_filename: str
    created_at: str
    ingestion: dict[str, Any]


ReadDocumentJobManifests = Callable[[str], list[dict[str, Any]]]
GetJob = Callable[[str], FileHistoryJob | None]
ReadJobRunHistory = Callable[..., list[dict[str, Any]]]
BuildEffectiveOutputPlan = Callable[[FileHistoryJob], dict[str, Any] | None]


def build_file_history_payload(
    job: FileHistoryJob,
    *,
    read_document_job_manifests: ReadDocumentJobManifests,
    get_job: GetJob,
    read_job_run_history: ReadJobRunHistory,
    build_effective_output_plan: BuildEffectiveOutputPlan,
) -> dict[str, Any]:
    versions: list[dict[str, Any]] = []
    for manifest in read_document_job_manifests(job.document_id):
        version_job_id = str(manifest.get("job_id") or "")
        if not version_job_id:
            continue
        version_job = get_job(version_job_id)
        if version_job is None:
            continue

        runs = read_job_run_history(version_job.job_id, limit=None, job=version_job)
        plan = build_effective_output_plan(version_job)
        merged_artifact_urls: dict[str, str] = {}
        latest_output_pages: list[int] = []
        effective_page_run_ids: dict[int, str | None] = {}
        if plan is not None:
            merged_artifact_urls = {
                "document.md": (
                    f"/api/jobs/{version_job.job_id}/merged-artifact/document.md"
                ),
                "document_ir.json": (
                    f"/api/jobs/{version_job.job_id}/merged-artifact/document_ir.json"
                ),
            }
            latest_output_pages = [
                int(page_number) for page_number in plan["page_numbers"]
            ]
            effective_page_run_ids = {
                int(page_number): entry.get("run_id")
                for page_number, entry in plan["effective_page_runs"].items()
            }

        versions.append(
            {
                "job_id": version_job.job_id,
                "document_id": version_job.document_id,
                "file_version": int(version_job.file_version),
                "replaces_job_id": version_job.replaces_job_id,
                "filename": version_job.original_filename,
                "created_at": version_job.created_at,
                "page_count": int(version_job.ingestion.get("page_count", 0)),
                "is_current": version_job.job_id == job.job_id,
                "has_output": bool(merged_artifact_urls),
                "latest_output_pages": latest_output_pages,
                "effective_page_run_ids": effective_page_run_ids,
                "merged_artifact_urls": merged_artifact_urls,
                "runs": runs,
            }
        )

    return {
        "document_id": job.document_id,
        "current_job_id": job.job_id,
        "versions": versions,
    }
