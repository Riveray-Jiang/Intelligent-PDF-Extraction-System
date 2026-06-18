from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse


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


class MinerUDirectVlmWorker:
    def __init__(self, *, model_id: str, scale: float) -> None:
        from huggingface_hub import snapshot_download
        import torch
        from transformers import AutoModelForImageTextToText
        from transformers import AutoProcessor
        from mineru_vl_utils import MinerUClient

        started = time.perf_counter()
        model_path = snapshot_download(model_id)
        download_sec = round(time.perf_counter() - started, 4)

        load_started = time.perf_counter()
        processor = AutoProcessor.from_pretrained(model_path, use_fast=True)
        model = AutoModelForImageTextToText.from_pretrained(
            model_path,
            torch_dtype="auto",
            device_map="auto",
        )
        self.client = MinerUClient(
            backend="transformers",
            model=model,
            processor=processor,
            batch_size=1,
            use_tqdm=False,
        )
        self.model_id = model_id
        self.scale = scale
        self.download_sec = download_sec
        self.load_sec = round(time.perf_counter() - load_started, 4)
        self.cuda_available = bool(torch.cuda.is_available())
        self.torch_version = getattr(torch, "__version__", "")

    def parse_pdf(self, pdf_bytes: bytes, *, filename: str) -> dict[str, Any]:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="mineru-vlm-") as temp_dir:
            pdf_path = Path(temp_dir) / (filename or "input.pdf")
            pdf_path.write_bytes(pdf_bytes)

            content_list: list[dict[str, Any]] = []
            page_summaries: list[dict[str, Any]] = []
            for page_index, image in enumerate(_render_pages(pdf_path, self.scale)):
                page_started = time.perf_counter()
                blocks = [_serializable_block(block) for block in self.client.two_step_extract(image)]
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
                    item["source"] = "mineru2.5-pro-service"
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

        return {
            "content_list": content_list,
            "summary": {
                "model_id": self.model_id,
                "torch": self.torch_version,
                "cuda_available": self.cuda_available,
                "download_sec": self.download_sec,
                "load_sec": self.load_sec,
                "total_sec": round(time.perf_counter() - started, 4),
                "pages": page_summaries,
            },
        }


def create_app(worker: MinerUDirectVlmWorker) -> FastAPI:
    app = FastAPI(title="MinerU2.5-Pro Repair Worker")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "model_id": worker.model_id,
            "cuda_available": worker.cuda_available,
            "load_sec": worker.load_sec,
        }

    @app.post("/parse")
    async def parse(file: UploadFile = File(...)) -> JSONResponse:
        try:
            pdf_bytes = await file.read()
            if not pdf_bytes:
                raise HTTPException(status_code=400, detail="Empty PDF")
            payload = worker.parse_pdf(pdf_bytes, filename=file.filename or "input.pdf")
            return JSONResponse(payload)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Persistent MinerU2.5-Pro repair worker.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19102)
    parser.add_argument("--model-id", default=os.environ.get("MINERU_DIRECT_VLM_MODEL_ID", ""))
    parser.add_argument("--scale", type=float, default=float(os.environ.get("MINERU_DIRECT_VLM_SCALE", "2.0")))
    args = parser.parse_args()
    if not args.model_id:
        raise SystemExit("Missing --model-id or MINERU_DIRECT_VLM_MODEL_ID")

    import uvicorn

    worker = MinerUDirectVlmWorker(model_id=args.model_id, scale=float(args.scale))
    uvicorn.run(create_app(worker), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
