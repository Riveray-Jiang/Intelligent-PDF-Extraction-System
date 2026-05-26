from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


IMAGE_AGENT_CACHE_VERSION = 11


class ImageAgentCacheJob(Protocol):
    job_dir: Path


def image_agent_cache_path(job: ImageAgentCacheJob) -> Path:
    return job.job_dir / "image_agent_cache.json"


def legacy_image_agent_cache_path(output_dir: Path) -> Path:
    return output_dir / "image_agent_cache.json"


def _load_image_agent_cache_payload(cache_path: Path) -> dict | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != IMAGE_AGENT_CACHE_VERSION:
        return None
    return payload if isinstance(payload, dict) else None


def _load_image_agent_cache_record_from_path(
    cache_path: Path,
    page_number: int,
) -> dict | None:
    payload = _load_image_agent_cache_payload(cache_path)
    if payload is None:
        return None
    pages = payload.get("pages")
    if not isinstance(pages, dict):
        return None
    record = pages.get(str(page_number))
    return record if isinstance(record, dict) else None


def load_image_agent_cache_record(
    job: ImageAgentCacheJob,
    page_number: int,
    *,
    output_dir: Path | None = None,
) -> dict | None:
    cache_path = image_agent_cache_path(job)
    record = _load_image_agent_cache_record_from_path(cache_path, page_number)
    if record is not None:
        return record

    if output_dir is None:
        return None

    legacy_cache_path = legacy_image_agent_cache_path(output_dir)
    record = _load_image_agent_cache_record_from_path(legacy_cache_path, page_number)
    if record is None:
        return None
    save_image_agent_cache_record(job, page_number, record)
    return record


def save_image_agent_cache_record(
    job: ImageAgentCacheJob,
    page_number: int,
    record: dict,
) -> None:
    cache_path = image_agent_cache_path(job)
    payload: dict = {"version": IMAGE_AGENT_CACHE_VERSION, "pages": {}}
    existing = _load_image_agent_cache_payload(cache_path)
    if existing is not None:
        payload = existing

    pages = payload.get("pages")
    if not isinstance(pages, dict):
        pages = {}
        payload["pages"] = pages
    payload["version"] = IMAGE_AGENT_CACHE_VERSION
    pages[str(page_number)] = record
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
