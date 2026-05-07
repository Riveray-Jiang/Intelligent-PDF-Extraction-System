# Product Dev Run

## Backend

```powershell
cd C:\Users\lj448\dev\pdf-extraction-agent-system
.\scripts\run_product_server.ps1
```

Default backend URL:

- `http://127.0.0.1:8892`

Key product API routes:

- `POST /api/upload`
- `GET /api/jobs/<job_id>/session`
- `GET /api/jobs/<job_id>/status`
- `POST /api/jobs/<job_id>/run`
- `GET /api/jobs/<job_id>/thumb/<file>`
- `GET /api/jobs/<job_id>/preview/<file>`
- `GET /api/jobs/<job_id>/page-preview?page=<n>`
- `GET /api/jobs/<job_id>/artifact/<name>`

Runtime data is stored in:

- `data/jobs/<job_id>/`

## Frontend

```powershell
cd C:\Users\lj448\dev\pdf-extraction-agent-system\frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Frontend URL:

- `http://127.0.0.1:5173/`

The Vite dev server proxies `/api/*` to `http://127.0.0.1:8892`.

## Product Flow

1. Open the frontend.
2. Upload a PDF.
3. Wait for the document review desk to load for the new `job_id`.
4. Choose:
   - `All pages`
   - `Outline sections`
   - `Manual page range`
5. Run the pipeline.
6. Inspect:
   - page previews
   - `document_ir.json`
   - `document.md`
   - `validation_report.json`
   - `pipeline_state.json`

## Current Product Defaults

- Primary parser: `MinerU`
- Fallback path: `Paddle`
- Runtime strategy: upload-first, job-based, HITL review before parse
