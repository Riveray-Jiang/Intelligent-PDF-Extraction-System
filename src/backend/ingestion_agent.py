from __future__ import annotations

from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import yaml

from .pdfium_runtime import PDFIUM_LOCK


class IngestionAgent:
    """Render pages, collect page metadata, and extract outline if present."""

    DEFAULT_RENDER_DPI = 150
    DEFAULT_INK_RATIO_NON_BLANK_THRESHOLD = 0.0025
    DEFAULT_INK_PIXEL_THRESHOLD = 245

    def __init__(self, config_path: str | Path | None = None) -> None:
        root = Path(__file__).resolve().parents[2]
        config_path = Path(config_path) if config_path else (root / "configs" / "quality_floor.yaml")

        self.BLANK_DETECT_RENDER_DPI = self.DEFAULT_RENDER_DPI
        self.INK_RATIO_NON_BLANK_THRESHOLD = self.DEFAULT_INK_RATIO_NON_BLANK_THRESHOLD
        self.INK_PIXEL_THRESHOLD = self.DEFAULT_INK_PIXEL_THRESHOLD
        self._load_config(config_path)

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ink_ratio_from_page(page, scale: float, pixel_threshold: int) -> float:
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil().convert("L")
        histogram = image.histogram()
        total_pixels = sum(histogram)
        if total_pixels <= 0:
            return 0.0
        ink_pixels = sum(histogram[: max(0, min(256, pixel_threshold))])
        return float(ink_pixels) / float(total_pixels)

    def _load_config(self, config_path: Path) -> None:
        if not config_path.exists():
            return
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return

        algorithms = data.get("algorithms")
        if not isinstance(algorithms, dict):
            return

        value = self._as_float(algorithms.get("render_dpi"))
        if value is not None:
            self.BLANK_DETECT_RENDER_DPI = max(value, 36.0)

        value = self._as_float(algorithms.get("ink_ratio_non_blank_threshold"))
        if value is not None:
            self.INK_RATIO_NON_BLANK_THRESHOLD = max(value, 0.0)

    def run(
        self,
        pdf_path: str | Path,
        output_dir: str | Path | None = None,
        render_thumbnails: bool = False,
        thumbnail_dpi: int = 36,
    ) -> dict:
        pdf_path = Path(pdf_path).resolve()
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument(str(pdf_path))
            try:
                page_count = len(doc)

                outline: list[dict] = []
                for i, bookmark in enumerate(doc.get_toc()):
                    try:
                        page_index = int(bookmark.get_dest().get_index())
                    except Exception:
                        continue
                    page_index = max(0, min(page_count - 1, page_index))
                    outline.append(
                        {
                            "id": i + 1,  # 1-based id for CLI friendliness
                            "title": bookmark.get_title(),
                            "level": int(getattr(bookmark, "level", 0)),
                            "page_index": page_index,
                        }
                    )

                pages: list[dict] = []
                thumbnail_paths: list[str] = []
                thumbnail_root: Path | None = None
                if output_dir is not None and render_thumbnails:
                    thumbnail_root = Path(output_dir).resolve() / "ingestion" / "thumbnails"
                    thumbnail_root.mkdir(parents=True, exist_ok=True)

                blank_detect_scale = max(float(self.BLANK_DETECT_RENDER_DPI) / 72.0, 0.1)
                thumbnail_scale = max(float(thumbnail_dpi) / 72.0, 0.1)
                blank_page_indices: list[int] = []
                non_blank_page_indices: list[int] = []
                for page_index in range(page_count):
                    page = doc[page_index]
                    width, height = page.get_size()
                    ink_ratio = self._ink_ratio_from_page(
                        page,
                        scale=blank_detect_scale,
                        pixel_threshold=self.INK_PIXEL_THRESHOLD,
                    )
                    is_blank = ink_ratio < self.INK_RATIO_NON_BLANK_THRESHOLD
                    if is_blank:
                        blank_page_indices.append(page_index)
                    else:
                        non_blank_page_indices.append(page_index)
                    pages.append(
                        {
                            "page_index": page_index,
                            "width": int(round(width)),
                            "height": int(round(height)),
                            "ink_ratio": round(ink_ratio, 6),
                            "is_blank": is_blank,
                        }
                    )

                    if thumbnail_root is not None:
                        thumb_file = thumbnail_root / f"page_{page_index + 1:04d}.jpg"
                        bitmap = page.render(scale=thumbnail_scale)
                        image = bitmap.to_pil()
                        image.save(thumb_file, format="JPEG", quality=70, optimize=True)
                        thumbnail_paths.append(str(thumb_file))

                return {
                    "pdf_path": str(pdf_path),
                    "page_count": page_count,
                    "pages": pages,
                    "has_outline": len(outline) > 0,
                    "outline": outline,
                    "thumbnail_paths": thumbnail_paths,
                    "blank_page_indices": blank_page_indices,
                    "non_blank_page_indices": non_blank_page_indices,
                    "blank_detection": {
                        "render_dpi": self.BLANK_DETECT_RENDER_DPI,
                        "ink_ratio_non_blank_threshold": self.INK_RATIO_NON_BLANK_THRESHOLD,
                    },
                }
            finally:
                doc.close()
