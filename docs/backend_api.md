# Backend API

> Module: [src/backend/product_server.py](../src/backend/product_server.py)
> Service: PDFProductServer/0.1
> Purpose: HTTP backend for the PDF extraction product. Handles upload → ingest → run pipeline → poll status → fetch artifacts.

## 1. Overview

Single-process multithreaded HTTP server built on the standard library `ThreadingHTTPServer` — no web framework. Default bind: `127.0.0.1:8892`.

Start it with:

```powershell
.\scripts\run_product_server.ps1 -BindHost 127.0.0.1 -Port 8892
# or
$env:PYTHONPATH = "src"
python -m backend.product_server --host 127.0.0.1 --port 8892
```

CORS is wide open (`*`), methods `GET, POST, OPTIONS`. There is **no auth** — do not expose the port outside localhost without a reverse proxy.

### Disk layout

Each upload becomes a **Job** rooted at `data/jobs/<job_id>/`:

```
data/jobs/job_<ts>_<8hex>/
├── <original>.pdf
├── job_manifest.json              # persisted job state
├── ingestion/
│   ├── ingestion_output.json      # page count / outline / sizes
│   ├── thumbnails/page_*.jpg      # rendered up-front
│   └── previews/page_*.jpg        # rendered on demand, cached
├── last_run_stdout.log
├── last_run_stderr.log
└── runs/run_<ts>_<mode>_<6hex>/
    ├── stdout.log
    ├── stderr.log
    └── output/
        ├── document_ir.json       # main artifact (structured IR)
        ├── document.md            # main artifact (markdown export)
        ├── validation_report.json
        ├── pipeline_state.json
        └── parse/<engine>/...     # raw engine output
data/run_history.jsonl              # append-only audit log
```

### Job lifecycle

| `status`    | Meaning                                                           |
| ----------- | ----------------------------------------------------------------- |
| `preparing` | File saved, ingestion in progress                                 |
| `ready`     | Ingestion done, waiting for the user to pick pages and run        |
| `running`   | Pipeline subprocess is alive                                      |
| `completed` | Subprocess exit code 0                                            |
| `failed`    | Subprocess exit code ≠ 0                                          |
| `canceled`  | Cancelled by the user; subprocess tree was killed                 |

`stage` and `progress_percent` are inferred on each `/status` call from which intermediate files exist on disk (parse subdir → IR → validation report → pipeline state). The pipeline does not push progress.

### How runs work

`POST /run` does **not** execute the algorithm in the request thread. It:
1. Builds a `python -m backend.pipeline_graph ...` command via [build_pipeline_command](../src/backend/product_server.py#L41-L89).
2. Spawns a daemon thread that `subprocess.Popen`s the pipeline.
3. Pipes stdout/stderr to log files; `/status` tails them.
4. On exit, updates `status` and appends one line to `run_history.jsonl`.

`run_mode` controls which engines run:
- `fast` → MinerU pipeline only (`configs/engines_prod.yaml`)
- `reliable` → MinerU + Paddle cascade repair (`configs/engines_prod_vlm_repair.yaml`)

Cancel uses `CTRL_BREAK_EVENT` + `taskkill /T /F` on Windows, `SIGTERM` then `SIGKILL` on POSIX, so the whole process tree is reaped.

---

## 2. Endpoint summary

| Method  | Path                                            | Purpose                          |
| ------- | ----------------------------------------------- | -------------------------------- |
| GET     | `/api/health`                                   | Liveness probe                   |
| POST    | `/api/upload`                                   | Upload PDF, create job, ingest   |
| GET     | `/api/jobs/{id}/session`                        | Full session payload             |
| GET     | `/api/jobs/{id}/status`                         | Live status snapshot             |
| POST    | `/api/jobs/{id}/run`                            | Start the pipeline               |
| POST    | `/api/jobs/{id}/cancel`                         | Cancel the running pipeline      |
| GET     | `/api/jobs/{id}/page-preview?page=N`            | Per-page IR / markdown preview   |
| GET     | `/api/jobs/{id}/thumb/{filename}`               | Page thumbnail (jpg)             |
| GET     | `/api/jobs/{id}/preview/page_NNNN.jpg`          | High-res preview, rendered lazily|
| GET     | `/api/jobs/{id}/artifact/{name}`                | Download a pipeline artifact     |
| OPTIONS | any                                             | CORS preflight                   |

Routing: [do_GET](../src/backend/product_server.py#L741-L769) / [do_POST](../src/backend/product_server.py#L771-L785).

---

## 3. Endpoints

### `GET /api/health`

Returns `{"ok": true}`. Use for liveness checks.

### `POST /api/upload`

Uploads a PDF, creates a job, runs ingestion synchronously (renders thumbnails, extracts outline), returns the full session.

**Request** — either:
- `multipart/form-data` with field `file` (recommended), or
- raw `application/pdf` / `application/octet-stream` body.

Filenames are sanitized to `[A-Za-z0-9._-]`.

**Response `201`**

```json
{
  "job_id": "job_20260410_120355_a1b2c3d4",
  "session": {
    "job_id": "job_...",
    "input_pdf_name": "paper.pdf",
    "page_count": 42,
    "has_outline": true,
    "default_selection_mode": "outline",
    "pages": [{ "page_index": 0 }, "..."],
    "outline": [
      { "id": 1, "title": "Introduction", "page_index": 0, "level": 0 }
    ],
    "job": { "<status snapshot, see /status>": "..." }
  }
}
```

`default_selection_mode` is `"outline"` if the PDF has bookmarks, otherwise `"all"`.

**Errors:** 400 (missing/empty file or wrong content type), 500 (ingest failure).

> Note: ingestion is synchronous, so this call scales with page count. Set a generous client timeout.

### `GET /api/jobs/{id}/session`

Returns the same `session` payload as upload. Use this to restore state when the user reloads. If the job isn't in memory, it's rehydrated from `job_manifest.json` (see [JobStore._load_job_from_disk](../src/backend/product_server.py#L393-L454)). 404 if unknown.

### `GET /api/jobs/{id}/status`

Poll this every 1–2 seconds while a job is running.

```json
{
  "job_id": "job_...",
  "status": "running",
  "stage": "Running first pass",
  "progress_percent": 38,
  "message": "Running first pass",
  "run_mode": "fast",
  "selection_mode": "outline",
  "selection": "1-3",
  "engines": { "primary": "mineru", "fallback": "paddle" },
  "cascade_attempt": 0,
  "failed_pages_count": null,
  "log_tail": "INFO ParseAgent ...",
  "returncode": null,
  "started_at": "2026-04-10T12:04:10Z",
  "finished_at": null,
  "cancel_requested": false,
  "artifacts": {
    "document_ir.json": "/api/jobs/<id>/artifact/document_ir.json",
    "document.md":      "/api/jobs/<id>/artifact/document.md"
  }
}
```

Notes:
- `stage` / `progress_percent` step through `8 → 38 → 68 → 82 → 92 → 100` as intermediate files appear.
- `log_tail` is the last non-empty line of stderr (then stdout), capped at 280 chars.
- `cascade_attempt` and `failed_pages_count` are read live from `pipeline_state.json` and `validation_report.json`.
- `artifacts` only contains keys for files that already exist — don't assume all four are present.

### `POST /api/jobs/{id}/run`

Starts the pipeline. The job must not already be `running`.

**Body**

```json
{
  "selection_mode": "all | outline | pagerange",
  "selection":      "1-3,5,8-12",
  "run_mode":       "fast | reliable",
  "output_dir":     "optional, defaults to <job_dir>/runs"
}
```

| Field            | Default                          | Notes                                              |
| ---------------- | -------------------------------- | -------------------------------------------------- |
| `selection_mode` | `"all"`                          | Forwarded to `pipeline_graph --selection-mode`     |
| `selection`      | `null`                           | Range expression for `pagerange`/`outline` modes   |
| `run_mode`       | previous run's mode, else `fast` | `fast` = primary only; `reliable` = + cascade      |
| `output_dir`     | `<job_dir>/runs`                 | Final path is `<output_dir>/run_<ts>_<mode>_<hex>/output` |

**Response `202`** — status snapshot, `status="running"`. Start polling immediately.

**Errors:** 400 (bad JSON / unknown `run_mode`), 404 (no such job), 409 (a run is already in progress).

### `POST /api/jobs/{id}/cancel`

No body. Returns `202` with the snapshot (`cancel_requested=true`, `stage="Canceling"`). Once the subprocess tree is reaped the next `/status` will report `status="canceled"`. Returns `409` if nothing is running.

### `GET /api/jobs/{id}/page-preview?page=<1-based>`

Per-page summary read from `document_ir.json`. Used for the right-hand markdown panel.

```json
{
  "page_number": 3,
  "page_index": 2,
  "in_document_ir": true,
  "block_count": 17,
  "block_types": { "text": 12, "table": 1, "figure": 2, "figure_title": 2 },
  "source_engine": "mineru",
  "page_markdown": "## 2.1 Methods\n\n...",
  "visual_content_detected": true,
  "visual_hint": "This page is mainly visual. Use the page preview as the primary reference."
}
```

If the IR doesn't exist yet, the same shape comes back with `in_document_ir=false`, `block_count=null`, `page_markdown=""`. 400 if `page` is missing/invalid.

### `GET /api/jobs/{id}/thumb/{filename}`

Serves a pre-rendered thumbnail (e.g. `page_0001.jpg`) from `ingestion/thumbnails/`. `Cache-Control: public, max-age=3600`. 404 if missing.

### `GET /api/jobs/{id}/preview/page_NNNN.jpg`

Serves a high-res page image. Rendered lazily at 150 DPI by `pypdfium2` on the first request and cached to `ingestion/previews/`. 400 (bad name), 404 (page out of range), 500 (render failure).

### `GET /api/jobs/{id}/artifact/{name}`

Whitelist of 4 artifacts (see [_serve_artifact](../src/backend/product_server.py#L951-L967)):

| `name`                   | Content-Type     |
| ------------------------ | ---------------- |
| `document_ir.json`       | application/json |
| `document.md`            | text/markdown    |
| `validation_report.json` | application/json |
| `pipeline_state.json`    | application/json |

404 if the name isn't whitelisted or the file hasn't been produced yet.

---

## 4. Data models

Defined with Pydantic v2 in [src/backend/types.py](../src/backend/types.py).

**DocumentIR**

```jsonc
{
  "doc_id": "string",
  "source_file": "string",
  "source_engine": "mineru | paddle",
  "generated_at": "ISO-8601",
  "pages": [{
    "page_index": 0,
    "width": 612,
    "height": 792,
    "blocks": [{
      "id": "p0_b0",
      "type": "text | title | table | figure | figure_title | image | image_caption | ...",
      "text": "string",
      "bbox": [x0, y0, x1, y1],
      "order": 0,
      "confidence": 0.97,
      "source": { "engine": "mineru", "raw_type": "..." },
      "page_index": 0
    }]
  }]
}
```

**ValidationReport**

```jsonc
{
  "empty_page_rate": 0.02,
  "order_anomaly_rate": 0.0,
  "table_anomaly_rate": 0.05,
  "coverage_rate": 0.98,
  "non_blank_pages": 40,
  "pages_with_content": 41,
  "empty_pages": 1,
  "anomalous_order_pages": 0,
  "total_tables": 12,
  "anomalous_tables": 1,
  "failed_pages": [17],
  "pass_quality_floor": true
}
```

`failed_pages` drives cascade repair; `pass_quality_floor` is the final gate against `configs/quality_floor.yaml`.

**pipeline_state.json**

```jsonc
{
  "manual_review_required": false,
  "parse_error": null,
  "parse_attempt": 1,
  "rerun_attempt": 0,
  "cascade_attempt": 1,
  "cascade_active": true,
  "engine": "mineru"
}
```

---

## 5. Typical flow

```
Frontend                                     Backend
  │  POST /api/upload (file)                    │
  │ ──────────────────────────────────────────▶│  create_job → ingest (sync)
  │ ◀───────────────────────────────────── 201 │  { job_id, session }
  │                                              │
  │  POST /api/jobs/<id>/run                    │
  │   { selection_mode, selection, run_mode }   │
  │ ──────────────────────────────────────────▶│  spawn pipeline subprocess
  │ ◀───────────────────────────────────── 202 │  status=running
  │                                              │
  │  GET /api/jobs/<id>/status   (poll 1–2s)    │
  │ ──────────────────────────────────────────▶│
  │ ◀───────────────────────────────────── 200 │  status / stage / log_tail / artifacts
  │                                              │
  │  GET /api/jobs/<id>/artifact/document.md    │
  │ ──────────────────────────────────────────▶│
  │ ◀───────────────────────────────────── 200 │
```

To restore a session after a reload, just call `GET /api/jobs/<id>/session` with the saved `job_id`.

---

## 6. HTTP ↔ pipeline CLI mapping

The backend is a thin wrapper around the `pipeline_graph` CLI. To reproduce a run by hand:

| HTTP                              | CLI flag                                                       |
| --------------------------------- | -------------------------------------------------------------- |
| uploaded PDF                      | `--input <pdf>`                                                |
| (hardcoded)                       | `--engine mineru`                                              |
| `selection_mode`                  | `--selection-mode <all\|outline\|pagerange>`                   |
| `selection`                       | `--selection "<expr>"` (only when non-empty)                   |
| `output_dir`                      | `--output-dir <dir>`                                           |
| (hardcoded)                       | `--engine-config configs/engines_prod.yaml`                    |
| `max_parse_attempts=1`            | `--max-parse-attempts 1`                                       |
| `max_rerun_attempts=0`            | `--max-rerun-attempts 0`                                       |
| `run_mode == "reliable"`          | `--cascade-enabled --cascade-engine paddle --cascade-engine-config configs/engines_prod_vlm_repair.yaml --max-cascade-attempts 1` |

Exit code mapping: `0 → completed`, non-zero → `failed`, killed by signal → `canceled`.

---

## 7. Known limitations / handover TODO

1. **No auth, no rate limiting.** Anyone who can reach the port can upload PDFs and trigger runs. Put it behind a reverse proxy before exposing it.
2. **Synchronous ingestion.** `/api/upload` blocks while thumbnails render — slow for large PDFs. Consider moving ingestion onto the background thread with a new `status="ingesting"`.
3. **In-process job registry.** `JobStore` only caches in memory; restarting the server loses tracking of any in-flight subprocesses (manifests are still on disk, but the run isn't supervised anymore).
4. **Hardcoded engines.** Primary/cascade engines and config paths live on `JobRecord` ([product_server.py:210-216](../src/backend/product_server.py#L210-L216)). Switching tracks (A VLM vs B lightweight) currently requires a code change. Easy fix: expose `engine_config` as an optional `/run` field.
5. **`cgi.FieldStorage` is removed in Python 3.13.** The upload handler needs to move to `python-multipart` or `email.parser` before upgrading.
6. **Polling only.** No SSE / WebSocket. If you have many concurrent jobs, an SSE stream from `/status` would be cheaper.
7. **`run_history.jsonl` grows forever.** Add external log rotation if this runs long-term.

---

## 8. Files worth reading first

| File | What's in it |
| ---- | ------------ |
| [src/backend/product_server.py](../src/backend/product_server.py)     | HTTP server, job state machine, subprocess supervision |
| [src/backend/pipeline_graph.py](../src/backend/pipeline_graph.py)     | LangGraph pipeline + CLI entrypoint                    |
| [src/backend/ingestion_agent.py](../src/backend/ingestion_agent.py)   | Page count, outline, thumbnail rendering               |
| [src/backend/parse_agent.py](../src/backend/parse_agent.py)           | Runs engines via `docker run`                          |
| [src/backend/ir_builder_agent.py](../src/backend/ir_builder_agent.py) | Engine output → `DocumentIR`                           |
| [src/backend/validation_agent.py](../src/backend/validation_agent.py) | Produces `ValidationReport`                            |
| [src/backend/markdown_export.py](../src/backend/markdown_export.py)   | `DocumentIR` → markdown                                |
| [src/backend/types.py](../src/backend/types.py)                       | Shared Pydantic models                                 |
| [configs/engines_prod.yaml](../configs/engines_prod.yaml)             | Primary engine config (`fast` mode)                    |
| [configs/engines_prod_vlm_repair.yaml](../configs/engines_prod_vlm_repair.yaml) | Cascade engine config (`reliable` mode)      |

> Last updated: 2026-04-10. Update §7 when handing over.
