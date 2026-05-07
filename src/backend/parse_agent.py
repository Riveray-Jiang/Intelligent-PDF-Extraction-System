from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from itertools import chain
from pathlib import Path
from typing import Any
from typing import Sequence

import yaml

from .adapters.mineru_adapter import MineruAdapter
from .adapters.paddle_adapter import PaddleAdapter
from .engine_service_manager import EngineServiceManager


class ParseAgent:
    """Execute parsing profiles and normalize raw engine output."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        paddle_adapter: PaddleAdapter | None = None,
        mineru_adapter: MineruAdapter | None = None,
        service_manager: EngineServiceManager | None = None,
        allow_mock_output: bool = False,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        self.project_root = root
        self.config_path = Path(config_path) if config_path else (root / "configs" / "engines.yaml")
        self.paddle_adapter = paddle_adapter or PaddleAdapter()
        self.mineru_adapter = mineru_adapter or MineruAdapter()
        self.service_manager = service_manager or EngineServiceManager(root)
        self.allow_mock_output = allow_mock_output

    def _load_engine_config(self, engine: str) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        engines = data.get("engines", {})
        if not isinstance(engines, dict):
            return {}
        config = engines.get(engine, {})
        return config if isinstance(config, dict) else {}

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any] | list[dict[str, Any]]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        raise ValueError(f"Unsupported JSON structure in {path}")

    @staticmethod
    def _paddle_sort_key(path: Path) -> tuple[int, str]:
        match = re.search(r"_(\d+)_res\.json$", path.name)
        if match:
            return int(match.group(1)), path.name
        if path.name == "result.json":
            return 10**9, path.name
        return 10**9 - 1, path.name

    @staticmethod
    def _paddle_page_hint(path: Path) -> int | None:
        match = re.search(r"_(\d+)_res\.json$", path.name)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _to_int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _extract_paddle_pages(
        cls,
        raw: dict[str, Any] | list[dict[str, Any]],
        source_path: Path,
        fallback_page_index: int,
    ) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            raw_pages = raw.get("pages")
            if isinstance(raw_pages, list):
                pages = [dict(item) for item in raw_pages if isinstance(item, dict)]
            else:
                raw_result = raw.get("result")
                if isinstance(raw_result, list):
                    pages = [dict(item) for item in raw_result if isinstance(item, dict)]
                elif isinstance(raw.get("parsing_res_list"), list):
                    pages = [{"parsing_res_list": raw.get("parsing_res_list", [])}]
                else:
                    pages = [dict(raw)]
        else:
            pages = [dict(item) for item in raw if isinstance(item, dict)]

        page_hint = cls._paddle_page_hint(source_path)
        normalized_pages: list[dict[str, Any]] = []
        for offset, page in enumerate(pages):
            page_index = cls._to_int_or_none(page.get("page_index"))
            if page_index is None:
                page_index = cls._to_int_or_none(page.get("page_id"))
            if page_index is None:
                if page_hint is not None and len(pages) == 1:
                    page_index = page_hint
                else:
                    page_index = fallback_page_index + offset
            page["page_index"] = page_index
            normalized_pages.append(page)
        return normalized_pages

    @staticmethod
    def _find_json_files(root: Path, engine: str) -> list[Path]:
        if not root.exists():
            return []
        if engine == "paddle":
            per_page = sorted(root.rglob("*_res.json"), key=ParseAgent._paddle_sort_key)
            if per_page:
                return per_page
            aggregated = sorted(root.rglob("result.json"))
            if aggregated:
                return aggregated
        if engine == "mineru":
            preferred = sorted(
                set(chain(root.rglob("*_content_list.json"), root.rglob("content_list.json")))
            )
            if preferred:
                return preferred
        return sorted(root.rglob("*.json"))

    @classmethod
    def _load_paddle_json_batch(
        cls,
        candidates: list[Path],
    ) -> list[dict[str, Any]]:
        merged_pages: list[dict[str, Any]] = []
        fallback_page_index = 0
        for candidate in candidates:
            raw = cls._load_json(candidate)
            pages = cls._extract_paddle_pages(raw, candidate, fallback_page_index)
            merged_pages.extend(pages)
            fallback_page_index += max(1, len(pages))
        return merged_pages

    @classmethod
    def _load_paddle_json_batch_from_items(
        cls,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        def item_sort_key(item: dict[str, Any]) -> tuple[int, str]:
            return cls._paddle_sort_key(Path(str(item.get("name", "result.json"))))

        merged_pages: list[dict[str, Any]] = []
        fallback_page_index = 0
        for item in sorted((entry for entry in items if isinstance(entry, dict)), key=item_sort_key):
            raw = item.get("data")
            if not isinstance(raw, (dict, list)):
                continue
            source_name = Path(str(item.get("name", "result.json")))
            pages = cls._extract_paddle_pages(raw, source_name, fallback_page_index)
            merged_pages.extend(pages)
            fallback_page_index += max(1, len(pages))
        return merged_pages

    @staticmethod
    def _extract_selected_pages(selection: dict[str, Any]) -> list[int]:
        selected = selection.get("selected_page_indices", [])
        if not isinstance(selected, list):
            return []
        out: list[int] = []
        for value in selected:
            try:
                out.append(int(value))
            except (TypeError, ValueError):
                continue
        return sorted(set(out))

    @staticmethod
    def _to_mock_paddle(selected_pages: list[int]) -> dict[str, Any]:
        return {
            "pages": [{"page_index": page_index, "blocks": []} for page_index in selected_pages],
        }

    @staticmethod
    def _to_mock_mineru() -> dict[str, Any]:
        return {"content_list": []}

    def _adapt(
        self,
        engine: str,
        raw: dict[str, Any] | list[dict[str, Any]],
        source_file: str,
        selected_pages: list[int] | None,
    ) -> dict[str, Any]:
        if engine == "paddle":
            return self.paddle_adapter.parse(
                raw=raw,
                source_file=source_file,
                selected_pages=selected_pages,
            )
        if engine == "mineru":
            return self.mineru_adapter.parse(
                raw=raw,
                source_file=source_file,
                selected_pages=selected_pages,
            )
        raise ValueError(f"Unsupported engine: {engine}")

    @staticmethod
    def _normalize_profile_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _build_engine_command(
        self,
        engine: str,
        engine_cfg: dict[str, Any],
        profile: dict[str, Any],
        input_path: str,
        output_path: str,
        selected_pages: list[int],
    ) -> list[str]:
        command_raw = str(engine_cfg.get("command", "")).strip()
        if not command_raw:
            raise ValueError(f"Missing command in config for engine={engine}")
        command = shlex.split(command_raw)
        if not command:
            raise ValueError(f"Invalid command in config for engine={engine}")

        if engine == "paddle":
            command.extend(["-i", input_path, "--save_path", output_path])
            for key in ("enable_hpi", "use_tensorrt", "precision"):
                if key in profile:
                    command.extend([f"--{key}", self._normalize_profile_value(profile[key])])
        elif engine == "mineru":
            command.extend(["-p", input_path, "-o", output_path])
            mineru_option_map = {
                "backend": "-b",
                "method": "-m",
                "lang": "-l",
                "source": "--source",
                "device": "-d",
                "vram": "--vram",
                "start": "-s",
                "end": "-e",
                "formula": "-f",
                "table": "-t",
            }
            for key, flag in mineru_option_map.items():
                if key in profile:
                    command.extend([flag, self._normalize_profile_value(profile[key])])
        else:
            raise ValueError(f"Unsupported engine: {engine}")

        return command

    @staticmethod
    def _extract_profile_env(profile: dict[str, Any]) -> dict[str, str]:
        env_cfg = profile.get("env", {})
        if not isinstance(env_cfg, dict):
            return {}
        return {str(key): str(value) for key, value in env_cfg.items()}

    @staticmethod
    def _profile_service_fallback_runtime(engine_cfg: dict[str, Any]) -> str | None:
        service_cfg = engine_cfg.get("service", {})
        if not isinstance(service_cfg, dict):
            return None
        value = str(service_cfg.get("fallback_runtime", "")).strip().lower()
        return value or None

    def _run_subprocess(
        self,
        command: Sequence[str],
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        run_env = None
        if env:
            merged_env = dict(os.environ)
            merged_env.update(env)
            run_env = merged_env
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=run_env,
            check=False,
            timeout=timeout_sec,
        )

    @staticmethod
    def _docker_mount_path(path: Path) -> str:
        resolved = path.resolve()
        if os.name == "nt":
            # Docker Desktop on Windows is more reliable with forward slashes.
            return resolved.as_posix()
        return str(resolved)

    @staticmethod
    def _profile_timeout_sec(engine_cfg: dict[str, Any], profile: dict[str, Any]) -> int | None:
        """Resolve timeout seconds for a parse profile.

        `timeout_sec=0` (or `none/null/infinite`) means no timeout.
        """

        def parse_timeout(value: Any) -> int | None:
            if value is None:
                return None
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"", "none", "null", "inf", "infinite"}:
                    return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            if parsed <= 0:
                return None
            return parsed

        profile_value = parse_timeout(profile.get("timeout_sec"))
        if "timeout_sec" in profile:
            return profile_value

        engine_value = parse_timeout(engine_cfg.get("timeout_sec"))
        if "timeout_sec" in engine_cfg:
            return engine_value
        return 1800

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

    @staticmethod
    def _contains_unrecoverable_error(text: str, patterns: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(pattern.lower() in lowered for pattern in patterns)

    @staticmethod
    def _extract_unrecoverable_line(text: str, patterns: tuple[str, ...]) -> str | None:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            lowered = line.lower()
            if any(pattern.lower() in lowered for pattern in patterns):
                return line
        return None

    @staticmethod
    def _read_log_tail(path: Path, max_chars: int = 2000) -> str:
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        if not text:
            return ""
        return text[-max_chars:]

    @staticmethod
    def _create_selected_pdf(source_pdf: Path, selected_pages: list[int], destination_pdf: Path) -> list[int]:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(str(source_pdf))
        total_pages = len(reader.pages)
        effective_pages = [idx for idx in selected_pages if 0 <= idx < total_pages]
        if not effective_pages:
            raise ValueError(
                f"No valid selected pages for {source_pdf.name}. selected={selected_pages}, total_pages={total_pages}"
            )
        writer = PdfWriter()
        for page_index in effective_pages:
            writer.add_page(reader.pages[page_index])
        with destination_pdf.open("wb") as handle:
            writer.write(handle)
        return effective_pages

    @staticmethod
    def _remap_local_page_index(value: Any, page_map: list[int]) -> int | None:
        try:
            local_index = int(value)
        except (TypeError, ValueError):
            return None
        if 0 <= local_index < len(page_map):
            return page_map[local_index]
        return local_index

    @classmethod
    def _remap_adapted_output_pages(cls, adapted: dict[str, Any], page_map: list[int]) -> None:
        pages = adapted.get("pages")
        if isinstance(pages, list):
            for page in pages:
                if not isinstance(page, dict):
                    continue
                remapped_page_index = cls._remap_local_page_index(page.get("page_index"), page_map)
                if remapped_page_index is not None:
                    page["page_index"] = remapped_page_index
                blocks = page.get("blocks")
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    remapped_block_index = cls._remap_local_page_index(block.get("page_index"), page_map)
                    if remapped_block_index is not None:
                        block["page_index"] = remapped_block_index

        mineru_items = adapted.get("mineru_content_list")
        if isinstance(mineru_items, list):
            for item in mineru_items:
                if not isinstance(item, dict):
                    continue
                remapped_item_index = cls._remap_local_page_index(item.get("page_idx"), page_map)
                if remapped_item_index is not None:
                    item["page_idx"] = remapped_item_index

    def _run_docker_profile(
        self,
        engine: str,
        engine_cfg: dict[str, Any],
        profile: dict[str, Any],
        source_pdf: Path,
        attempt_dir: Path,
        selected_pages: list[int],
    ) -> tuple[int, str, list[int] | None, dict[str, float]]:
        image = str(engine_cfg.get("image", "")).strip()
        if not image:
            raise ValueError(f"Missing image in config for engine={engine}")

        selected_page_map: list[int] | None = None
        input_source_pdf = source_pdf
        selected_pdf_started = time.perf_counter()
        selected_pdf_sec = 0.0
        if selected_pages:
            selected_input_pdf = attempt_dir / "_selected_pages_input.pdf"
            selected_page_map = self._create_selected_pdf(source_pdf, selected_pages, selected_input_pdf)
            input_source_pdf = selected_input_pdf
            selected_pdf_sec = round(time.perf_counter() - selected_pdf_started, 4)

        input_mount = self._docker_mount_path(input_source_pdf.parent)
        output_mount = self._docker_mount_path(attempt_dir)
        input_in_container = f"/input/{input_source_pdf.name}"
        output_in_container = "/output"
        timeout_sec = self._profile_timeout_sec(engine_cfg, profile)

        command = self._build_engine_command(
            engine=engine,
            engine_cfg=engine_cfg,
            profile=profile,
            input_path=input_in_container,
            output_path=output_in_container,
            selected_pages=selected_pages,
        )

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{input_mount}:/input:ro",
            "-v",
            f"{output_mount}:/output",
        ]
        for host_cache, container_cache in self._resolve_cache_mounts(engine_cfg):
            docker_cmd.extend(["-v", f"{host_cache}:{container_cache}"])
        use_gpu = bool(profile.get("use_gpu", True))
        env_map = self._extract_profile_env(profile)
        if str(env_map.get("MINERU_DEVICE_MODE", "")).lower() == "cpu":
            use_gpu = False
        if use_gpu:
            docker_cmd[3:3] = ["--gpus", "all"]
        for key, value in env_map.items():
            docker_cmd.extend(["-e", f"{key}={value}"])
        docker_cmd.append(image)
        docker_cmd.extend(command)

        docker_started = time.perf_counter()
        try:
            result = self._run_subprocess(docker_cmd, timeout_sec=timeout_sec)
        except FileNotFoundError as exc:
            raise RuntimeError("docker executable was not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Docker command timed out after {timeout_sec}s for engine={engine}"
            ) from exc
        docker_run_sec = round(time.perf_counter() - docker_started, 4)

        (attempt_dir / "command.txt").write_text(" ".join(shlex.quote(part) for part in docker_cmd), encoding="utf-8")
        (attempt_dir / "stdout.log").write_text(result.stdout or "", encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(result.stderr or "", encoding="utf-8")

        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip()
            detail = stderr_tail[-500:] if stderr_tail else "no stderr"
            raise RuntimeError(
                f"Docker command failed (returncode={result.returncode}) for engine={engine}: {detail}"
            )

        return result.returncode, " ".join(docker_cmd), selected_page_map, {
            "selected_pdf_sec": selected_pdf_sec,
            "docker_run_sec": docker_run_sec,
        }

    def _run_docker_service_profile(
        self,
        engine: str,
        engine_cfg: dict[str, Any],
        profile: dict[str, Any],
        source_pdf: Path,
        attempt_dir: Path,
        selected_pages: list[int],
    ) -> tuple[str, list[int] | None, dict[str, float], dict[str, Any] | list[dict[str, Any]] | None]:
        selected_page_map: list[int] | None = None
        input_source_pdf = source_pdf
        selected_pdf_started = time.perf_counter()
        selected_pdf_sec = 0.0
        if selected_pages:
            selected_input_pdf = attempt_dir / "_selected_pages_input.pdf"
            selected_page_map = self._create_selected_pdf(source_pdf, selected_pages, selected_input_pdf)
            input_source_pdf = selected_input_pdf
            selected_pdf_sec = round(time.perf_counter() - selected_pdf_started, 4)

        timeout_sec = self._profile_timeout_sec(engine_cfg, profile) or 1800
        service_started = time.perf_counter()
        service_info = self.service_manager.ensure_service(engine, engine_cfg, profile)
        service_ensure_sec = round(time.perf_counter() - service_started, 4)
        base_url = str(service_info["url"]).rstrip("/")
        service_type = str(service_info["service_type"])

        invoke_started = time.perf_counter()
        raw_output: dict[str, Any] | list[dict[str, Any]] | None = None
        if service_type == "mineru_api":
            raw_output = self.service_manager.invoke_mineru_parse(
                base_url=base_url,
                pdf_path=input_source_pdf,
                backend=str(profile.get("backend", "pipeline")),
                parse_method=str(profile.get("method", "auto")),
                lang_list=[str(profile.get("lang", "ch"))],
                formula_enable=bool(profile.get("formula", True)),
                table_enable=bool(profile.get("table", True)),
                start_page_id=int(profile.get("start", 0) or 0),
                end_page_id=int(profile.get("end", 99999) or 99999),
                timeout_sec=timeout_sec,
            )
        elif service_type == "mineru_direct_vlm_worker":
            raw_output = self.service_manager.invoke_mineru_direct_vlm_parse(
                base_url=base_url,
                pdf_path=input_source_pdf,
                timeout_sec=timeout_sec,
            )
        elif service_type == "paddle_worker":
            service_payload = self.service_manager.invoke_paddle_parse(
                base_url=base_url,
                pdf_path=input_source_pdf,
                timeout_sec=timeout_sec,
            )
            json_items = service_payload.get("json_files")
            if not isinstance(json_items, list) or not json_items:
                raise RuntimeError("Paddle service returned no json_files")
            raw_output = self._load_paddle_json_batch_from_items(json_items)
        else:
            raise RuntimeError(f"Unsupported docker service type for engine={engine}: {service_type}")
        service_run_sec = round(time.perf_counter() - invoke_started, 4)

        command_str = f"service://{service_type} {base_url}"
        return command_str, selected_page_map, {
            "selected_pdf_sec": selected_pdf_sec,
            "service_ensure_sec": service_ensure_sec,
            "service_run_sec": service_run_sec,
        }, raw_output

    def _run_mineru_direct_vlm_profile(
        self,
        engine: str,
        engine_cfg: dict[str, Any],
        profile: dict[str, Any],
        source_pdf: Path,
        attempt_dir: Path,
        selected_pages: list[int],
    ) -> tuple[str, list[int] | None, dict[str, float], dict[str, Any]]:
        if engine != "mineru":
            raise RuntimeError("direct MinerU VLM repair only supports engine=mineru")

        image = str(engine_cfg.get("image", "")).strip()
        model_id = str(profile.get("direct_vlm_model_id", "")).strip()
        if not image or not model_id:
            raise ValueError("direct MinerU VLM repair requires image and direct_vlm_model_id")

        selected_page_map: list[int] | None = None
        input_source_pdf = source_pdf
        selected_pdf_started = time.perf_counter()
        selected_pdf_sec = 0.0
        if selected_pages:
            selected_input_pdf = attempt_dir / "_selected_pages_input.pdf"
            selected_page_map = self._create_selected_pdf(source_pdf, selected_pages, selected_input_pdf)
            input_source_pdf = selected_input_pdf
            selected_pdf_sec = round(time.perf_counter() - selected_pdf_started, 4)

        input_mount = self._docker_mount_path(input_source_pdf.parent)
        output_mount = self._docker_mount_path(attempt_dir)
        workspace_mount = self._docker_mount_path(self.project_root)
        input_in_container = f"/input/{input_source_pdf.name}"
        output_in_container = "/output/direct_vlm"
        timeout_sec = self._profile_timeout_sec(engine_cfg, profile)

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{input_mount}:/input:ro",
            "-v",
            f"{output_mount}:/output",
            "-v",
            f"{workspace_mount}:/workspace:ro",
            "-w",
            "/workspace",
        ]
        for host_cache, container_cache in self._resolve_cache_mounts(engine_cfg):
            docker_cmd.extend(["-v", f"{host_cache}:{container_cache}"])

        use_gpu = bool(profile.get("use_gpu", True))
        env_map = self._extract_profile_env(profile)
        if str(env_map.get("MINERU_DEVICE_MODE", "")).lower() == "cpu":
            use_gpu = False
        if use_gpu:
            docker_cmd[3:3] = ["--gpus", "all"]
        env_map.setdefault("PYTHONIOENCODING", "utf-8")
        env_map.setdefault("PYTHONPATH", "/workspace/src")
        for key, value in env_map.items():
            docker_cmd.extend(["-e", f"{key}={value}"])

        docker_cmd.extend(
            [
                image,
                "python3",
                "-m",
                "backend.mineru_vlm_direct_runner",
                "--model-id",
                model_id,
                "--pdf",
                input_in_container,
                "--output",
                output_in_container,
                "--scale",
                self._normalize_profile_value(profile.get("scale", 2.0)),
            ]
        )

        docker_started = time.perf_counter()
        try:
            result = self._run_subprocess(docker_cmd, timeout_sec=timeout_sec)
        except FileNotFoundError as exc:
            raise RuntimeError("docker executable was not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Direct MinerU VLM repair timed out after {timeout_sec}s"
            ) from exc
        docker_run_sec = round(time.perf_counter() - docker_started, 4)

        (attempt_dir / "command.txt").write_text(" ".join(shlex.quote(part) for part in docker_cmd), encoding="utf-8")
        (attempt_dir / "stdout.log").write_text(result.stdout or "", encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(result.stderr or "", encoding="utf-8")
        if result.returncode != 0:
            detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-700:] or "no output"
            raise RuntimeError(
                f"Direct MinerU VLM repair failed (returncode={result.returncode}): {detail}"
            )

        content_list_path = attempt_dir / "direct_vlm" / "content_list.json"
        if not content_list_path.exists():
            raise RuntimeError("Direct MinerU VLM repair returned no content_list.json")
        raw_content = json.loads(content_list_path.read_text(encoding="utf-8"))
        if not isinstance(raw_content, list):
            raise RuntimeError("Direct MinerU VLM repair returned invalid content_list.json")

        command_str = " ".join(docker_cmd)
        return command_str, selected_page_map, {
            "selected_pdf_sec": selected_pdf_sec,
            "docker_run_sec": docker_run_sec,
        }, {"content_list": raw_content}

    def _execute_profile(
        self,
        engine: str,
        engine_cfg: dict[str, Any],
        profile: dict[str, Any],
        source_pdf: Path,
        attempt_dir: Path,
        selected_pages: list[int],
    ) -> tuple[str, list[int] | None, dict[str, float], dict[str, Any] | list[dict[str, Any]] | None]:
        runtime = str(engine_cfg.get("runtime", "docker")).strip().lower()
        if runtime == "docker_direct_vlm":
            return self._run_mineru_direct_vlm_profile(
                engine=engine,
                engine_cfg=engine_cfg,
                profile=profile,
                source_pdf=source_pdf,
                attempt_dir=attempt_dir,
                selected_pages=selected_pages,
            )
        if runtime == "docker":
            _, command_str, selected_page_map, timing = self._run_docker_profile(
                engine=engine,
                engine_cfg=engine_cfg,
                profile=profile,
                source_pdf=source_pdf,
                attempt_dir=attempt_dir,
                selected_pages=selected_pages,
            )
            return command_str, selected_page_map, timing, None
        if runtime == "docker_service":
            try:
                return self._run_docker_service_profile(
                    engine=engine,
                    engine_cfg=engine_cfg,
                    profile=profile,
                    source_pdf=source_pdf,
                    attempt_dir=attempt_dir,
                    selected_pages=selected_pages,
                )
            except Exception:
                fallback_runtime = self._profile_service_fallback_runtime(engine_cfg)
                if fallback_runtime == "docker_direct_vlm":
                    command_str, selected_page_map, timing, direct_raw_output = self._run_mineru_direct_vlm_profile(
                        engine=engine,
                        engine_cfg=engine_cfg,
                        profile=profile,
                        source_pdf=source_pdf,
                        attempt_dir=attempt_dir,
                        selected_pages=selected_pages,
                    )
                    timing["service_fallback_to_direct_vlm"] = 1.0
                    return command_str, selected_page_map, timing, direct_raw_output
                if fallback_runtime != "docker":
                    raise
                _, command_str, selected_page_map, timing = self._run_docker_profile(
                    engine=engine,
                    engine_cfg=engine_cfg,
                    profile=profile,
                    source_pdf=source_pdf,
                    attempt_dir=attempt_dir,
                    selected_pages=selected_pages,
                )
                timing["service_fallback_to_docker"] = 1.0
                return command_str, selected_page_map, timing, None
        raise ValueError(f"Unsupported runtime={runtime} for engine={engine}")

    def run(self, engine: str, pdf_path: str, selection: dict, output_dir: str) -> dict:
        run_started = time.perf_counter()
        engine = engine.lower().strip()
        if engine not in {"paddle", "mineru"}:
            raise ValueError(f"Unsupported engine: {engine}")

        source_pdf = Path(pdf_path).resolve()
        if not source_pdf.exists():
            raise FileNotFoundError(f"PDF not found: {source_pdf}")

        selected_pages = self._extract_selected_pages(selection or {})
        parse_root = Path(output_dir).resolve() / "parse" / engine
        parse_root.mkdir(parents=True, exist_ok=True)

        config = self._load_engine_config(engine)
        profiles = config.get("retry_profiles", [])
        if not isinstance(profiles, list) or not profiles:
            profiles = [{"name": "default"}]

        # Error patterns that indicate the container image itself is broken;
        # retrying with a different profile cannot fix these.
        _UNRECOVERABLE_PATTERNS = (
            "DependencyError",
            "ModuleNotFoundError",
            "ImportError",
            "No module named",
            "name 'torch' is not defined",
            "docker executable was not found",
        )

        errors: list[str] = []
        preparsed_json = selection.get("preparsed_json") if isinstance(selection, dict) else None
        if preparsed_json:
            json_path = Path(str(preparsed_json)).resolve()
            if json_path.exists():
                load_started = time.perf_counter()
                raw = self._load_json(json_path)
                adapted = self._adapt(engine, raw, str(source_pdf), selected_pages)
                adapted["parse_meta"] = {
                    "mode": "preparsed_json",
                    "profile": "external",
                    "raw_path": str(json_path),
                    "timings": {
                        "load_adapt_sec": round(time.perf_counter() - load_started, 4),
                        "total_sec": round(time.perf_counter() - run_started, 4),
                    },
                }
                return adapted
            errors.append(f"preparsed_json not found: {json_path}")

        for attempt_index, profile in enumerate(profiles, start=1):
            attempt_started = time.perf_counter()
            profile_name = str(profile.get("name", f"attempt_{attempt_index}"))
            attempt_dir = parse_root / f"attempt_{attempt_index:02d}_{profile_name}"
            attempt_dir.mkdir(parents=True, exist_ok=True)

            command_str = ""
            selected_page_map: list[int] | None = None
            profile_timing: dict[str, float] = {}
            direct_raw_output: dict[str, Any] | list[dict[str, Any]] | None = None
            try:
                command_str, selected_page_map, profile_timing, direct_raw_output = self._execute_profile(
                    engine=engine,
                    engine_cfg=config,
                    profile=profile,
                    source_pdf=source_pdf,
                    attempt_dir=attempt_dir,
                    selected_pages=selected_pages,
                )
            except Exception as exc:
                error_text = str(exc)
                errors.append(f"profile={profile_name} execute_error={exc}")
                if self._contains_unrecoverable_error(error_text, _UNRECOVERABLE_PATTERNS):
                    reason = self._extract_unrecoverable_line(error_text, _UNRECOVERABLE_PATTERNS) or error_text[-500:]
                    errors.append(f"profile={profile_name} unrecoverable_reason={reason}")
                    errors.append(
                        f"ABORT: unrecoverable error detected ({profile_name}), "
                        f"skipping remaining {len(profiles) - attempt_index} profile(s)"
                    )
                    break
                continue

            if direct_raw_output is not None:
                try:
                    load_started = time.perf_counter()
                    selected_for_adapter = None if selected_page_map else selected_pages
                    adapted = self._adapt(engine, direct_raw_output, str(source_pdf), selected_for_adapter)
                    if selected_page_map:
                        self._remap_adapted_output_pages(adapted, selected_page_map)
                    profile_timing["load_adapt_sec"] = round(time.perf_counter() - load_started, 4)
                    profile_timing["profile_total_sec"] = round(time.perf_counter() - attempt_started, 4)
                    service_raw_path = attempt_dir / "service_result.json"
                    service_raw_path.write_text(
                        json.dumps(direct_raw_output, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    adapted["parse_meta"] = {
                        "mode": "service_json",
                        "profile": profile_name,
                        "attempt": attempt_index,
                        "raw_path": str(service_raw_path),
                        "runtime": str(config.get("runtime", "docker")),
                        "command": command_str,
                        "selected_page_map": selected_page_map,
                        "timings": {
                            **profile_timing,
                            "total_sec": round(time.perf_counter() - run_started, 4),
                        },
                    }
                    return adapted
                except Exception as exc:
                    errors.append(f"profile={profile_name} service_result error={exc}")

            discovery_started = time.perf_counter()
            candidates = self._find_json_files(attempt_dir, engine)
            profile_timing["json_discovery_sec"] = round(time.perf_counter() - discovery_started, 4)
            if engine == "paddle" and candidates:
                try:
                    load_started = time.perf_counter()
                    raw = self._load_paddle_json_batch(candidates)
                    selected_for_adapter = None if selected_page_map else selected_pages
                    adapted = self._adapt(engine, raw, str(source_pdf), selected_for_adapter)
                    if selected_page_map:
                        self._remap_adapted_output_pages(adapted, selected_page_map)
                    profile_timing["load_adapt_sec"] = round(time.perf_counter() - load_started, 4)
                    profile_timing["profile_total_sec"] = round(time.perf_counter() - attempt_started, 4)
                    adapted["parse_meta"] = {
                        "mode": "json",
                        "profile": profile_name,
                        "attempt": attempt_index,
                        "raw_path": [str(candidate) for candidate in candidates],
                        "runtime": str(config.get("runtime", "docker")),
                        "command": command_str,
                        "selected_page_map": selected_page_map,
                        "timings": {
                            **profile_timing,
                            "total_sec": round(time.perf_counter() - run_started, 4),
                        },
                    }
                    return adapted
                except Exception as exc:  # pragma: no cover - defensive parsing path
                    errors.append(f"profile={profile_name} file_batch={attempt_dir} error={exc}")

            for candidate in candidates:
                try:
                    load_started = time.perf_counter()
                    raw = self._load_json(candidate)
                    selected_for_adapter = None if selected_page_map else selected_pages
                    adapted = self._adapt(engine, raw, str(source_pdf), selected_for_adapter)
                    if selected_page_map:
                        self._remap_adapted_output_pages(adapted, selected_page_map)
                    profile_timing["load_adapt_sec"] = round(time.perf_counter() - load_started, 4)
                    profile_timing["profile_total_sec"] = round(time.perf_counter() - attempt_started, 4)
                    adapted["parse_meta"] = {
                        "mode": "json",
                        "profile": profile_name,
                        "attempt": attempt_index,
                        "raw_path": str(candidate),
                        "runtime": str(config.get("runtime", "docker")),
                        "command": command_str,
                        "selected_page_map": selected_page_map,
                        "timings": {
                            **profile_timing,
                            "total_sec": round(time.perf_counter() - run_started, 4),
                        },
                    }
                    return adapted
                except Exception as exc:  # pragma: no cover - defensive parsing path
                    errors.append(f"profile={profile_name} file={candidate} error={exc}")

            stderr_tail = self._read_log_tail(attempt_dir / "stderr.log")
            stdout_tail = self._read_log_tail(attempt_dir / "stdout.log")
            merged_log_tail = "\n".join(part for part in (stderr_tail, stdout_tail) if part)
            if self._contains_unrecoverable_error(merged_log_tail, _UNRECOVERABLE_PATTERNS):
                detail = self._extract_unrecoverable_line(merged_log_tail, _UNRECOVERABLE_PATTERNS)
                if detail is None:
                    detail = merged_log_tail[-500:] if merged_log_tail else "no stderr/stdout detail"
                errors.append(f"profile={profile_name} unrecoverable_log={detail}")
                errors.append(
                    f"ABORT: unrecoverable error detected in logs ({profile_name}), "
                    f"skipping remaining {len(profiles) - attempt_index} profile(s)"
                )
                break
            errors.append(f"profile={profile_name} no parse json output found in {attempt_dir}")

        if self.allow_mock_output:
            if engine == "paddle":
                raw = self._to_mock_paddle(selected_pages)
            else:
                raw = self._to_mock_mineru()
            adapted = self._adapt(engine, raw, str(source_pdf), selected_pages)
            adapted["parse_meta"] = {
                "mode": "mock",
                "profile": "mock",
                "attempt": len(profiles),
                "notes": "No parser JSON output found. Using mock output.",
                "timings": {
                    "total_sec": round(time.perf_counter() - run_started, 4),
                },
            }
            return adapted

        details = "; ".join(errors) if errors else "no parse output found"
        raise RuntimeError(f"ParseAgent failed for engine={engine}: {details}")
