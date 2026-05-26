from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Protocol

from .document_artifacts import artifact_paths_for_output_dir
from .ingestion_agent import IngestionAgent
from .job_utils import compute_duration_sec
from .job_utils import make_job_id
from .job_utils import make_run_id
from .job_utils import sanitize_filename
from .job_utils import utc_now
from .pipeline_command import build_pipeline_command
from .pipeline_command import default_selection_mode
from .run_insights import read_run_insights
from .selection_agent import sanitize_outline_items


DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = DEFAULT_REPO_ROOT / "data" / "jobs"


class IngestionAgentLike(Protocol):
    def run(
        self,
        *,
        pdf_path: Path,
        output_dir: Path,
        render_thumbnails: bool,
    ) -> dict[str, Any]:
        ...


@dataclass
class JobRecord:
    job_id: str
    document_id: str
    file_version: int
    original_filename: str
    input_pdf: Path
    job_dir: Path
    replaces_job_id: str | None = None
    engine: str = "mineru"
    cascade_engine: str = ""
    run_mode: str = "fast"
    max_parse_attempts: int = 1
    max_rerun_attempts: int = 0
    max_cascade_attempts: int = 0
    ingestion: dict[str, Any] = field(default_factory=dict)
    status: str = "preparing"
    message: str = "Preparing document."
    stage: str = "Uploading"
    progress_percent: int = 6
    output_dir: str | None = None
    run_id: str | None = None
    selection_mode: str | None = None
    selection: str | None = None
    command: list[str] = field(default_factory=list)
    returncode: int | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    repo_root: Path = field(default=DEFAULT_REPO_ROOT, repr=False, compare=False)
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def engine_config(self) -> Path:
        if self.run_mode == "reliable":
            return self.repo_root / "configs" / "engines_prod_repair.yaml"
        return self.repo_root / "configs" / "engines_prod.yaml"

    @property
    def cascade_engine_config(self) -> Path:
        return self.repo_root / "configs" / "engines_prod_repair.yaml"

    @property
    def default_output_dir(self) -> Path:
        return self.job_dir / "runs"

    @property
    def thumbnails_dir(self) -> Path:
        return self.job_dir / "ingestion" / "thumbnails"

    @property
    def previews_dir(self) -> Path:
        return self.job_dir / "ingestion" / "previews"

    def artifact_paths(self) -> dict[str, Path]:
        base = Path(self.output_dir) if self.output_dir else self.default_output_dir
        return artifact_paths_for_output_dir(base)

    def write_manifest(self) -> None:
        payload = {
            "job_id": self.job_id,
            "document_id": self.document_id,
            "file_version": int(self.file_version),
            "replaces_job_id": self.replaces_job_id,
            "original_filename": self.original_filename,
            "input_pdf": str(self.input_pdf),
            "job_dir": str(self.job_dir),
            "status": self.status,
            "message": self.message,
            "stage": self.stage,
            "progress_percent": self.progress_percent,
            "page_count": int(self.ingestion.get("page_count", 0)),
            "has_outline": bool(
                sanitize_outline_items(self.ingestion.get("outline", []))
            ),
            "default_selection_mode": (
                default_selection_mode(self.ingestion) if self.ingestion else "all"
            ),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
            "output_dir": self.output_dir,
            "run_id": self.run_id,
            "selection_mode": self.selection_mode,
            "selection": self.selection,
            "run_mode": self.run_mode,
            "stdout_log": self.stdout_log,
            "stderr_log": self.stderr_log,
        }
        (self.job_dir / "job_manifest.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _artifact_flags(self) -> dict[str, bool]:
        base = Path(self.output_dir) if self.output_dir else self.default_output_dir
        parse_dir = base / "parse"
        return {
            "primary_parse": (parse_dir / self.engine).exists(),
            "fallback_parse": bool(self.cascade_engine)
            and (parse_dir / self.cascade_engine).exists(),
            "document_ir": (base / "document_ir.json").exists(),
            "document_md": (base / "document.md").exists(),
            "validation_report": (base / "validation_report.json").exists(),
            "pipeline_state": (base / "pipeline_state.json").exists(),
        }

    @staticmethod
    def _tail_log_line(path: Path | None) -> str | None:
        if path is None or not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines[-120:]):
            stripped = line.strip()
            if stripped:
                return stripped[:280]
        return None

    def status_snapshot(self) -> dict[str, Any]:
        with self.lock:
            output_dir = self.output_dir or str(self.default_output_dir)
            stdout_log = Path(self.stdout_log) if self.stdout_log else None
            stderr_log = Path(self.stderr_log) if self.stderr_log else None
            snapshot = {
                "job_id": self.job_id,
                "status": self.status,
                "message": self.message,
                "stage": self.stage,
                "progress_percent": self.progress_percent,
                "output_dir": output_dir,
                "run_id": self.run_id,
                "selection_mode": self.selection_mode,
                "selection": self.selection,
                "run_mode": self.run_mode,
                "returncode": self.returncode,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_sec": compute_duration_sec(self.started_at, self.finished_at),
                "cancel_requested": self.cancel_requested,
            }

        flags = self._artifact_flags()
        if snapshot["status"] == "running":
            if snapshot["cancel_requested"]:
                snapshot["stage"] = "Canceling"
                snapshot["message"] = "Stopping extraction."
                snapshot["progress_percent"] = max(snapshot["progress_percent"], 96)
            elif flags["validation_report"] or flags["pipeline_state"]:
                snapshot["stage"] = (
                    "Finalizing repair"
                    if self.run_mode == "reliable"
                    else "Finalizing result"
                )
                snapshot["message"] = "Wrapping up files."
                snapshot["progress_percent"] = max(snapshot["progress_percent"], 92)
            elif flags["document_ir"]:
                snapshot["stage"] = (
                    "Checking repair"
                    if self.run_mode == "reliable"
                    else "Checking result"
                )
                snapshot["message"] = "Checking the extracted pages."
                snapshot["progress_percent"] = max(snapshot["progress_percent"], 82)
            elif flags["fallback_parse"]:
                snapshot["stage"] = "Running repair"
                snapshot["message"] = "Repairing flagged pages."
                snapshot["progress_percent"] = max(snapshot["progress_percent"], 68)
            elif flags["primary_parse"]:
                snapshot["stage"] = (
                    "Running repair" if self.run_mode == "reliable" else "Running fast"
                )
                snapshot["message"] = "Extracting selected pages."
                snapshot["progress_percent"] = max(snapshot["progress_percent"], 38)

        log_tail = self._tail_log_line(stderr_log) or self._tail_log_line(stdout_log)
        run_insights = read_run_insights(self)
        snapshot["log_tail"] = log_tail
        snapshot["engines"] = {
            "primary": self.engine,
        }
        snapshot["cascade_attempt"] = run_insights["cascade_attempt"]
        snapshot["failed_pages_count"] = run_insights["failed_pages_count"]
        snapshot["image_agent"] = run_insights["image_agent"]
        snapshot["artifacts"] = {
            name: f"/api/jobs/{self.job_id}/artifact/{name}"
            for name, exists in {
                "document_ir.json": flags["document_ir"],
                "document.md": flags["document_md"],
                "validation_report.json": flags["validation_report"],
                "pipeline_state.json": flags["pipeline_state"],
            }.items()
            if exists
        }
        return snapshot

    def session_payload(self) -> dict[str, Any]:
        usable_outline = sanitize_outline_items(self.ingestion.get("outline", []))
        return {
            "job_id": self.job_id,
            "document_id": self.document_id,
            "file_version": int(self.file_version),
            "replaces_job_id": self.replaces_job_id,
            "input_pdf": str(self.input_pdf),
            "input_pdf_name": self.original_filename,
            "job_dir": str(self.job_dir),
            "page_count": int(self.ingestion.get("page_count", 0)),
            "has_outline": bool(usable_outline),
            "default_selection_mode": default_selection_mode(self.ingestion),
            "default_output_dir": str(self.default_output_dir),
            "pages": [
                {"page_index": int(page.get("page_index", 0))}
                for page in self.ingestion.get("pages", [])
            ],
            "outline": [
                {
                    "id": int(item.get("id", 0)),
                    "title": str(item.get("title", "")),
                    "page_index": int(item.get("page_index", 0)),
                    "level": int(item.get("level", 0)),
                }
                for item in usable_outline
            ],
            "job": self.status_snapshot(),
        }


ResolveRequestedPages = Callable[[JobRecord, str, str | None], list[int]]
EnsureRunAllowed = Callable[[JobRecord, list[int], str], None]
BuildPipelineCommand = Callable[..., list[str]]
AppendRunHistory = Callable[[JobRecord], None]
MakeJobId = Callable[[], str]
MakeRunId = Callable[[str], str]
SanitizeFilename = Callable[[str], str]
UtcNow = Callable[[], str]


def _missing_resolve_requested_pages(
    job: JobRecord,
    selection_mode: str,
    selection: str | None,
) -> list[int]:
    raise RuntimeError("resolve_requested_pages dependency is required.")


def _missing_ensure_run_allowed(
    job: JobRecord,
    requested_pages: list[int],
    run_mode: str,
) -> None:
    raise RuntimeError("ensure_run_allowed dependency is required.")


def _noop_append_run_history(job: JobRecord) -> None:
    return None


class JobStore:
    def __init__(
        self,
        *,
        repo_root: Path = DEFAULT_REPO_ROOT,
        data_root: Path = DEFAULT_DATA_ROOT,
        ingestion_agent: IngestionAgentLike | None = None,
        resolve_requested_pages: ResolveRequestedPages = _missing_resolve_requested_pages,
        ensure_run_allowed: EnsureRunAllowed = _missing_ensure_run_allowed,
        build_pipeline_command: BuildPipelineCommand = build_pipeline_command,
        append_run_history: AppendRunHistory = _noop_append_run_history,
        make_job_id: MakeJobId = make_job_id,
        make_run_id: MakeRunId = make_run_id,
        sanitize_filename: SanitizeFilename = sanitize_filename,
        utc_now: UtcNow = utc_now,
    ) -> None:
        self.repo_root = repo_root
        self.data_root = data_root
        self.ingestion_agent = ingestion_agent or IngestionAgent()
        self.resolve_requested_pages = resolve_requested_pages
        self.ensure_run_allowed = ensure_run_allowed
        self.build_pipeline_command = build_pipeline_command
        self.append_run_history = append_run_history
        self.make_job_id = make_job_id
        self.make_run_id = make_run_id
        self.sanitize_filename = sanitize_filename
        self.utc_now = utc_now
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _ingestion_snapshot_path(job_dir: Path) -> Path:
        return job_dir / "ingestion" / "ingestion_output.json"

    def _load_job_from_disk(self, job_id: str) -> JobRecord | None:
        job_dir = self.data_root / job_id
        manifest_path = job_dir / "job_manifest.json"
        if not manifest_path.exists():
            return None

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        input_pdf = Path(manifest.get("input_pdf") or "")
        if not input_pdf.exists():
            candidates = [
                path
                for path in job_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".pdf"
            ]
            if not candidates:
                return None
            input_pdf = candidates[0]

        ingestion_path = self._ingestion_snapshot_path(job_dir)
        ingestion: dict[str, Any]
        if ingestion_path.exists():
            try:
                ingestion = json.loads(ingestion_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ingestion = self.ingestion_agent.run(
                    pdf_path=input_pdf,
                    output_dir=job_dir,
                    render_thumbnails=False,
                )
        else:
            ingestion = self.ingestion_agent.run(
                pdf_path=input_pdf,
                output_dir=job_dir,
                render_thumbnails=False,
            )
            ingestion_path.parent.mkdir(parents=True, exist_ok=True)
            ingestion_path.write_text(
                json.dumps(ingestion, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        job = JobRecord(
            job_id=job_id,
            document_id=str(manifest.get("document_id") or job_id),
            file_version=int(manifest.get("file_version") or 1),
            original_filename=str(manifest.get("original_filename") or input_pdf.name),
            input_pdf=input_pdf,
            job_dir=job_dir,
            replaces_job_id=manifest.get("replaces_job_id"),
            run_mode=str(manifest.get("run_mode") or "fast"),
            ingestion=ingestion,
            status=str(manifest.get("status") or "ready"),
            message=str(manifest.get("message") or "Document ready for review."),
            stage=str(manifest.get("stage") or "Ready"),
            progress_percent=int(manifest.get("progress_percent") or 100),
            output_dir=manifest.get("output_dir"),
            run_id=manifest.get("run_id"),
            selection_mode=manifest.get("selection_mode"),
            selection=manifest.get("selection"),
            created_at=str(manifest.get("created_at") or self.utc_now()),
            started_at=manifest.get("started_at"),
            finished_at=manifest.get("finished_at"),
            cancel_requested=bool(manifest.get("cancel_requested")),
            repo_root=self.repo_root,
        )

        stdout_log = manifest.get("stdout_log")
        stderr_log = manifest.get("stderr_log")
        if stdout_log:
            job.stdout_log = str(stdout_log)
        elif (job_dir / "last_run_stdout.log").exists():
            job.stdout_log = str(job_dir / "last_run_stdout.log")
        if stderr_log:
            job.stderr_log = str(stderr_log)
        elif (job_dir / "last_run_stderr.log").exists():
            job.stderr_log = str(job_dir / "last_run_stderr.log")
        return job

    def create_job(
        self,
        *,
        filename: str,
        content: bytes,
        replaces_job_id: str | None = None,
    ) -> JobRecord:
        job_id = self.make_job_id()
        safe_name = self.sanitize_filename(filename)
        job_dir = self.data_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        input_pdf = job_dir / safe_name
        input_pdf.write_bytes(content)

        previous_job = self.get(replaces_job_id) if replaces_job_id else None
        document_id = previous_job.document_id if previous_job is not None else job_id
        file_version = (previous_job.file_version + 1) if previous_job is not None else 1

        job = JobRecord(
            job_id=job_id,
            document_id=document_id,
            file_version=file_version,
            original_filename=safe_name,
            input_pdf=input_pdf,
            job_dir=job_dir,
            replaces_job_id=previous_job.job_id if previous_job is not None else None,
            repo_root=self.repo_root,
        )
        job.write_manifest()

        with job.lock:
            job.status = "preparing"
            job.message = "Rendering pages and building document map."
            job.stage = "Ingestion"
            job.progress_percent = 12
            job.write_manifest()

        ingestion = self.ingestion_agent.run(
            pdf_path=input_pdf,
            output_dir=job_dir,
            render_thumbnails=True,
        )
        self._ingestion_snapshot_path(job_dir).write_text(
            json.dumps(ingestion, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        with job.lock:
            job.ingestion = ingestion
            job.status = "ready"
            job.message = "Document ready for review."
            job.stage = "Ready"
            job.progress_percent = 100
            job.write_manifest()

        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                return job

        job = self._load_job_from_disk(job_id)
        if job is None:
            return None

        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                return existing
            self._jobs[job_id] = job
            return job

    def start_run(
        self,
        job: JobRecord,
        *,
        selection_mode: str,
        selection: str | None,
        output_dir: Path,
        run_mode: str,
    ) -> None:
        requested_pages = self.resolve_requested_pages(job, selection_mode, selection)
        self.ensure_run_allowed(job, requested_pages, run_mode)
        base_output_root = output_dir.resolve()
        run_id = self.make_run_id(run_mode)
        run_dir = base_output_root / run_id
        run_output_dir = run_dir / "output"
        stdout_log = run_dir / "stdout.log"
        stderr_log = run_dir / "stderr.log"

        with job.lock:
            if job.status == "running":
                raise RuntimeError("A pipeline job is already running for this document.")

            job.output_dir = str(run_output_dir)
            job.run_id = run_id
            job.selection_mode = selection_mode
            job.selection = selection
            job.run_mode = run_mode
            job.command = self.build_pipeline_command(
                input_pdf=job.input_pdf,
                output_dir=run_output_dir,
                selection_mode=selection_mode,
                selection=selection,
                run_mode=run_mode,
                engine=job.engine,
                engine_config=job.engine_config,
                cascade_engine=job.cascade_engine,
                cascade_engine_config=job.cascade_engine_config,
                max_parse_attempts=job.max_parse_attempts,
                max_rerun_attempts=job.max_rerun_attempts,
                max_cascade_attempts=job.max_cascade_attempts,
            )
            job.stdout_log = str(stdout_log)
            job.stderr_log = str(stderr_log)
            job.returncode = None
            job.started_at = self.utc_now()
            job.finished_at = None
            job.cancel_requested = False
            job.process = None
            job.status = "running"
            job.message = "Launching extraction pipeline."
            job.stage = "Booting pipeline"
            job.progress_percent = 8
            job.write_manifest()

        self._clean_output(run_output_dir)
        thread = threading.Thread(target=self._run_pipeline, args=(job,), daemon=True)
        thread.start()

    def cancel_run(self, job: JobRecord) -> None:
        with job.lock:
            if job.status != "running":
                raise RuntimeError("No running extraction to cancel.")

            job.cancel_requested = True
            job.message = "Stopping extraction."
            job.stage = "Canceling"
            job.write_manifest()
            process = job.process

        if process is None:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                time.sleep(0.05)
                with job.lock:
                    if job.status != "running":
                        return
                    process = job.process
                if process is not None:
                    break

        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)

    @staticmethod
    def _clean_output(output_dir: Path) -> None:
        run_dir = output_dir.parent
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return

        if os.name == "nt":
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is not None:
                try:
                    process.send_signal(ctrl_break)
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

            if process.poll() is None:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=creationflags,
                )
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _finish_canceled_job(self, job: JobRecord) -> None:
        with job.lock:
            job.process = None
            job.returncode = None
            job.finished_at = self.utc_now()
            job.status = "canceled"
            job.message = "Extraction canceled."
            job.stage = "Canceled"
            job.progress_percent = 100
            job.write_manifest()
        self.append_run_history(job)

    def _run_pipeline(self, job: JobRecord) -> None:
        with job.lock:
            stdout_log = Path(job.stdout_log or (job.job_dir / "last_run_stdout.log"))
            stderr_log = Path(job.stderr_log or (job.job_dir / "last_run_stderr.log"))
            if job.cancel_requested:
                pass
            else:
                command = list(job.command)

        try:
            if job.cancel_requested:
                self._finish_canceled_job(job)
                return

            popen_kwargs: dict[str, Any] = {
                "cwd": self.repo_root,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0,
                )
            else:
                popen_kwargs["start_new_session"] = True

            process = subprocess.Popen(command, **popen_kwargs)

            with job.lock:
                job.process = process
                cancel_requested = job.cancel_requested

            if cancel_requested and process.poll() is None:
                self._terminate_process_tree(process)

            stdout, stderr = process.communicate()
            stdout_log.write_text(stdout or "", encoding="utf-8")
            stderr_log.write_text(stderr or "", encoding="utf-8")
            (job.job_dir / "last_run_stdout.log").write_text(
                stdout or "",
                encoding="utf-8",
            )
            (job.job_dir / "last_run_stderr.log").write_text(
                stderr or "",
                encoding="utf-8",
            )

            with job.lock:
                job.process = None
                job.returncode = process.returncode
                job.finished_at = self.utc_now()
                if job.cancel_requested:
                    job.status = "canceled"
                    job.message = "Extraction canceled."
                    job.stage = "Canceled"
                    job.progress_percent = 100
                elif process.returncode == 0:
                    job.status = "completed"
                    job.message = "Pipeline completed successfully."
                    job.stage = "Completed"
                    job.progress_percent = 100
                else:
                    job.status = "failed"
                    job.message = f"Pipeline failed with exit code {process.returncode}."
                    job.stage = "Failed"
                    job.progress_percent = 100
                job.write_manifest()
            self.append_run_history(job)
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            try:
                stderr_log.write_text(error_text, encoding="utf-8")
                (job.job_dir / "last_run_stderr.log").write_text(
                    error_text,
                    encoding="utf-8",
                )
            except OSError:
                pass

            with job.lock:
                job.process = None
                job.returncode = -1
                job.finished_at = self.utc_now()
                if job.cancel_requested:
                    job.status = "canceled"
                    job.message = "Extraction canceled."
                    job.stage = "Canceled"
                else:
                    job.status = "failed"
                    job.message = f"Pipeline failed before startup: {error_text}"
                    job.stage = "Failed"
                job.progress_percent = 100
                job.write_manifest()
            self.append_run_history(job)
