from __future__ import annotations

import argparse
import json
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any


PIPELINE: Any = None
PIPELINE_MODE = ""


def load_pipeline(mode: str, device: str) -> Any:
    if mode == "pp_structurev3":
        from paddleocr import PPStructureV3

        return PPStructureV3(device=device)
    if mode == "doc_parser":
        from paddleocr import PaddleOCRVL

        return PaddleOCRVL(device=device)
    raise ValueError(f"Unsupported paddle worker mode: {mode}")


class PaddleWorkerHandler(BaseHTTPRequestHandler):
    server_version = "PaddleWorker/0.1"

    @staticmethod
    def _run_pipeline(input_path: Path) -> tuple[int, list[dict[str, Any]]]:
        with tempfile.TemporaryDirectory(prefix="paddle-worker-output-") as tmp_dir_str:
            output_dir = Path(tmp_dir_str) / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            results = list(PIPELINE.predict(input=str(input_path)))
            saved = 0
            for result in results:
                result.save_to_json(save_path=str(output_dir))
                saved += 1

            json_files: list[dict[str, Any]] = []
            for json_path in sorted(output_dir.rglob("*.json")):
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                json_files.append({"name": json_path.name, "data": payload})
        return saved, json_files

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._send_json({"status": "ok", "mode": PIPELINE_MODE})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/parse", "/parse-image"}:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        request_bytes = self.rfile.read(content_length) if content_length else b""
        if not request_bytes:
            self._send_json({"error": "Empty request body"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            with tempfile.TemporaryDirectory(prefix="paddle-worker-") as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                if self.path == "/parse-image":
                    requested_name = self.headers.get("X-File-Name", "input.png")
                    suffix = Path(str(requested_name)).suffix or ".png"
                    input_path = tmp_dir / f"input{suffix}"
                else:
                    input_path = tmp_dir / "input.pdf"
                input_path.write_bytes(request_bytes)
                saved, json_files = self._run_pipeline(input_path)
        except Exception as exc:  # pragma: no cover - runtime failure depends on container env
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json({"status": "ok", "pages": saved, "mode": PIPELINE_MODE, "json_files": json_files})


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent Paddle worker service.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19101)
    parser.add_argument("--mode", choices=["pp_structurev3", "doc_parser"], required=True)
    parser.add_argument("--device", default="gpu")
    return parser


def main() -> None:
    global PIPELINE
    global PIPELINE_MODE

    args = build_arg_parser().parse_args()
    PIPELINE_MODE = args.mode
    PIPELINE = load_pipeline(args.mode, args.device)
    server = ThreadingHTTPServer((args.host, args.port), PaddleWorkerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
