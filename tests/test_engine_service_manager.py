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


def test_running_service_restarts_when_health_check_fails(monkeypatch) -> None:
    manager = EngineServiceManager()
    events: list[str] = []

    monkeypatch.setattr(manager, "_docker_container_running", lambda name: True)
    monkeypatch.setattr(manager, "_docker_container_exists", lambda name: True)

    def fake_wait_for_health(url: str, timeout: int) -> None:
        events.append(f"health:{url}:{timeout}")
        if len(events) == 1:
            raise RuntimeError("health endpoint not ready")

    def fake_restart_container(name: str) -> None:
        events.append(f"restart:{name}")

    monkeypatch.setattr(manager, "_wait_for_health", fake_wait_for_health)
    monkeypatch.setattr(manager, "_docker_restart_existing", fake_restart_container)

    service_info = manager.ensure_service(
        "mineru",
        {
            "image": "pdf-agent/mineru-runner:0.1.0",
            "service": {
                "type": "mineru_api",
                "host": "127.0.0.1",
                "port": 19100,
                "container_name": "pdf-agent-mineru-api",
                "health_path": "/openapi.json",
                "running_health_timeout_sec": 12,
                "startup_timeout_sec": 600,
            },
        },
        {"name": "pipeline_gpu_auto"},
    )

    assert service_info["url"] == "http://127.0.0.1:19100"
    assert events == [
        "health:http://127.0.0.1:19100/openapi.json:12",
        "restart:pdf-agent-mineru-api",
        "health:http://127.0.0.1:19100/openapi.json:600",
    ]


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


def test_prewarm_from_config_runs_real_parse_for_mineru_api(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        """
engines:
  mineru:
    image: pdf-agent/mineru-runner:0.1.0
    runtime: docker_service
    service:
      type: mineru_api
      host: 127.0.0.1
      port: 19100
      container_name: pdf-agent-mineru-api
    retry_profiles:
      - name: pipeline_gpu_auto
        backend: pipeline
        lang: ch
""",
        encoding="utf-8",
    )
    manager = EngineServiceManager(project_root=tmp_path)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        manager,
        "ensure_service",
        lambda engine, engine_cfg, profile: {
            "url": "http://127.0.0.1:19100",
            "health_url": "http://127.0.0.1:19100/openapi.json",
            "service_type": "mineru_api",
        },
    )

    def fake_invoke_mineru_parse(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"content_list": [{"type": "text", "content": "warmup"}]}

    monkeypatch.setattr(manager, "invoke_mineru_parse", fake_invoke_mineru_parse)

    warmed = manager.prewarm_from_config(config_path)

    assert warmed == ["mineru:http://127.0.0.1:19100"]
    assert captured["base_url"] == "http://127.0.0.1:19100"
    assert captured["backend"] == "pipeline"
    assert captured["lang_list"] == ["ch"]
    assert captured["start_page_id"] == 0
    assert captured["end_page_id"] == 0
    assert Path(captured["pdf_path"]).exists()


def test_prewarm_from_config_skips_parse_for_direct_vlm_worker(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        """
engines:
  mineru:
    image: pdf-agent/mineru-runner:0.1.0
    runtime: docker_service
    service:
      type: mineru_direct_vlm_worker
      host: 127.0.0.1
      port: 19103
      container_name: pdf-agent-mineru25-pro-repair
    retry_profiles:
      - name: mineru25_pro_direct
        direct_vlm_model_id: opendatalab/MinerU2.5-Pro-2604-1.2B
""",
        encoding="utf-8",
    )
    manager = EngineServiceManager(project_root=tmp_path)

    monkeypatch.setattr(
        manager,
        "ensure_service",
        lambda engine, engine_cfg, profile: {
            "url": "http://127.0.0.1:19103",
            "health_url": "http://127.0.0.1:19103/health",
            "service_type": "mineru_direct_vlm_worker",
        },
    )
    monkeypatch.setattr(
        manager,
        "invoke_mineru_parse",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("direct worker should not use MinerU API parse")),
    )

    assert manager.prewarm_from_config(config_path) == ["mineru:http://127.0.0.1:19103"]
