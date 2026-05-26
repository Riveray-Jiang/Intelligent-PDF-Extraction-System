from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from typing import Protocol


class RunInsightsJob(Protocol):
    def artifact_paths(self) -> dict[str, Path]:
        ...


def read_run_insights(job: RunInsightsJob) -> dict[str, Any]:
    pipeline_state = job.artifact_paths()["pipeline_state.json"]
    validation_report = job.artifact_paths()["validation_report.json"]
    has_openai_key = bool(os.getenv("OPENAI_API_KEY", "").strip())
    cascade_attempt = None
    failed_pages_count = None
    image_agent: dict[str, Any] = {
        "enabled": has_openai_key,
        "name": "Image Agent",
        "model": "gpt-4o" if has_openai_key else None,
        "image_pages_detected": 0,
        "image_pages_enriched": 0,
        "image_pages_failed": 0,
    }

    if pipeline_state.exists():
        try:
            pipeline_payload = json.loads(pipeline_state.read_text(encoding="utf-8"))
            cascade_attempt = pipeline_payload.get("cascade_attempt")
            raw_image_agent = pipeline_payload.get("image_agent")
            if isinstance(raw_image_agent, dict):
                image_agent.update(raw_image_agent)
                image_agent["name"] = "Image Agent"
        except (OSError, json.JSONDecodeError):
            cascade_attempt = None
    if validation_report.exists():
        try:
            failed_pages = json.loads(validation_report.read_text(encoding="utf-8")).get("failed_pages") or []
            failed_pages_count = len(failed_pages)
        except (OSError, json.JSONDecodeError):
            failed_pages_count = None

    return {
        "cascade_attempt": cascade_attempt,
        "failed_pages_count": failed_pages_count,
        "image_agent": image_agent,
    }
