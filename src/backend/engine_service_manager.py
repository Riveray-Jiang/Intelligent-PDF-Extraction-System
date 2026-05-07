from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import yaml


class EngineServiceManager:
    """Manage reusable parsing services backed by long-lived Docker containers."""

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[2]

    @staticmethod
    def _normalize_bool(value: Any) -> str:
        return "true" if bool(value) else "false"

    @staticmethod
    def _docker_mount_path(path: Path) -> str:
        return path.resolve().as_posix()

    def _load_engine_config(self, config_path: str | Path) -> dict[str, Any]:
        path = Path(config_path).resolve()
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {}
        engines = data.get("engines", {})
        return engines if isinstance(engines, dict) else {}

    def _run_subprocess(self, command: list[str], timeout_sec: int | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_sec,
        )

    def _resolve_cache_mounts(self, engine_cfg: dict[str, Any]) -> list[tuple[str, str]]:
        mounts_cfg = engine_cfg.get("cache_mounts", [])
        if not isinstance(mounts_cfg, list):
            return []
        resolved: list[tuple[str, str]] = []
        for entry in mounts_cfg:
            if not isinstance(entry, dict):
                continue
            container_path = str(entry.get("container_path", "")).strip()
            host_path_raw = str(entry.get("host_path", "")).strip()
            if not container_path or not host_path_raw:
                continue
            host_path = Path(host_path_raw)
            if not host_path.is_absolute():
                host_path = self.project_root / host_path
            host_path.mkdir(parents=True, exist_ok=True)
            resolved.append((self._docker_mount_path(host_path), container_path))
        return resolved

    def _docker_container_running(self, name: str) -> bool:
        result = self._run_subprocess(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout_sec=20)
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def _docker_container_exists(self, name: str) -> bool:
        result = self._run_subprocess(["docker", "inspect", name], timeout_sec=20)
        return result.returncode == 0

    def _docker_start_existing(self, name: str) -> None:
        result = self._run_subprocess(["docker", "start", name], timeout_sec=120)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise RuntimeError(f"Failed to start existing service container {name}: {detail}")

    def _wait_for_health(self, url: str, startup_timeout_sec: int) -> None:
        deadline = time.time() + max(10, int(startup_timeout_sec))
        last_error = "health endpoint not ready"
        while time.time() < deadline:
            try:
                response = httpx.get(url, timeout=5.0)
                if response.status_code == 200:
                    return
                last_error = f"health returned {response.status_code}"
            except Exception as exc:  # pragma: no cover - network race on startup
                last_error = str(exc)
            time.sleep(1.0)
        raise RuntimeError(f"Service failed to become healthy at {url}: {last_error}")

    def _run_detached_container(
        self,
        *,
        name: str,
        image: str,
        port: int,
        env_map: dict[str, str],
        volume_mounts: list[tuple[str, str]],
        command: list[str],
        use_gpu: bool,
        workdir: str | None = None,
    ) -> None:
        docker_cmd = [
            "docker",
            "run",
            "-d",
            "--restart",
            "unless-stopped",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:{port}",
        ]
        if use_gpu:
            docker_cmd.extend(["--gpus", "all"])
        if workdir:
            docker_cmd.extend(["-w", workdir])
        for host_path, container_path in volume_mounts:
            docker_cmd.extend(["-v", f"{host_path}:{container_path}"])
        for key, value in env_map.items():
            docker_cmd.extend(["-e", f"{key}={value}"])
        docker_cmd.append(image)
        docker_cmd.extend(command)

        result = self._run_subprocess(docker_cmd, timeout_sec=180)
        if result.returncode == 0:
            return

        stderr_text = (result.stderr or result.stdout or "").strip()
        if "is already in use by container" in stderr_text and self._docker_container_running(name):
            return
        detail = stderr_text[-500:] if stderr_text else "no stderr"
        raise RuntimeError(f"Failed to start service container {name}: {detail}")

    def ensure_service(self, engine: str, engine_cfg: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        service_cfg = engine_cfg.get("service", {})
        if not isinstance(service_cfg, dict):
            raise RuntimeError(f"Missing service config for engine={engine}")

        service_type = str(service_cfg.get("type", "")).strip().lower()
        host = str(service_cfg.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        port = int(service_cfg.get("port", 0) or 0)
        container_name = str(service_cfg.get("container_name", "")).strip()
        image = str(engine_cfg.get("image", "")).strip()
        startup_timeout_sec = int(service_cfg.get("startup_timeout_sec", 600) or 600)
        if not service_type or not port or not container_name or not image:
            raise RuntimeError(f"Incomplete service config for engine={engine}")

        health_path = str(service_cfg.get("health_path", "/health")).strip() or "/health"
        health_url = f"http://{host}:{port}{health_path}"
        if self._docker_container_running(container_name):
            self._wait_for_health(health_url, startup_timeout_sec)
            return {"url": f"http://{host}:{port}", "health_url": health_url, "service_type": service_type}

        if self._docker_container_exists(container_name):
            self._docker_start_existing(container_name)
            self._wait_for_health(health_url, startup_timeout_sec)
            return {"url": f"http://{host}:{port}", "health_url": health_url, "service_type": service_type}

        volume_mounts = self._resolve_cache_mounts(engine_cfg)
        env_map = dict(service_cfg.get("env", {})) if isinstance(service_cfg.get("env"), dict) else {}
        profile_env = profile.get("env", {}) if isinstance(profile.get("env"), dict) else {}
        for key, value in profile_env.items():
            env_map[str(key)] = str(value)

        use_gpu = bool(profile.get("use_gpu", True))
        if str(env_map.get("MINERU_DEVICE_MODE", "")).lower() == "cpu":
            use_gpu = False

        if service_type == "mineru_api":
            if "source" in profile:
                env_map.setdefault("MINERU_MODEL_SOURCE", str(profile.get("source")))
            bootstrap_packages = service_cfg.get("bootstrap_pip_packages", [])
            if isinstance(bootstrap_packages, list) and bootstrap_packages:
                package_args = " ".join(shlex.quote(str(pkg)) for pkg in bootstrap_packages if str(pkg).strip())
                bootstrap_cmd = (
                    "python3 -m pip install --disable-pip-version-check --no-input "
                    f"{package_args} >/tmp/mineru-api-bootstrap.log 2>&1 && "
                    f"exec mineru-api --host 0.0.0.0 --port {port}"
                )
                command = ["sh", "-lc", bootstrap_cmd]
            else:
                command = ["mineru-api", "--host", "0.0.0.0", "--port", str(port)]
            self._run_detached_container(
                name=container_name,
                image=image,
                port=port,
                env_map=env_map,
                volume_mounts=volume_mounts,
                command=command,
                use_gpu=use_gpu,
            )
        elif service_type == "mineru_direct_vlm_worker":
            volume_mounts = [
                (self._docker_mount_path(self.project_root), "/workspace"),
                *volume_mounts,
            ]
            env_map.setdefault("PYTHONPATH", "/workspace/src")
            model_id = str(profile.get("direct_vlm_model_id", "")).strip()
            if not model_id:
                raise RuntimeError(f"Missing direct_vlm_model_id for service {container_name}")
            scale = str(profile.get("scale", 2.0))
            env_map.setdefault("MINERU_DIRECT_VLM_MODEL_ID", model_id)
            env_map.setdefault("MINERU_DIRECT_VLM_SCALE", scale)
            bootstrap_packages = service_cfg.get("bootstrap_pip_packages", [])
            if isinstance(bootstrap_packages, list) and bootstrap_packages:
                package_args = " ".join(shlex.quote(str(pkg)) for pkg in bootstrap_packages if str(pkg).strip())
                bootstrap_cmd = (
                    "python3 -m pip install --disable-pip-version-check --no-input "
                    f"{package_args} >/tmp/mineru-vlm-worker-bootstrap.log 2>&1 && "
                    "exec python3 -m backend.mineru_vlm_worker_service "
                    f"--host 0.0.0.0 --port {port} "
                    f"--model-id {shlex.quote(model_id)} --scale {shlex.quote(scale)}"
                )
                command = ["sh", "-lc", bootstrap_cmd]
            else:
                command = [
                    "python3",
                    "-m",
                    "backend.mineru_vlm_worker_service",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(port),
                    "--model-id",
                    model_id,
                    "--scale",
                    scale,
                ]
            self._run_detached_container(
                name=container_name,
                image=image,
                port=port,
                env_map=env_map,
                volume_mounts=volume_mounts,
                command=command,
                use_gpu=use_gpu,
                workdir="/workspace",
            )
        elif service_type == "paddle_worker":
            volume_mounts = [
                (self._docker_mount_path(self.project_root), "/workspace"),
                *volume_mounts,
            ]
            env_map.setdefault("PYTHONPATH", "/workspace/src")
            worker_mode = str(service_cfg.get("worker_mode", "")).strip()
            if not worker_mode:
                raise RuntimeError(f"Missing worker_mode for paddle service {container_name}")
            device = "gpu" if use_gpu else "cpu"
            command = [
                "python",
                "-m",
                "backend.paddle_worker_service",
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
                "--mode",
                worker_mode,
                "--device",
                device,
            ]
            self._run_detached_container(
                name=container_name,
                image=image,
                port=port,
                env_map=env_map,
                volume_mounts=volume_mounts,
                command=command,
                use_gpu=use_gpu,
                workdir="/workspace",
            )
        else:
            raise RuntimeError(f"Unsupported service_type={service_type} for engine={engine}")

        self._wait_for_health(health_url, startup_timeout_sec)
        return {"url": f"http://{host}:{port}", "health_url": health_url, "service_type": service_type}

    def invoke_mineru_parse(
        self,
        *,
        base_url: str,
        pdf_path: str | Path,
        backend: str,
        parse_method: str = "auto",
        lang_list: list[str] | None = None,
        formula_enable: bool = True,
        table_enable: bool = True,
        start_page_id: int = 0,
        end_page_id: int = 99999,
        timeout_sec: int = 1800,
    ) -> dict[str, Any]:
        path = Path(pdf_path).resolve()
        form_data: dict[str, Any] = {
            "backend": backend,
            "parse_method": parse_method,
            "formula_enable": self._normalize_bool(formula_enable),
            "table_enable": self._normalize_bool(table_enable),
            "return_md": "false",
            "return_middle_json": "false",
            "return_model_output": "false",
            "return_content_list": "true",
            "return_images": "false",
            "response_format_zip": "false",
            "start_page_id": str(start_page_id),
            "end_page_id": str(end_page_id),
            "lang_list": [str(lang) for lang in (lang_list or ["ch"])],
        }

        with path.open("rb") as handle:
            files = [("files", (path.name, handle, "application/pdf"))]
            response = httpx.post(
                f"{base_url.rstrip('/')}/file_parse",
                data=form_data,
                files=files,
                timeout=float(timeout_sec),
            )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", {})
        if not isinstance(results, dict) or not results:
            raise RuntimeError("MinerU service returned no results")
        file_payload = next(iter(results.values()))
        if not isinstance(file_payload, dict):
            raise RuntimeError("MinerU service returned invalid file result payload")
        content_list_text = file_payload.get("content_list")
        if not content_list_text:
            raise RuntimeError("MinerU service returned no content_list")
        if isinstance(content_list_text, str):
            content_list = json.loads(content_list_text)
        else:
            content_list = content_list_text
        if not isinstance(content_list, list):
            raise RuntimeError("MinerU service returned invalid content_list structure")
        return {"content_list": content_list}

    def invoke_mineru_direct_vlm_parse(
        self,
        *,
        base_url: str,
        pdf_path: str | Path,
        timeout_sec: int = 1800,
    ) -> dict[str, Any]:
        path = Path(pdf_path).resolve()
        with path.open("rb") as handle:
            files = {"file": (path.name, handle, "application/pdf")}
            response = httpx.post(
                f"{base_url.rstrip('/')}/parse",
                files=files,
                timeout=float(timeout_sec),
            )
        response.raise_for_status()
        payload = response.json()
        content_list = payload.get("content_list")
        if not isinstance(content_list, list):
            raise RuntimeError("MinerU direct VLM service returned invalid content_list")
        return payload

    def invoke_paddle_parse(
        self,
        *,
        base_url: str,
        pdf_path: str | Path,
        timeout_sec: int = 1800,
    ) -> dict[str, Any]:
        path = Path(pdf_path).resolve()
        with path.open("rb") as handle:
            response = httpx.post(
                f"{base_url.rstrip('/')}/parse",
                content=handle.read(),
                headers={"Content-Type": "application/pdf"},
                timeout=float(timeout_sec),
            )
        response.raise_for_status()
        return response.json()

    def invoke_paddle_parse_image(
        self,
        *,
        base_url: str,
        image_bytes: bytes,
        filename: str = "input.png",
        timeout_sec: int = 1800,
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{base_url.rstrip('/')}/parse-image",
            content=image_bytes,
            headers={
                "Content-Type": "application/octet-stream",
                "X-File-Name": filename,
            },
            timeout=float(timeout_sec),
        )
        response.raise_for_status()
        return response.json()

    def prewarm_from_config(self, config_path: str | Path, engine_names: list[str] | None = None) -> list[str]:
        engines = self._load_engine_config(config_path)
        allowed = {name.strip().lower() for name in (engine_names or []) if str(name).strip()}
        warmed: list[str] = []
        for engine_name, engine_cfg in engines.items():
            if not isinstance(engine_cfg, dict):
                continue
            if allowed and str(engine_name).strip().lower() not in allowed:
                continue
            if str(engine_cfg.get("runtime", "")).strip().lower() != "docker_service":
                continue
            profiles = engine_cfg.get("retry_profiles", [])
            if not isinstance(profiles, list) or not profiles:
                continue
            service_info = self.ensure_service(str(engine_name), engine_cfg, profiles[0])
            warmed.append(f"{engine_name}:{service_info['url']}")
        return warmed
