# PDF Extraction Agent System (Python)

Local multi-agent PDF extraction system with LangGraph orchestration and engine adapters for PaddleOCR-VL and MinerU.

## Quick Start
Prerequisites:
- Python 3.11
- Node.js 20+
- Docker Desktop, required for real Paddle/MinerU parsing

Backend setup and test:
```powershell
cd C:\Users\lj448\dev\pdf-extraction-agent-system
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\run_product_server.ps1
```

Frontend setup:
```powershell
cd C:\Users\lj448\dev\pdf-extraction-agent-system\frontend
npm install
npm run lint
npm run build
npm run dev -- --host 0.0.0.0 --port 5173
```

Open `http://127.0.0.1:5173/`. The frontend defaults to `http://127.0.0.1:8892` for the backend; override with `frontend/.env` if needed:
```text
VITE_BACKEND_URL=http://127.0.0.1:8892
```

Optional Visual Agent support:
```text
OPENAI_API_KEY=<your key>
```

## Current Status
- Phase 1 complete: project skeleton, configs, Docker baseline, and benchmark manifest.
- Phase 2 complete: all five agents implemented, adapters implemented, graph wiring enabled, and unit tests passing.
- Phase 3 in progress: ParseAgent now executes retry profiles via `docker run` and ingests JSON outputs.

## Pipeline
- `ingest` -> `select` -> `parse` -> `build_ir` -> `enrich_visual` -> `validate`
- CLI entrypoint:
  - `python -m backend.pipeline_graph --input <pdf> --engine <paddle|mineru> --selection-mode <all|outline|pagerange> --selection "<expr>" --output-dir <dir> --max-parse-attempts 2 --max-rerun-attempts 1`
  - Dev-only mock parsing: append `--allow-mock-parse`
  - If `--selection-mode outline` is requested but the PDF has no outline/bookmarks, the system falls back to `all`.
- Host requirement for real parsing: Docker CLI available on `PATH` and target engine image built/pulled.
- Windows/WSL note: on Windows host this flow assumes Docker Desktop (`docker.exe`). If running from WSL Linux Python, ensure paths are Linux-visible (for example `/mnt/c/...`) and use Linux Docker CLI.

## Visual Agent
- `Visual Agent` automatically enriches visual-heavy pages such as diagrams, workflows, maps, and other non-text visuals.
- Enable it by setting `OPENAI_API_KEY` in the backend environment before starting the product server or pipeline.
- It only runs on pages that already contain visual block types from the base parser, and failures fall back to the normal non-LLM output.

## Benchmark (Phase 4)
- Run benchmark:
  - `python -m backend.benchmark_runner --manifest benchmarks/benchmark_set.yaml --engines paddle,mineru --output-dir reports/benchmark_run_001`
- Freeze calibrated runtime thresholds to config:
  - append `--freeze-thresholds --quality-config configs/quality_floor.yaml`
  - Guardrail: freeze is skipped automatically if no successful warm runs are available (all runs parse-failed or manual-review).
- Main outputs:
  - `benchmark_summary.md`
  - `quality_floor_baseline.md`
  - `engine_decision.md`

## MinerU Fast Upgrade Benchmark
- Purpose: decide whether `Fast` should move from `MinerU 2.7.6 pipeline` to `MinerU 3.x pipeline / hybrid` using local A6000 measurements.
- Build the MinerU 3.1 probe image and run the full A/B/C/D matrix. Default is warm-only; add `-ColdRuns 1` only when explicitly measuring service/model startup cost:
  - `.\scripts\run_mineru_fast_upgrade_benchmark.ps1 -BuildMinerU31 -OutputDir reports/mineru_fast_upgrade_v2`
- Dry-run manifest/config validation without invoking Docker:
  - `python -m backend.mineru_fast_upgrade_benchmark --manifest benchmarks/mineru_fast_upgrade_v2.yaml --dry-run`
- Main outputs:
  - `benchmark_summary.md`
  - `benchmark_candidate_rows.json`
  - `benchmark_doc_rows.json`
  - `benchmark_overview.json`

## Paper Tracks (Fair Comparison)
- Track A (VLM vs VLM):
  - Paddle: `doc_parser`
  - MinerU: `hybrid-auto-engine`
  - Config: `configs/engines_track_a_vlm.yaml`
- Track B (Lightweight vs Lightweight):
  - Paddle: `pp_structurev3`
  - MinerU: `pipeline`
  - Config: `configs/engines_track_b_lightweight.yaml`

- Manifests:
  - `benchmarks/benchmark_paper_10pages.yaml`
  - `benchmarks/benchmark_paper_50pages.yaml`
  - `benchmarks/benchmark_paper_278pages.yaml`

- Example:
  - `python -m backend.benchmark_runner --manifest benchmarks/benchmark_paper_10pages.yaml --engines paddle,mineru --engine-config configs/engines_track_b_lightweight.yaml --output-dir reports/paper_track_b_light_10p --max-parse-attempts 1 --max-rerun-attempts 0`

## Key Directories
- `src/backend/`: agents, graph, adapters, types
- `configs/`: engine retries and quality floor config
- `benchmarks/`: benchmark manifest
- `docker/`: Paddle and MinerU images + compose
- `reports/`: generated outputs and benchmark reports

## Production Preset
- Primary engine: `MinerU pipeline`
- Fallback engine: `Paddle pp_structurev3`
- Fixed config: `configs/engines_prod.yaml`
- One-command PowerShell entrypoint:
  - `.\scripts\run_prod_pipeline.ps1 -InputPdf "<pdf>" -OutputDir "<output_dir>"`
  - Example with page subset:
    - `.\scripts\run_prod_pipeline.ps1 -InputPdf "<pdf>" -OutputDir reports/prod_run_001 -SelectionMode pagerange -Selection "1-50"`
