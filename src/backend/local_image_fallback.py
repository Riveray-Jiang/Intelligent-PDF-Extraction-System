from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .types import Page


class LocalImageFallbackJob(Protocol):
    job_dir: Path


def apply_local_image_fallback(
    job: LocalImageFallbackJob,
    output_dir: Path,
    page_number: int,
    page: Page,
) -> Page:
    # Product repair uses the dedicated MinerU2.5-Pro repair path. Keep this
    # hook inert so preview/merged output is not silently changed by Paddle.
    return page
