from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_job_manifest(job_id: str, *, data_root: Path) -> dict[str, Any] | None:
    manifest_path = data_root / job_id / "job_manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    manifest["job_id"] = str(manifest.get("job_id") or job_id)
    manifest["document_id"] = str(manifest.get("document_id") or manifest["job_id"])
    manifest["file_version"] = int(manifest.get("file_version") or 1)
    manifest["replaces_job_id"] = manifest.get("replaces_job_id")
    return manifest


def read_document_job_manifests(document_id: str, *, data_root: Path) -> list[dict[str, Any]]:
    if not data_root.exists():
        return []

    manifests: list[dict[str, Any]] = []
    for job_dir in data_root.iterdir():
        if not job_dir.is_dir():
            continue
        manifest = load_job_manifest(job_dir.name, data_root=data_root)
        if manifest is None:
            continue
        if str(manifest.get("document_id") or manifest.get("job_id")) != document_id:
            continue
        manifests.append(manifest)

    manifests.sort(
        key=lambda item: (
            int(item.get("file_version") or 1),
            str(item.get("created_at") or ""),
        ),
        reverse=True,
    )
    return manifests
