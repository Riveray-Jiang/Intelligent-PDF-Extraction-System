# Backend API

Service: `PDFProductServer/0.1`

Default bind: `http://127.0.0.1:8892`

Primary module: [src/backend/product_server.py](../src/backend/product_server.py)

The backend is a local `ThreadingHTTPServer` API for the PDF extraction product.
It handles upload, synchronous ingestion, background extraction runs, status
polling, output previews, run history, merged output, and artifact download.

There is no authentication. Keep the service on localhost unless it is placed
behind a proper reverse proxy and access-control layer.

## Runtime Components

| Module | Responsibility |
| --- | --- |
| `product_server.py` | HTTP routing and request/response handling |
| `job_store.py` | job manifests, in-memory job cache, subprocess lifecycle |
| `pipeline_command.py` | product-server to pipeline CLI command builder |
| `output_planner.py` | effective output selection across fast and repair runs |
| `merged_output.py` | merged final IR, markdown, and zip output |
| `file_history.py` | file-version and run-history payloads |
| `pipeline_graph.py` | extraction pipeline orchestration |

## Disk Layout

Each upload creates a job under `data/jobs/<job_id>/`:

```text
data/jobs/job_<timestamp>_<hex>/
  <uploaded>.pdf
  job_manifest.json
  ingestion/
    ingestion_output.json
    thumbnails/page_*.jpg
    previews/page_*.jpg
  last_run_stdout.log
  last_run_stderr.log
  runs/
    run_<timestamp>_<mode>_<hex>/
      stdout.log
      stderr.log
      output/
        document_ir.json
        document.md
        validation_report.json
        pipeline_state.json
        parse/<engine>/...
```

Run history is appended to `data/run_history.jsonl`.

`data/jobs/`, `data/benchmarks/`, `reports/`, and `tmp/` are runtime output and
are ignored by Git.

## Job Lifecycle

| Status | Meaning |
| --- | --- |
| `preparing` | PDF saved and ingestion is running |
| `ready` | ingestion completed and the job can be run |
| `running` | extraction subprocess is active |
| `completed` | subprocess exited with code 0 |
| `failed` | subprocess failed or crashed before startup |
| `canceled` | user requested cancellation and the process tree was stopped |

`stage` and `progress_percent` are derived from the job state and output files.
The pipeline is polled; it does not stream progress.

## Run Modes

| Mode | Product behavior | Config |
| --- | --- | --- |
| `fast` | production MinerU service parser | `configs/engines_prod.yaml` |
| `reliable` | stronger MinerU 2.5 Pro repair profile for selected pages | `configs/engines_prod_repair.yaml` |

The product server prevents duplicate fast runs for pages that already have fast
output and only allows reliable repair for pages already present in the current
merged output.

## Endpoint Summary

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | liveness probe |
| `POST` | `/api/upload` | upload PDF and create a job |
| `GET` | `/api/jobs/{job_id}/session` | restore full frontend session |
| `GET` | `/api/jobs/{job_id}/status` | current job status snapshot |
| `GET` | `/api/jobs/{job_id}/runs` | completed run history for the job |
| `GET` | `/api/jobs/{job_id}/file-history` | file-version and merged-output summary |
| `POST` | `/api/jobs/{job_id}/run` | start fast or reliable extraction |
| `POST` | `/api/jobs/{job_id}/cancel` | cancel active extraction |
| `POST` | `/api/jobs/{job_id}/image-agent` | generate page-level image interpretation |
| `GET` | `/api/jobs/{job_id}/page-preview?page=N` | page markdown and IR preview |
| `GET` | `/api/jobs/{job_id}/thumb/{filename}` | pre-rendered thumbnail |
| `GET` | `/api/jobs/{job_id}/preview/{filename}` | high-resolution page preview |
| `GET` | `/api/jobs/{job_id}/artifact/{name}` | latest run artifact |
| `GET` | `/api/jobs/{job_id}/runs/{run_id}/artifact/{name}` | artifact from a specific run |
| `GET` | `/api/jobs/{job_id}/merged-artifact/{name}` | merged final `document.md` or `document_ir.json` |
| `GET` | `/api/jobs/{job_id}/download-output.zip` | merged output bundle |
| `OPTIONS` | any path | CORS preflight |

## Endpoints

### `GET /api/health`

Returns:

```json
{"ok": true}
```

### `POST /api/upload`

Accepts either:

- `multipart/form-data` with a `file` field
- raw `application/pdf` or `application/octet-stream`

Optional multipart field:

- `replaces_job_id`: creates a new file version in the same document family

Response `201`:

```json
{
  "job_id": "job_20260526_165227_2fd470bc",
  "session": {
    "job_id": "job_20260526_165227_2fd470bc",
    "document_id": "job_20260526_165227_2fd470bc",
    "file_version": 1,
    "input_pdf_name": "demo.pdf",
    "page_count": 1,
    "default_selection_mode": "all",
    "pages": [{"page_index": 0}],
    "outline": [],
    "job": {}
  }
}
```

Errors:

- `400`: missing body, empty file, or unsupported content type
- `500`: ingestion failed

Ingestion is synchronous. Large PDFs can make upload slow because thumbnails and
page metadata are generated before the response is returned.

### `GET /api/jobs/{job_id}/session`

Returns the same session shape as upload. If the job is not in memory, the
server attempts to rehydrate it from `job_manifest.json` and
`ingestion/ingestion_output.json`.

### `GET /api/jobs/{job_id}/status`

Returns the current status snapshot:

```json
{
  "job_id": "job_...",
  "status": "running",
  "message": "Extracting selected pages.",
  "stage": "Running fast",
  "progress_percent": 38,
  "output_dir": "data/jobs/.../runs/run_.../output",
  "run_id": "run_...",
  "selection_mode": "pagerange",
  "selection": "1",
  "run_mode": "fast",
  "returncode": null,
  "started_at": "2026-05-26T16:52:28Z",
  "finished_at": null,
  "duration_sec": null,
  "cancel_requested": false,
  "log_tail": null,
  "engines": {"primary": "mineru"},
  "cascade_attempt": 0,
  "failed_pages_count": 0,
  "image_agent": {
    "enabled": false,
    "name": "Image Agent",
    "model": null
  },
  "artifacts": {}
}
```

Poll every 1-2 seconds while `status` is `running`.

### `GET /api/jobs/{job_id}/runs`

Returns recent run history:

```json
{
  "runs": [
    {
      "job_id": "job_...",
      "run_id": "run_...",
      "status": "completed",
      "run_mode": "fast",
      "selection_mode": "pagerange",
      "selection": "1",
      "resolved_pages": [1],
      "duration_sec": 6.0,
      "output_dir": "data/jobs/.../output",
      "artifact_urls": {
        "document_ir.json": "/api/jobs/<job_id>/runs/<run_id>/artifact/document_ir.json"
      }
    }
  ]
}
```

The endpoint reads `data/run_history.jsonl`, newest first.

### `GET /api/jobs/{job_id}/file-history`

Returns the known file versions for the document family and the merged output
state for each version:

```json
{
  "document_id": "doc_...",
  "current_job_id": "job_...",
  "versions": [
    {
      "job_id": "job_...",
      "file_version": 1,
      "filename": "demo.pdf",
      "is_current": true,
      "has_output": true,
      "latest_output_pages": [1],
      "effective_page_run_ids": {"1": "run_..."},
      "merged_artifact_urls": {
        "document.md": "/api/jobs/<job_id>/merged-artifact/document.md",
        "document_ir.json": "/api/jobs/<job_id>/merged-artifact/document_ir.json"
      },
      "runs": []
    }
  ]
}
```

### `POST /api/jobs/{job_id}/run`

Starts a background extraction subprocess.

Request:

```json
{
  "selection_mode": "pagerange",
  "selection": "1-3,5",
  "run_mode": "fast",
  "output_dir": "optional/root/output/path"
}
```

Fields:

| Field | Values | Default |
| --- | --- | --- |
| `selection_mode` | `all`, `outline`, `pagerange` | `all` |
| `selection` | page expression or outline ids | `null` |
| `run_mode` | `fast`, `reliable` | previous job mode or `fast` |
| `output_dir` | output root | job `runs/` directory |

Response `202` is a status snapshot with `status="running"`.

Errors:

- `400`: invalid JSON or unknown run mode
- `409`: duplicate page run, unavailable repair page, or job already running

### `POST /api/jobs/{job_id}/cancel`

Cancels the active subprocess tree and returns a status snapshot. Returns `409`
if the job is not running.

### `GET /api/jobs/{job_id}/page-preview?page=N`

Returns page-level preview data from the effective output run:

```json
{
  "run_id": "run_...",
  "page_number": 1,
  "page_index": 0,
  "in_document_ir": true,
  "block_count": 9,
  "block_types": {"text": 8, "table": 1},
  "source_engine": "mineru",
  "page_markdown": "...",
  "page_ir": {},
  "image_content_detected": false,
  "image_hint": null,
  "image_alt_text": null,
  "image_interpretation_markdown": null,
  "image_agent_language": null,
  "image_agent_kind": null,
  "image_agent_generated": false,
  "image_agent_empty": false
}
```

Optional query:

- `run_id=<run_id>`: preview a specific run instead of effective merged output

### `POST /api/jobs/{job_id}/image-agent`

Request:

```json
{
  "page": 1,
  "run_id": "optional"
}
```

Runs Image Agent for one page and returns the same image interpretation fields
used by page preview. This endpoint requires `OPENAI_API_KEY`; otherwise it
returns `409`.

Because this sends rendered page imagery to OpenAI, call it only for documents
that are allowed to leave the local machine.

### Artifact Endpoints

Whitelisted artifact names:

- `document_ir.json`
- `document.md`
- `validation_report.json`
- `pipeline_state.json`

Endpoints:

- `GET /api/jobs/{job_id}/artifact/{name}`: latest job output artifact
- `GET /api/jobs/{job_id}/runs/{run_id}/artifact/{name}`: specific run artifact
- `GET /api/jobs/{job_id}/merged-artifact/document.md`
- `GET /api/jobs/{job_id}/merged-artifact/document_ir.json`
- `GET /api/jobs/{job_id}/download-output.zip`

`download-output.zip` contains:

- `document.md`
- `document_ir.json`
- `metadata.json`
- `pages/page_XXXX.md`

### Image Preview Endpoints

- `GET /api/jobs/{job_id}/thumb/{filename}` serves pre-rendered thumbnails.
- `GET /api/jobs/{job_id}/preview/page_NNNN.jpg` renders a higher-resolution
  preview lazily with `pypdfium2` and caches it under `ingestion/previews/`.

## HTTP to Pipeline Mapping

`JobStore.start_run` builds a command equivalent to:

```powershell
python -m backend.pipeline_graph `
  --input "<uploaded.pdf>" `
  --engine mineru `
  --selection-mode pagerange `
  --selection "1" `
  --output-dir "<job>/runs/<run_id>/output" `
  --engine-config configs/engines_prod.yaml `
  --max-parse-attempts 1 `
  --max-rerun-attempts 0
```

For `run_mode="reliable"`, the command still uses `engine=mineru`, but the job
selects `configs/engines_prod_repair.yaml`, which points at the MinerU 2.5 Pro
repair service.

## Operational Notes

- Docker service prewarm happens at server startup unless `--skip-prewarm` is
  passed.
- Existing parser containers are restarted automatically when they are running
  but fail their health check.
- Parser timeouts are long-running parse limits, not product-server request
  timeouts.
- `run_history.jsonl` is append-only and should be rotated externally for long
  lived deployments.
- The in-memory job cache can rehydrate completed jobs from disk, but it cannot
  supervise subprocesses that were already running before the server restarted.

## Files Worth Reading First

| File | Purpose |
| --- | --- |
| [src/backend/product_server.py](../src/backend/product_server.py) | HTTP routes and response serving |
| [src/backend/job_store.py](../src/backend/job_store.py) | job state and subprocess lifecycle |
| [src/backend/pipeline_graph.py](../src/backend/pipeline_graph.py) | extraction pipeline |
| [src/backend/parse_agent.py](../src/backend/parse_agent.py) | parser service execution |
| [src/backend/ir_builder_agent.py](../src/backend/ir_builder_agent.py) | parser output to DocumentIR |
| [src/backend/output_planner.py](../src/backend/output_planner.py) | effective output selection |
| [src/backend/merged_output.py](../src/backend/merged_output.py) | merged final artifacts |
| [src/backend/types.py](../src/backend/types.py) | shared Pydantic models |
| [configs/engines_prod.yaml](../configs/engines_prod.yaml) | fast parser service profile |
| [configs/engines_prod_repair.yaml](../configs/engines_prod_repair.yaml) | reliable repair profile |
