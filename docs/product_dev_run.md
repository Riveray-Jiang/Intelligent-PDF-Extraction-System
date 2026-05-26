# Product Development Runbook

This runbook is for local product development and demos. It assumes Windows
PowerShell, Docker Desktop, Python 3.11, and Node.js 20+.

## Start Backend

From the repository root:

```powershell
.\scripts\run_product_server.ps1
```

Default backend URL:

```text
http://127.0.0.1:8892
```

The backend prewarms the parser services from:

- `configs/engines_prod.yaml`
- `configs/engines_prod_repair.yaml`

Expected listening ports:

- `8892`: product API
- `19100`: MinerU fast service
- `19103`: MinerU repair service

Check:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8892,19100,19103
Invoke-RestMethod http://127.0.0.1:8892/api/health
```

## Start Frontend

In another terminal:

```powershell
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Open:

```text
http://127.0.0.1:5173/
```

The Vite dev server uses `http://127.0.0.1:8892` by default. To override it,
create `frontend/.env`:

```text
VITE_BACKEND_URL=http://127.0.0.1:8892
```

## Product Smoke Test

After a backend refactor or demo setup change, run:

1. Upload a small PDF.
2. Select one page.
3. Run `fast`.
4. Wait for `completed`.
5. Open page preview.
6. Run `reliable` on the same page.
7. Confirm the page preview now resolves to the repair run.
8. Open `RUNS`; expect at least two runs.
9. Open file history; expect `has_output=true`.
10. Download merged output zip.

API-only checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8892/api/health
Invoke-WebRequest http://127.0.0.1:5173/ -UseBasicParsing
.\.venv\Scripts\python.exe -m pytest
```

## Key API Routes

- `POST /api/upload`
- `GET /api/jobs/<job_id>/session`
- `GET /api/jobs/<job_id>/status`
- `GET /api/jobs/<job_id>/runs`
- `GET /api/jobs/<job_id>/file-history`
- `POST /api/jobs/<job_id>/run`
- `POST /api/jobs/<job_id>/cancel`
- `POST /api/jobs/<job_id>/image-agent`
- `GET /api/jobs/<job_id>/page-preview?page=<n>`
- `GET /api/jobs/<job_id>/download-output.zip`

See [backend_api.md](backend_api.md) for request and response details.

## Runtime Data

Runtime output is local-only:

- `data/jobs/<job_id>/`
- `data/run_history.jsonl`
- `reports/`
- `tmp/`
- `.runtime_cache/`
- `.runtime_logs/`

These paths are ignored by Git. Keep only curated benchmark manifests and
documentation assets in the repository.

## Image Agent

If `.env` contains `OPENAI_API_KEY`, the Image Agent endpoint is enabled.

Do not trigger Image Agent during sensitive-document testing unless the document
is allowed to be sent to OpenAI.

## Troubleshooting

If the frontend loads but upload fails:

```powershell
Invoke-RestMethod http://127.0.0.1:8892/api/health
Get-NetTCPConnection -State Listen -LocalPort 5173,8892
```

If parser runs hang at startup, check parser services:

```powershell
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

The backend restarts stale running parser containers when the container is up
but its health endpoint is not responding.
