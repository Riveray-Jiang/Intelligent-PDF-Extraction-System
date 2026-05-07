from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def _block_to_plain(block: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("content", "text", "html"):
        value = block.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content") or item.get("html")
                    if isinstance(text, str):
                        parts.append(text)
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _render_pages(pdf_path: Path, scale: float) -> list[Any]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf_path))
    images: list[Any] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            try:
                images.append(page.render(scale=scale).to_pil().convert("RGB"))
            finally:
                page.close()
    finally:
        document.close()
    return images


def _serializable_block(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    try:
        return dict(block)
    except (TypeError, ValueError):
        return {"type": "text", "content": str(block)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MinerU2.5-Pro direct VLM repair.")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--scale", type=float, default=2.0)
    args = parser.parse_args()

    from huggingface_hub import snapshot_download
    import torch
    from transformers import AutoModelForImageTextToText
    from transformers import AutoProcessor
    from mineru_vl_utils import MinerUClient

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = Path(args.pdf)

    started = time.perf_counter()
    model_path = snapshot_download(args.model_id)
    download_sec = round(time.perf_counter() - started, 4)

    load_started = time.perf_counter()
    processor = AutoProcessor.from_pretrained(model_path, use_fast=True)
    model = AutoModelForImageTextToText.from_pretrained(
        model_path,
        torch_dtype="auto",
        device_map="auto",
    )
    client = MinerUClient(
        backend="transformers",
        model=model,
        processor=processor,
        batch_size=1,
        use_tqdm=False,
    )
    load_sec = round(time.perf_counter() - load_started, 4)

    content_list: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    for page_index, image in enumerate(_render_pages(pdf_path, args.scale)):
        page_started = time.perf_counter()
        blocks = [_serializable_block(block) for block in client.two_step_extract(image)]
        page_sec = round(time.perf_counter() - page_started, 4)

        for order, block in enumerate(blocks):
            text = _block_to_plain(block)
            if not text:
                continue
            item = dict(block)
            item["page_idx"] = page_index
            item["type"] = str(item.get("type") or "text")
            item["content"] = text
            item["order"] = order
            item["source"] = "mineru2.5-pro-direct"
            content_list.append(item)

        page_summaries.append(
            {
                "page_idx": page_index,
                "seconds": page_sec,
                "block_count": len(blocks),
                "kept_blocks": sum(1 for block in blocks if _block_to_plain(block)),
                "block_types": [str(block.get("type", "")) for block in blocks],
            }
        )

    summary = {
        "model_id": args.model_id,
        "torch": getattr(torch, "__version__", ""),
        "cuda_available": bool(torch.cuda.is_available()),
        "download_sec": download_sec,
        "load_sec": load_sec,
        "total_sec": round(time.perf_counter() - started, 4),
        "pages": page_summaries,
    }
    (output_dir / "content_list.json").write_text(
        json.dumps(content_list, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
