from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4


def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def parse_utc(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def compute_duration_sec(started_at: str | None, finished_at: str | None) -> float | None:
    started = parse_utc(started_at)
    finished = parse_utc(finished_at)
    if started and finished:
        return round((finished - started).total_seconds(), 2)
    return None


def make_job_id() -> str:
    return f"job_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"


def make_run_id(run_mode: str) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{run_mode}_{uuid4().hex[:6]}"


def sanitize_filename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", ".", " ")).strip()
    cleaned = cleaned.replace(" ", "_")
    return cleaned or "input.pdf"
