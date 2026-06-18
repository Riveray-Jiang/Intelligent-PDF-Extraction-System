from __future__ import annotations

import argparse
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.parse import parse_qs
from urllib.parse import urlparse

from .document_artifacts import ARTIFACT_FILENAMES
from .document_artifacts import artifact_paths_for_output_dir
from .document_artifacts import build_page_model as build_page_model
from .document_artifacts import format_merged_page_markdown as format_merged_page_markdown
from .document_artifacts import load_document_ir as load_document_ir
from .document_artifacts import page_model_to_payload
from .ingestion_agent import IngestionAgent
from .engine_service_manager import EngineServiceManager
from .file_history import build_file_history_payload as _build_file_history_payload
from .markdown_export import page_to_preview_markdown
from .pdfium_runtime import PDFIUM_LOCK
from .selection_agent import SelectionAgent
from .image_agent import ImageAgent
from .image_agent import page_has_image_content
from .image_agent_cache import IMAGE_AGENT_CACHE_VERSION as IMAGE_AGENT_CACHE_VERSION
from .image_agent_cache import image_agent_cache_path as image_agent_cache_path
from .image_agent_cache import legacy_image_agent_cache_path as legacy_image_agent_cache_path
from .image_agent_cache import load_image_agent_cache_record
from .image_agent_cache import save_image_agent_cache_record
from .image_agent_preview import extract_image_agent_preview
from .job_manifests import load_job_manifest as _load_job_manifest
from .job_manifests import read_document_job_manifests as _read_document_job_manifests
from .job_store import JobRecord
from .job_store import JobStore
from .job_utils import compute_duration_sec
from .job_utils import make_job_id
from .job_utils import make_run_id
from .job_utils import parse_utc as parse_utc
from .job_utils import sanitize_filename
from .job_utils import utc_now
from .local_image_fallback import apply_local_image_fallback
from .merged_output import build_merged_output as _build_merged_output
from .merged_output import build_merged_output_bundle as _build_merged_output_bundle
from .multipart_form import parse_multipart_form_data as _parse_multipart_form_data
from .output_planner import build_effective_output_plan as _build_effective_output_plan
from .output_planner import completed_history_entries as _completed_history_entries
from .output_planner import completed_page_set_for_run_mode as _completed_page_set_for_run_mode
from .output_planner import compress_page_numbers as compress_page_numbers
from .output_planner import current_output_page_set as _current_output_page_set
from .output_planner import ensure_run_allowed as _ensure_run_allowed
from .output_planner import load_page_preview_source
from .output_planner import looks_like_bad_reliable_override as looks_like_bad_reliable_override
from .output_planner import resolve_output_dir as _resolve_output_dir
from .output_planner import resolve_page_preview_output as _resolve_page_preview_output
from .pipeline_command import build_pipeline_command
from .pipeline_command import default_selection_mode as default_selection_mode
from .run_insights import read_run_insights


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data" / "jobs"
RUN_HISTORY_PATH = REPO_ROOT / "data" / "run_history.jsonl"
CURRENT_REPAIR_ENGINE_VERSION = "mineru2.5-pro-direct-v1"
INGESTION_AGENT = IngestionAgent()
SELECTION_AGENT = SelectionAgent()
RUN_HISTORY_LOCK = threading.Lock()
IMAGE_AGENT = ImageAgent()
_looks_like_bad_reliable_override = looks_like_bad_reliable_override


def append_run_history(job: "JobRecord") -> None:
    duration_sec = compute_duration_sec(job.started_at, job.finished_at)
    run_insights = read_run_insights(job)

    record = {
        "job_id": job.job_id,
        "document_id": job.document_id,
        "file_version": int(job.file_version),
        "replaces_job_id": job.replaces_job_id,
        "run_id": job.run_id,
        "filename": job.original_filename,
        "page_count": int(job.ingestion.get("page_count", 0)),
        "selection_mode": job.selection_mode,
        "selection": job.selection,
        "run_mode": job.run_mode,
        "status": job.status,
        "engine": job.engine,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_sec": duration_sec,
        "cascade_attempt": run_insights["cascade_attempt"],
        "failed_pages_count": run_insights["failed_pages_count"],
        "image_agent": run_insights["image_agent"],
        "engine_config": job.engine_config.name,
        "repair_engine_version": CURRENT_REPAIR_ENGINE_VERSION if job.run_mode == "reliable" else None,
        "job_dir": str(job.job_dir),
        "output_dir": job.output_dir,
    }

    RUN_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_HISTORY_LOCK:
        with RUN_HISTORY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_job_run_history(
    job_id: str,
    *,
    limit: int | None = 50,
    job: "JobRecord" | None = None,
) -> list[dict[str, Any]]:
    if not RUN_HISTORY_PATH.exists():
        return []

    with RUN_HISTORY_LOCK:
        try:
            lines = RUN_HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

    records: list[dict[str, Any]] = []
    for raw_line in reversed(lines):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if record.get("job_id") != job_id:
            continue

        run_id = record.get("run_id")
        output_dir_value = record.get("output_dir")
        resolved_pages: list[int] = []
        artifact_urls: dict[str, str] = {}
        if run_id and output_dir_value:
            artifact_paths = artifact_paths_for_output_dir(Path(output_dir_value))
            artifact_urls = {
                name: f"/api/jobs/{job_id}/runs/{run_id}/artifact/{name}"
                for name, path in artifact_paths.items()
                if path.exists()
            }
        if job is not None:
            resolved_pages = resolve_history_pages(job, record)

        records.append(
            {
                "job_id": str(record.get("job_id") or job_id),
                "document_id": str(record.get("document_id") or (job.document_id if job else job_id)),
                "file_version": int(record.get("file_version") or (job.file_version if job else 1)),
                "replaces_job_id": record.get("replaces_job_id"),
                "run_id": run_id,
                "filename": record.get("filename"),
                "page_count": int(record.get("page_count") or 0),
                "status": str(record.get("status") or "completed"),
                "run_mode": record.get("run_mode"),
                "selection_mode": record.get("selection_mode"),
                "selection": record.get("selection"),
                "resolved_pages": resolved_pages,
                "started_at": record.get("started_at"),
                "finished_at": record.get("finished_at"),
                "duration_sec": record.get("duration_sec"),
                "failed_pages_count": record.get("failed_pages_count"),
                "cascade_attempt": record.get("cascade_attempt"),
                "image_agent": record.get("image_agent") or {},
                "engine_config": record.get("engine_config"),
                "repair_engine_version": record.get("repair_engine_version"),
                "output_dir": output_dir_value,
                "artifact_urls": artifact_urls,
            }
        )
        if limit is not None and len(records) >= limit:
            break
    return records


def load_job_manifest(job_id: str) -> dict[str, Any] | None:
    return _load_job_manifest(job_id, data_root=DATA_ROOT)


def read_document_job_manifests(document_id: str) -> list[dict[str, Any]]:
    return _read_document_job_manifests(document_id, data_root=DATA_ROOT)


def resolve_history_pages(job: "JobRecord", entry: dict[str, Any]) -> list[int]:
    page_count = int(job.ingestion.get("page_count", 0))
    if page_count <= 0:
        return []

    selection_mode = str(entry.get("selection_mode") or "all")
    selection = entry.get("selection")
    try:
        resolved = SELECTION_AGENT.run(job.ingestion, selection_mode, selection)
    except ValueError:
        if selection_mode == "all":
            return list(range(1, page_count + 1))
        return []
    return [int(page_index) + 1 for page_index in resolved.get("selected_page_indices", [])]


def resolve_requested_pages(job: "JobRecord", selection_mode: str, selection: str | None) -> list[int]:
    page_count = int(job.ingestion.get("page_count", 0))
    if page_count <= 0:
        return []

    try:
        resolved = SELECTION_AGENT.run(job.ingestion, selection_mode, selection)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return sorted(
        {
            int(page_index) + 1
            for page_index in resolved.get("selected_page_indices", [])
            if int(page_index) + 1 > 0
        }
    )


def completed_history_entries(job: "JobRecord") -> list[dict[str, Any]]:
    return _completed_history_entries(job, read_job_run_history=read_job_run_history)


def completed_page_set_for_run_mode(job: "JobRecord", run_mode: str) -> set[int]:
    return _completed_page_set_for_run_mode(
        job,
        run_mode,
        completed_history_entries=completed_history_entries,
        resolve_history_pages=resolve_history_pages,
        repair_engine_version=CURRENT_REPAIR_ENGINE_VERSION,
    )


def current_output_page_set(job: "JobRecord") -> set[int]:
    return _current_output_page_set(job, build_effective_output_plan=build_effective_output_plan)


def ensure_run_allowed(job: "JobRecord", requested_pages: list[int], run_mode: str) -> None:
    return _ensure_run_allowed(
        job,
        requested_pages,
        run_mode,
        completed_page_set_for_run_mode=completed_page_set_for_run_mode,
        current_output_page_set=current_output_page_set,
    )


def build_effective_output_plan(job: "JobRecord") -> dict[str, Any] | None:
    return _build_effective_output_plan(
        job,
        completed_history_entries=completed_history_entries,
        resolve_history_pages=resolve_history_pages,
    )


def build_merged_output(job: "JobRecord") -> tuple[dict[str, Any], str] | None:
    return _build_merged_output(
        job,
        build_effective_output_plan=build_effective_output_plan,
        apply_local_image_fallback=apply_local_image_fallback,
    )


def build_merged_output_bundle(job: "JobRecord") -> tuple[bytes, str] | None:
    return _build_merged_output_bundle(
        job,
        build_merged_output_for_job=build_merged_output,
        sanitize_filename=sanitize_filename,
        utc_now=utc_now,
    )


def resolve_output_dir(job: "JobRecord", run_id: str | None = None) -> Path:
    return _resolve_output_dir(job, run_id)


def resolve_page_preview_output(job: "JobRecord", page_number: int, run_id: str | None = None) -> tuple[Path, str | None]:
    return _resolve_page_preview_output(
        job,
        page_number,
        run_id,
        resolve_output_dir=resolve_output_dir,
        build_effective_output_plan=build_effective_output_plan,
    )


def build_file_history_payload(job: "JobRecord") -> dict[str, Any]:
    return _build_file_history_payload(
        job,
        read_document_job_manifests=read_document_job_manifests,
        get_job=JOB_STORE.get,
        read_job_run_history=read_job_run_history,
        build_effective_output_plan=build_effective_output_plan,
    )


JOB_STORE = JobStore(
    repo_root=REPO_ROOT,
    data_root=DATA_ROOT,
    ingestion_agent=INGESTION_AGENT,
    resolve_requested_pages=resolve_requested_pages,
    ensure_run_allowed=ensure_run_allowed,
    build_pipeline_command=build_pipeline_command,
    append_run_history=append_run_history,
    make_job_id=make_job_id,
    make_run_id=make_run_id,
    sanitize_filename=sanitize_filename,
    utc_now=utc_now,
)


class ProductRequestHandler(BaseHTTPRequestHandler):
    server_version = "PDFProductServer/0.1"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_common_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return

        job = self._job_from_path(parsed.path)
        path_parts = [part for part in parsed.path.split("/") if part]
        if job and len(path_parts) == 4 and path_parts[:3] == ["api", "jobs", job.job_id] and path_parts[3] == "session":
            self._send_json(job.session_payload())
            return
        if job and len(path_parts) == 4 and path_parts[:3] == ["api", "jobs", job.job_id] and path_parts[3] == "status":
            self._send_json(job.status_snapshot())
            return
        if job and len(path_parts) == 4 and path_parts[:3] == ["api", "jobs", job.job_id] and path_parts[3] == "runs":
            self._send_json({"runs": read_job_run_history(job.job_id, job=job)})
            return
        if job and len(path_parts) == 4 and path_parts[:3] == ["api", "jobs", job.job_id] and path_parts[3] == "file-history":
            self._send_json(build_file_history_payload(job))
            return
        if job and len(path_parts) == 4 and path_parts[:3] == ["api", "jobs", job.job_id] and path_parts[3] == "download-output.zip":
            self._serve_merged_output_bundle(job)
            return
        if (
            job
            and len(path_parts) == 7
            and path_parts[:3] == ["api", "jobs", job.job_id]
            and path_parts[3] == "runs"
            and path_parts[5] == "artifact"
        ):
            self._serve_run_artifact(job, path_parts[4], path_parts[6])
            return
        if (
            job
            and len(path_parts) == 5
            and path_parts[:3] == ["api", "jobs", job.job_id]
            and path_parts[3] == "merged-artifact"
        ):
            self._serve_merged_artifact(job, path_parts[4])
            return
        if parsed.path.endswith("/page-preview") and job:
            self._serve_page_preview(job, parsed.query)
            return
        if "/thumb/" in parsed.path and job:
            name = parsed.path.rsplit("/thumb/", 1)[1]
            self._serve_image(job.thumbnails_dir / Path(name).name)
            return
        if "/preview/" in parsed.path and job:
            name = parsed.path.rsplit("/preview/", 1)[1]
            self._serve_preview(job, Path(name).name)
            return
        if "/artifact/" in parsed.path and job:
            name = parsed.path.rsplit("/artifact/", 1)[1]
            self._serve_artifact(job, name)
            return
        self._send_text(f"Route not found: {parsed.path}", status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self._handle_upload()
            return

        job = self._job_from_path(parsed.path)
        path_parts = [part for part in parsed.path.split("/") if part]
        if job and len(path_parts) == 4 and path_parts[:3] == ["api", "jobs", job.job_id] and path_parts[3] == "run":
            self._handle_run(job)
            return
        if job and len(path_parts) == 4 and path_parts[:3] == ["api", "jobs", job.job_id] and path_parts[3] == "cancel":
            self._handle_cancel(job)
            return
        if job and len(path_parts) == 4 and path_parts[:3] == ["api", "jobs", job.job_id] and path_parts[3] == "image-agent":
            self._handle_image_agent(job)
            return

        self._send_text(f"Route not found: {parsed.path}", status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _job_from_path(self, path: str) -> JobRecord | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "jobs":
            return None
        return JOB_STORE.get(parts[2])

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", errors="replace"))

    def _send_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text: str, *, status: HTTPStatus) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_upload(self) -> None:
        content_type = self.headers.get("Content-Type") or ""
        lower_content_type = content_type.lower()

        filename = "input.pdf"
        content = b""
        replaces_job_id: str | None = None

        if "multipart/form-data" in lower_content_type:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                self._send_text("Missing file body.", status=HTTPStatus.BAD_REQUEST)
                return
            fields, files = _parse_multipart_form_data(self.rfile.read(length), content_type)
            file_item = files.get("file")
            if file_item is None:
                self._send_text("Missing uploaded file.", status=HTTPStatus.BAD_REQUEST)
                return
            filename = file_item[0] or "input.pdf"
            content = file_item[1]
            replaces_value = fields.get("replaces_job_id")
            if isinstance(replaces_value, str):
                replaces_job_id = replaces_value.strip() or None
        else:
            if "pdf" not in lower_content_type and lower_content_type != "application/octet-stream":
                self._send_text("Upload must be a PDF.", status=HTTPStatus.BAD_REQUEST)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                self._send_text("Missing file body.", status=HTTPStatus.BAD_REQUEST)
                return
            content = self.rfile.read(length)

        if not content:
            self._send_text("Uploaded file is empty.", status=HTTPStatus.BAD_REQUEST)
            return

        try:
            job = JOB_STORE.create_job(
                filename=filename,
                content=content,
                replaces_job_id=replaces_job_id,
            )
        except Exception as exc:  # pragma: no cover - failure path for runtime errors
            self._send_text(f"Upload failed: {exc}", status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"job_id": job.job_id, "session": job.session_payload()}, status=HTTPStatus.CREATED)

    def _handle_run(self, job: JobRecord) -> None:
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_text("Invalid JSON payload.", status=HTTPStatus.BAD_REQUEST)
            return

        selection_mode = str(payload.get("selection_mode") or "all").strip().lower()
        selection = str(payload.get("selection") or "").strip() or None
        run_mode = str(payload.get("run_mode") or job.run_mode or "fast").strip().lower()
        output_dir_value = str(payload.get("output_dir") or "").strip()
        output_dir = Path(output_dir_value) if output_dir_value else job.default_output_dir

        if run_mode not in {"fast", "reliable"}:
            self._send_text("Invalid run mode.", status=HTTPStatus.BAD_REQUEST)
            return

        try:
            JOB_STORE.start_run(
                job,
                selection_mode=selection_mode,
                selection=selection,
                output_dir=output_dir,
                run_mode=run_mode,
            )
        except RuntimeError as exc:
            self._send_text(str(exc), status=HTTPStatus.CONFLICT)
            return

        self._send_json(job.status_snapshot(), status=HTTPStatus.ACCEPTED)

    def _handle_cancel(self, job: JobRecord) -> None:
        try:
            JOB_STORE.cancel_run(job)
        except RuntimeError as exc:
            self._send_text(str(exc), status=HTTPStatus.CONFLICT)
            return

        self._send_json(job.status_snapshot(), status=HTTPStatus.ACCEPTED)

    def _handle_image_agent(self, job: JobRecord) -> None:
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_text("Invalid JSON payload.", status=HTTPStatus.BAD_REQUEST)
            return

        raw_page = payload.get("page")
        run_id = str(payload.get("run_id") or "").strip() or None
        try:
            page_number = int(raw_page)
        except (TypeError, ValueError):
            self._send_text("Invalid page.", status=HTTPStatus.BAD_REQUEST)
            return
        if page_number < 1:
            self._send_text("Invalid page.", status=HTTPStatus.BAD_REQUEST)
            return
        if not IMAGE_AGENT.enabled:
            self._send_text("Image Agent is unavailable.", status=HTTPStatus.CONFLICT)
            return

        output_dir, resolved_run_id = resolve_page_preview_output(job, page_number, run_id)
        preview_source = load_page_preview_source(output_dir, page_number - 1)
        if preview_source is None:
            self._send_text("Page output not found.", status=HTTPStatus.NOT_FOUND)
            return

        page_model, _, _, _ = preview_source
        page_model = apply_local_image_fallback(job, output_dir, page_number, page_model)
        if not page_has_image_content(page_model):
            self._send_text(
                "Image Agent only runs on pages with detected visual content.",
                status=HTTPStatus.CONFLICT,
            )
            return

        cached_record = load_image_agent_cache_record(job, page_number, output_dir=output_dir)
        if cached_record is None:
            try:
                record, _ = IMAGE_AGENT.generate_page_record(
                    page_model,
                    pdf_path=job.input_pdf,
                    source_name=job.original_filename,
                )
            except Exception as exc:
                self._send_text(f"Image Agent failed: {exc}", status=HTTPStatus.BAD_GATEWAY)
                return
            record["generated_at"] = utc_now()
            save_image_agent_cache_record(job, page_number, record)
            cached_record = record

        self._send_json(
            {
                "page_number": page_number,
                "run_id": resolved_run_id,
                **extract_image_agent_preview(page_model, cached_record),
            }
        )

    def _serve_image(self, path: Path) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Image not found")
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._send_common_headers()
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _serve_preview(self, job: JobRecord, name: str) -> None:
        preview_path = job.previews_dir / name
        if not preview_path.exists():
            import re

            match = re.match(r"page_(\d+)\.jpg", name)
            if not match:
                self.send_error(HTTPStatus.NOT_FOUND, "Invalid preview name")
                return
            page_number = int(match.group(1))
            page_index = page_number - 1
            try:
                import pypdfium2 as pdfium

                with PDFIUM_LOCK:
                    doc = pdfium.PdfDocument(str(job.input_pdf))
                    try:
                        if page_index < 0 or page_index >= len(doc):
                            self.send_error(HTTPStatus.NOT_FOUND, "Page out of range")
                            return
                        page = doc[page_index]
                        bitmap = page.render(scale=150.0 / 72.0)
                        image = bitmap.to_pil()
                        job.previews_dir.mkdir(parents=True, exist_ok=True)
                        image.save(preview_path, format="JPEG", quality=85, optimize=True)
                    finally:
                        doc.close()
            except Exception:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Failed to render preview")
                return
        self._serve_image(preview_path)

    def _serve_artifact(self, job: JobRecord, name: str) -> None:
        if name not in ARTIFACT_FILENAMES:
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        artifact = job.artifact_paths()[name]
        if not artifact.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        self._serve_artifact_path(artifact, name)

    def _serve_run_artifact(self, job: JobRecord, run_id: str, name: str) -> None:
        if name not in ARTIFACT_FILENAMES:
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        artifact = job.default_output_dir / run_id / "output" / name
        if not artifact.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        self._serve_artifact_path(artifact, name)

    def _serve_merged_artifact(self, job: JobRecord, name: str) -> None:
        if name not in {"document_ir.json", "document.md"}:
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return

        merged_output = build_merged_output(job)
        if merged_output is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return

        merged_document_ir, merged_markdown = merged_output
        if name.endswith(".md"):
            data = merged_markdown.encode("utf-8")
            content_type = "text/markdown; charset=utf-8"
        else:
            data = json.dumps(merged_document_ir, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self._send_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_merged_output_bundle(self, job: JobRecord) -> None:
        bundle = build_merged_output_bundle(job)
        if bundle is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Output not found")
            return
        data, filename = bundle
        encoded_filename = quote(filename)

        self.send_response(HTTPStatus.OK)
        self._send_common_headers()
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="pdf-extraction-output.zip"; filename*=UTF-8\'\'{encoded_filename}',
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_artifact_path(self, artifact: Path, name: str) -> None:
        data = artifact.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._send_common_headers()
        content_type = "text/markdown; charset=utf-8" if name.endswith(".md") else "application/json; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_page_preview(self, job: JobRecord, query: str) -> None:
        params = parse_qs(query)
        raw_page = (params.get("page") or [""])[0]
        run_id = (params.get("run_id") or [""])[0].strip() or None
        try:
            page_number = int(raw_page)
        except (TypeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid page")
            return
        if page_number < 1:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid page")
            return

        page_index = page_number - 1
        payload: dict[str, Any] = {
            "run_id": run_id,
            "page_number": page_number,
            "page_index": page_index,
            "in_document_ir": False,
            "block_count": None,
            "block_types": {},
            "page_markdown": "",
            "page_ir": None,
            "image_content_detected": False,
            "image_hint": None,
            "image_alt_text": None,
            "image_interpretation_markdown": None,
            "image_agent_language": None,
            "image_agent_kind": None,
            "image_agent_generated": False,
            "image_agent_empty": False,
        }
        output_dir, resolved_run_id = resolve_page_preview_output(job, page_number, run_id)
        payload["run_id"] = resolved_run_id
        preview_source = load_page_preview_source(output_dir, page_index)
        if preview_source is not None:
            page_model, block_types, source_engine, page_ir = preview_source
            page_model = apply_local_image_fallback(job, output_dir, page_number, page_model)
            has_image_content = page_has_image_content(page_model)
            cached_record = load_image_agent_cache_record(job, page_number, output_dir=output_dir) if has_image_content else None
            payload.update(
                {
                    "in_document_ir": True,
                    "block_count": len(page_model.blocks),
                    "block_types": block_types,
                    "source_engine": source_engine,
                    "page_markdown": page_to_preview_markdown(page_model),
                    "page_ir": page_model_to_payload(page_model),
                    "image_content_detected": has_image_content,
                    "image_hint": (
                        "This page is mainly image-based. Use the original page as the primary reference."
                        if has_image_content
                        else None
                    ),
                    **extract_image_agent_preview(page_model, cached_record),
                }
            )
        self._send_json(payload)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload-first product server for PDF extraction.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8892)
    parser.add_argument("--skip-prewarm", action="store_true")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if not args.skip_prewarm:
        manager = EngineServiceManager(REPO_ROOT)
        for config_path, engine_names in (
            (REPO_ROOT / "configs" / "engines_prod.yaml", ["mineru"]),
            (REPO_ROOT / "configs" / "engines_prod_repair.yaml", ["mineru"]),
        ):
            try:
                warmed = manager.prewarm_from_config(config_path, engine_names=engine_names)
                if warmed:
                    print(f"Prewarmed services from {config_path.name}: {', '.join(warmed)}", flush=True)
            except Exception as exc:
                print(f"Prewarm skipped for {config_path.name}: {exc}", flush=True)
    server = ThreadingHTTPServer((args.host, args.port), ProductRequestHandler)
    print(f"Product server listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
