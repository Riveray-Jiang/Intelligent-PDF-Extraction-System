from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def default_selection_mode(ingestion_output: dict[str, Any]) -> str:
    return "all"


def build_pipeline_command(
    *,
    input_pdf: Path,
    output_dir: Path,
    selection_mode: str,
    selection: str | None,
    run_mode: str,
    engine: str,
    engine_config: Path,
    cascade_engine: str,
    cascade_engine_config: Path,
    max_parse_attempts: int,
    max_rerun_attempts: int,
    max_cascade_attempts: int,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "backend.pipeline_graph",
        "--input",
        str(input_pdf),
        "--engine",
        engine,
        "--selection-mode",
        selection_mode,
        "--output-dir",
        str(output_dir),
        "--engine-config",
        str(engine_config),
        "--max-parse-attempts",
        str(max_parse_attempts),
        "--max-rerun-attempts",
        str(max_rerun_attempts),
    ]
    if selection:
        command.extend(["--selection", selection])
    return command
