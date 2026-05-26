<p align="center">
  <img src="docs/assets/logo.svg" alt="PDF Extraction logo" width="72">
</p>

# Intelligent PDF Extraction System

Local-first PDF extraction workspace for environmental documents. The product
lets a user upload a PDF, select pages, run fast extraction, repair selected
pages with a stronger parser profile, inspect evidence page by page, and export
structured artifacts for downstream review.

The repository is maintained as product engineering code. Runtime jobs,
benchmark outputs, and ad-hoc experiment files stay local unless they are
curated into fixtures, benchmark manifests, or product documentation.

![Product UI](docs/assets/product-ui.png)

## Product Scope

- Upload-first review UI for PDF extraction jobs.
- Page selection by all pages, outline, or manual page range.
- Fast extraction with the production MinerU service profile.
- Reliable page repair with the MinerU 2.5 Pro repair profile.
- Merged final output across fast and repair runs.
- Page-level preview, markdown, block counts, source run provenance, and run
  history.
- Optional Image Agent for image-heavy pages when `OPENAI_API_KEY` is set.
- Benchmark manifests for parser comparison and regression investigation.

## Architecture

```text
frontend/ React app
  -> src/backend/product_server.py      HTTP API and local product server
  -> src/backend/job_store.py           job state, manifests, subprocess runs
  -> src/backend/pipeline_graph.py      extraction pipeline orchestration
  -> src/backend/parse_agent.py         Docker-backed parser execution
  -> src/backend/ir_builder_agent.py    parser output -> DocumentIR
  -> src/backend/validation_agent.py    quality checks and repair signals
```

Important backend helper modules:

- `output_planner.py`: effective output selection across fast and repair runs
- `merged_output.py`: merged `document_ir.json`, markdown, and zip output
- `file_history.py`: file-version and run-history payloads
- `document_artifacts.py`: artifact paths, page models, and markdown helpers
- `engine_service_manager.py`: Docker service prewarm and stale-container repair

## Quick Start

Prerequisites:

- Python 3.11
- Node.js 20+
- Docker Desktop
- PowerShell on Windows

Backend:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\run_product_server.ps1
```

The backend listens on:

```text
http://127.0.0.1:8892
```

Frontend:

```powershell
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Open:

```text
http://127.0.0.1:5173/
```

If PowerShell blocks `npm.ps1`, use `npm.cmd`:

```powershell
npm.cmd install
npm.cmd run dev -- --host 0.0.0.0 --port 5173
```

The frontend defaults to `http://127.0.0.1:8892`. Override it with
`frontend/.env`:

```text
VITE_BACKEND_URL=http://127.0.0.1:8892
```

## Product Flow

1. Start the backend and frontend.
2. Upload a PDF.
3. Select pages.
4. Run `fast` extraction.
5. Inspect page previews and artifacts.
6. Run `reliable` repair for pages that need a stronger parser profile.
7. Download merged output.

Generated artifacts:

- `document_ir.json`: schema-agnostic document intermediate representation
- `document.md`: markdown export for review
- `validation_report.json`: quality and failed-page report
- `pipeline_state.json`: pipeline metadata and timing context
- `performance_profile.json`: node-level timing profile when produced

## Smoke Test Checklist

Use this after refactors or demo setup changes:

```powershell
Invoke-RestMethod http://127.0.0.1:8892/api/health
Invoke-WebRequest http://127.0.0.1:5173/ -UseBasicParsing
Get-NetTCPConnection -State Listen -LocalPort 5173,8892,19100,19103
.\.venv\Scripts\python.exe -m pytest
```

Manual product smoke:

- upload a small PDF
- run one-page `fast`
- run one-page `reliable`
- open page preview
- check `/runs` and `/file-history`
- download `download-output.zip`

## Runtime Data Policy

Tracked and useful:

- `configs/*.yaml`: parser and quality profiles
- `benchmarks/*.yaml`: reproducible benchmark manifests
- `tests/`: behavior and regression coverage
- `docs/assets/`: curated product documentation assets

Local-only and ignored:

- `data/jobs/`: uploaded PDFs, manifests, run outputs
- `data/benchmarks/`: benchmark output data
- `reports/`: ad-hoc experiment output
- `tmp/`: smoke-test downloads and screenshots
- `.runtime_cache/`, `.runtime_logs/`: local parser caches and logs

Do not commit raw PDFs, generated parser output, benchmark result folders, or
temporary smoke-test artifacts unless they have been deliberately reduced into a
small fixture or documented benchmark input.

See [docs/data_and_benchmarks.md](docs/data_and_benchmarks.md) for the retention
rules.

## Image Agent

Image Agent is optional. It is enabled when `.env` contains:

```text
OPENAI_API_KEY=<your key>
```

When enabled, Image Agent can interpret maps, figures, stamps, diagrams, charts,
and other image-heavy pages. It sends rendered page imagery to the configured
OpenAI endpoint, so use it only when the document can leave the local machine.

## CLI Pipeline

Run the pipeline directly:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m backend.pipeline_graph `
  --input "<pdf>" `
  --engine mineru `
  --selection-mode all `
  --output-dir "reports/run_001" `
  --engine-config configs/engines_prod.yaml `
  --max-parse-attempts 1 `
  --max-rerun-attempts 0
```

Production wrapper:

```powershell
.\scripts\run_prod_pipeline.ps1 -InputPdf "<pdf>" -OutputDir "reports/prod_run_001"
```

Page subset:

```powershell
.\scripts\run_prod_pipeline.ps1 `
  -InputPdf "<pdf>" `
  -OutputDir "reports/prod_run_001" `
  -SelectionMode pagerange `
  -Selection "1-50"
```

## Benchmarks

Benchmark manifests are retained because they describe reproducible comparisons.
Their generated outputs are ignored.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m backend.benchmark_runner `
  --manifest benchmarks/benchmark_set.yaml `
  --engines mineru `
  --output-dir reports/benchmark_run_001
```

MinerU fast-upgrade dry run:

```powershell
.\.venv\Scripts\python.exe -m backend.mineru_fast_upgrade_benchmark `
  --manifest benchmarks/mineru_fast_upgrade_v2.yaml `
  --dry-run
```

## Repository Layout

- `src/backend/`: product server, pipeline agents, parser adapters, exports
- `frontend/`: React + TypeScript review workspace
- `configs/`: parser service profiles and quality thresholds
- `benchmarks/`: reproducible benchmark manifests
- `docker/`: parser runtime images and compose config
- `docs/`: product, API, operations, and data-retention notes
- `tests/`: backend regression tests

## Validation

Current baseline:

```powershell
.\.venv\Scripts\python.exe -m pytest
npm.cmd run lint
npm.cmd run build
```

`ruff` is not installed in the checked-in virtual environment by default. Use
`git diff --check` for whitespace checks unless a lint environment is added.
