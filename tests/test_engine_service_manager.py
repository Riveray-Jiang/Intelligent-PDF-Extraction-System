from __future__ import annotations

from pathlib import Path

from backend.engine_service_manager import EngineServiceManager


def test_mineru_service_bootstrap_command(monkeypatch) -> None:
    manager = EngineServiceManager()
    captured: dict[str, object] = {}

    monkeypatch.setattr(manager, "_docker_container_running", lambda name: False)
    monkeypatch.setattr(manager, "_docker_container_exists", lambda name: False)
    monkeypatch.setattr(manager, "_wait_for_health", lambda url, timeout: None)

    def fake_run_detached_container(**kwargs):  # noqa: ANN003
        captured.update(kwargs)

    monkeypatch.setattr(manager, "_run_detached_container", fake_run_detached_container)

    service_info = manager.ensure_service(
        "mineru",
        {
            "image": "pdf-agent/mineru-runner:0.1.0",
            "cache_mounts": [],
            "service": {
                "type": "mineru_api",
                "host": "127.0.0.1",
                "port": 19100,
                "container_name": "pdf-agent-mineru-api",
                "bootstrap_pip_packages": ["fastapi", "uvicorn", "python-multipart"],
            },
        },
        {"name": "pipeline_gpu_auto", "source": "modelscope"},
    )

    assert service_info["service_type"] == "mineru_api"
    assert captured["name"] == "pdf-agent-mineru-api"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["sh", "-lc"]
    assert "python3 -m pip install" in command[2]
    assert "fastapi" in command[2]
    assert "uvicorn" in command[2]
    assert "python-multipart" in command[2]
    assert "exec mineru-api --host 0.0.0.0 --port 19100" in command[2]


def test_invoke_mineru_parse_uses_fastapi_compatible_multipart(tmp_path, monkeypatch) -> None:
    manager = EngineServiceManager()
    pdf_path = tmp_path / "demo.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "results": {
                    "demo.pdf": {
                        "content_list": '[{"page_idx": 0, "type": "text", "content": "ok"}]'
                    }
                }
            }

    def fake_post(url, data=None, files=None, timeout=None):  # noqa: ANN001
        captured["url"] = url
        captured["data"] = data
        captured["files"] = files
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("backend.engine_service_manager.httpx.post", fake_post)

    result = manager.invoke_mineru_parse(
        base_url="http://127.0.0.1:19100",
        pdf_path=pdf_path,
        backend="pipeline",
        lang_list=["ch"],
        timeout_sec=123,
    )

    assert result["content_list"][0]["content"] == "ok"
    assert captured["url"] == "http://127.0.0.1:19100/file_parse"
    assert captured["timeout"] == 123.0
    assert isinstance(captured["data"], dict)
    assert captured["data"]["lang_list"] == ["ch"]
    assert isinstance(captured["files"], list)
    assert captured["files"][0][0] == "files"
