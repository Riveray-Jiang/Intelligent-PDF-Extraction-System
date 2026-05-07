from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pypdf import PdfWriter
import yaml

from backend.parse_agent import ParseAgent


def _make_pdf(path: Path, pages: int = 1) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)


def test_parse_agent_mock_output_for_paddle(tmp_path) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump({"engines": {"paddle": {"retry_profiles": [{"name": "default"}]}}}),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=3)

    agent = ParseAgent(config_path=config_path, allow_mock_output=True)
    parsed = agent.run(
        engine="paddle",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": [0, 2]},
        output_dir=str(tmp_path / "out"),
    )

    assert parsed["parse_meta"]["mode"] == "mock"
    assert [page["page_index"] for page in parsed["pages"]] == [0, 2]


def test_parse_agent_preparsed_json_for_mineru(tmp_path) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump({"engines": {"mineru": {"retry_profiles": [{"name": "default"}]}}}),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=1)

    content_path = tmp_path / "content_list.json"
    content_path.write_text(
        json.dumps([{"type": "text", "content": "x", "bbox": [1, 2, 3, 4], "page_idx": 0}]),
        encoding="utf-8",
    )

    agent = ParseAgent(config_path=config_path, allow_mock_output=True)
    parsed = agent.run(
        engine="mineru",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": [0], "preparsed_json": str(content_path)},
        output_dir=str(tmp_path / "out"),
    )

    assert parsed["parse_meta"]["mode"] == "preparsed_json"
    assert parsed["mineru_content_list"][0]["page_idx"] == 0


def test_parse_agent_docker_profile_execution_paddle(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "paddle": {
                        "runtime": "docker",
                        "image": "pdf-agent/paddle-runner:0.1.0",
                        "command": "paddleocr doc_parser",
                        "retry_profiles": [
                            {
                                "name": "hpi_trt_fp16",
                                "enable_hpi": True,
                                "use_tensorrt": True,
                                "precision": "fp16",
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=1)

    seen_commands: list[list[str]] = []

    def fake_run(command, env=None, timeout_sec=None):  # noqa: ANN001
        seen_commands.append(list(command))
        assert timeout_sec == 1800
        output_dir = None
        for i, token in enumerate(command):
            if token == "-v" and i + 1 < len(command):
                mount = command[i + 1]
                if mount.endswith(":/output"):
                    output_dir = mount[: -len(":/output")]
                    break
        assert output_dir is not None
        out = tmp_path / "fake_out.json"
        out.write_text(
            json.dumps(
                {
                    "doc_id": "demo",
                    "source_file": str(pdf_path),
                    "pages": [
                        {
                            "page_index": 0,
                            "blocks": [{"id": "0", "type": "text", "text": "ok"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        # drop output inside mounted output dir so ParseAgent can discover it
        (Path(output_dir) / "result.json").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_subprocess", fake_run)

    parsed = agent.run(
        engine="paddle",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": [0]},
        output_dir=str(tmp_path / "out"),
    )

    assert parsed["parse_meta"]["mode"] == "json"
    assert parsed["parse_meta"]["profile"] == "hpi_trt_fp16"
    assert parsed["pages"][0]["blocks"][0]["text"] == "ok"
    assert seen_commands, "docker command should be executed"
    cmd = seen_commands[0]
    assert cmd[:4] == ["docker", "run", "--rm", "--gpus"]
    assert "pdf-agent/paddle-runner:0.1.0" in cmd
    assert "--enable_hpi" in cmd and "true" in cmd
    assert "--use_tensorrt" in cmd and "true" in cmd
    assert "--precision" in cmd and "fp16" in cmd


def test_parse_agent_aggregates_multiple_paddle_page_jsons(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "paddle": {
                        "runtime": "docker",
                        "image": "pdf-agent/paddle-runner:0.1.0",
                        "command": "paddleocr doc_parser",
                        "retry_profiles": [{"name": "no_hpi_no_trt_fp16", "precision": "fp16"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=3)

    def fake_run(command, env=None, timeout_sec=None):  # noqa: ANN001
        output_dir = None
        for i, token in enumerate(command):
            if token == "-v" and i + 1 < len(command):
                mount = command[i + 1]
                if mount.endswith(":/output"):
                    output_dir = mount[: -len(":/output")]
                    break
        assert output_dir is not None
        for page_idx in range(3):
            payload = {
                "parsing_res_list": [
                    {
                        "id": f"b{page_idx}",
                        "type": "text",
                        "text": f"p{page_idx}",
                    }
                ]
            }
            (Path(output_dir) / f"_selected_pages_input_{page_idx}_res.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_subprocess", fake_run)

    parsed = agent.run(
        engine="paddle",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": [0, 1, 2]},
        output_dir=str(tmp_path / "out"),
    )

    assert [page["page_index"] for page in parsed["pages"]] == [0, 1, 2]
    assert [page["blocks"][0]["text"] for page in parsed["pages"]] == ["p0", "p1", "p2"]
    assert isinstance(parsed["parse_meta"]["raw_path"], list)
    assert len(parsed["parse_meta"]["raw_path"]) == 3


def test_parse_agent_docker_profile_execution_mineru(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "mineru": {
                        "runtime": "docker",
                        "image": "pdf-agent/mineru-runner:0.1.0",
                        "command": "mineru",
                        "timeout_sec": 1200,
                        "retry_profiles": [
                            {
                                "name": "pipeline_cpu",
                                "backend": "pipeline",
                                "env": {"MINERU_DEVICE_MODE": "cpu"},
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=1)

    seen_commands: list[list[str]] = []
    def fake_run(command, env=None, timeout_sec=None):  # noqa: ANN001
        seen_commands.append(list(command))
        assert timeout_sec == 1200
        output_dir = None
        for i, token in enumerate(command):
            if token == "-v" and i + 1 < len(command):
                mount = command[i + 1]
                if mount.endswith(":/output"):
                    output_dir = mount[: -len(":/output")]
                    break
        assert output_dir is not None
        out = Path(output_dir) / "demo_content_list.json"
        out.write_text(
            json.dumps([{"page_idx": 0, "type": "text", "content": "mineru ok", "bbox": [1, 2, 3, 4]}]),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_subprocess", fake_run)

    parsed = agent.run(
        engine="mineru",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": [0]},
        output_dir=str(tmp_path / "out"),
    )

    assert parsed["parse_meta"]["mode"] == "json"
    assert parsed["parse_meta"]["profile"] == "pipeline_cpu"
    assert parsed["mineru_content_list"][0]["content"] == "mineru ok"
    assert seen_commands, "docker command should be executed"
    cmd = seen_commands[0]
    assert "pdf-agent/mineru-runner:0.1.0" in cmd
    assert "--gpus" not in cmd
    assert "-b" in cmd and "pipeline" in cmd
    assert "-p" in cmd and "/input/_selected_pages_input.pdf" in cmd
    assert "-e" in cmd and "MINERU_DEVICE_MODE=cpu" in cmd


def test_parse_agent_page_map_remaps_output_page_indices(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "paddle": {
                        "runtime": "docker",
                        "image": "pdf-agent/paddle-runner:0.1.0",
                        "command": "paddleocr doc_parser",
                        "retry_profiles": [{"name": "no_hpi_no_trt_fp32", "precision": "fp32"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=3)

    def fake_run(command, env=None, timeout_sec=None):  # noqa: ANN001
        output_dir = None
        for i, token in enumerate(command):
            if token == "-v" and i + 1 < len(command):
                mount = command[i + 1]
                if mount.endswith(":/output"):
                    output_dir = mount[: -len(":/output")]
                    break
        assert output_dir is not None
        (Path(output_dir) / "result.json").write_text(
            json.dumps(
                {
                    "pages": [
                        {
                            "page_index": 0,
                            "blocks": [{"id": "b0", "type": "text", "text": "ok", "page_index": 0}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_subprocess", fake_run)

    parsed = agent.run(
        engine="paddle",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": [2]},
        output_dir=str(tmp_path / "out"),
    )

    assert parsed["pages"][0]["page_index"] == 2
    assert parsed["parse_meta"]["selected_page_map"] == [2]


def test_parse_agent_page_map_keeps_all_paddle_pages_for_noncontiguous_selection(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "paddle": {
                        "runtime": "docker",
                        "image": "pdf-agent/paddle-runner:0.1.0",
                        "command": "paddleocr doc_parser",
                        "retry_profiles": [{"name": "no_hpi_no_trt_fp16", "precision": "fp16"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=30)
    selected_pages = [10, 11, 12, 19]

    def fake_run(command, env=None, timeout_sec=None):  # noqa: ANN001
        output_dir = None
        for i, token in enumerate(command):
            if token == "-v" and i + 1 < len(command):
                mount = command[i + 1]
                if mount.endswith(":/output"):
                    output_dir = mount[: -len(":/output")]
                    break
        assert output_dir is not None

        # Simulate local indices from the selected sub-PDF (0..3).
        for page_idx in range(4):
            payload = {
                "parsing_res_list": [
                    {
                        "id": f"b{page_idx}",
                        "type": "text",
                        "text": f"p{page_idx}",
                    }
                ]
            }
            (Path(output_dir) / f"_selected_pages_input_{page_idx}_res.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_subprocess", fake_run)

    parsed = agent.run(
        engine="paddle",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": selected_pages},
        output_dir=str(tmp_path / "out"),
    )

    assert [page["page_index"] for page in parsed["pages"]] == selected_pages
    assert [page["blocks"][0]["text"] for page in parsed["pages"]] == ["p0", "p1", "p2", "p3"]


def test_parse_agent_service_profile_execution_mineru(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "mineru": {
                        "runtime": "docker_service",
                        "image": "pdf-agent/mineru-runner:0.1.0",
                        "command": "mineru",
                        "service": {
                            "type": "mineru_api",
                            "host": "127.0.0.1",
                            "port": 19100,
                            "container_name": "pdf-agent-mineru-api",
                            "fallback_runtime": "docker",
                        },
                        "retry_profiles": [{"name": "pipeline_gpu_auto", "backend": "pipeline"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=2)

    def fake_service_profile(engine, engine_cfg, profile, source_pdf, attempt_dir, selected_pages):  # noqa: ANN001
        assert engine == "mineru"
        assert profile["name"] == "pipeline_gpu_auto"
        return (
            "service://mineru_api http://127.0.0.1:19100",
            [1],
            {"selected_pdf_sec": 0.01, "service_ensure_sec": 0.02, "service_run_sec": 0.03},
            {
                "content_list": [
                    {"page_idx": 0, "type": "text", "content": "service ok", "bbox": [1, 2, 3, 4]}
                ]
            },
        )

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_docker_service_profile", fake_service_profile)

    parsed = agent.run(
        engine="mineru",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": [1]},
        output_dir=str(tmp_path / "out"),
    )

    assert parsed["parse_meta"]["mode"] == "service_json"
    assert parsed["parse_meta"]["runtime"] == "docker_service"
    assert parsed["mineru_content_list"][0]["content"] == "service ok"
    assert parsed["mineru_content_list"][0]["page_idx"] == 1


def test_parse_agent_service_profile_execution_paddle(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "paddle": {
                        "runtime": "docker_service",
                        "image": "pdf-agent/paddle-runner:0.1.1-docx",
                        "command": "paddleocr pp_structurev3",
                        "service": {
                            "type": "paddle_worker",
                            "host": "127.0.0.1",
                            "port": 19101,
                            "container_name": "pdf-agent-paddle-ppstructurev3",
                            "worker_mode": "pp_structurev3",
                            "fallback_runtime": "docker",
                        },
                        "retry_profiles": [{"name": "pp_structurev3_fp16"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=1)

    def fake_service_profile(engine, engine_cfg, profile, source_pdf, attempt_dir, selected_pages):  # noqa: ANN001
        assert engine == "paddle"
        return (
            "service://paddle_worker http://127.0.0.1:19101",
            [0],
            {"selected_pdf_sec": 0.01, "service_ensure_sec": 0.02, "service_run_sec": 0.03},
            {
                "pages": [
                    {
                        "page_index": 0,
                        "blocks": [{"id": "0", "type": "text", "text": "paddle service ok"}],
                    }
                ]
            },
        )

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_docker_service_profile", fake_service_profile)

    parsed = agent.run(
        engine="paddle",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": [0]},
        output_dir=str(tmp_path / "out"),
    )

    assert parsed["parse_meta"]["mode"] == "service_json"
    assert parsed["parse_meta"]["runtime"] == "docker_service"
    assert parsed["pages"][0]["blocks"][0]["text"] == "paddle service ok"


def test_parse_agent_loads_paddle_service_json_batch_from_items() -> None:
    merged_pages = ParseAgent._load_paddle_json_batch_from_items(
        [
            {
                "name": "_selected_pages_input_1_res.json",
                "data": {"parsing_res_list": [{"id": "b1", "type": "text", "text": "p1"}]},
            },
            {
                "name": "_selected_pages_input_0_res.json",
                "data": {"parsing_res_list": [{"id": "b0", "type": "text", "text": "p0"}]},
            },
        ]
    )

    assert [page["page_index"] for page in merged_pages] == [0, 1]
    assert [page["parsing_res_list"][0]["text"] for page in merged_pages] == ["p0", "p1"]


def test_parse_agent_page_map_remaps_mineru_content_list_for_noncontiguous_selection(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "mineru": {
                        "runtime": "docker",
                        "image": "pdf-agent/mineru-runner:0.1.0",
                        "command": "mineru",
                        "retry_profiles": [{"name": "pipeline_gpu_auto", "backend": "pipeline"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=30)
    selected_pages = [10, 19]

    def fake_run(command, env=None, timeout_sec=None):  # noqa: ANN001
        output_dir = None
        for i, token in enumerate(command):
            if token == "-v" and i + 1 < len(command):
                mount = command[i + 1]
                if mount.endswith(":/output"):
                    output_dir = mount[: -len(":/output")]
                    break
        assert output_dir is not None
        out = Path(output_dir) / "_selected_pages_input_content_list.json"
        out.write_text(
            json.dumps(
                [
                    {"page_idx": 0, "type": "text", "content": "a", "bbox": [1, 2, 3, 4]},
                    {"page_idx": 1, "type": "text", "content": "b", "bbox": [1, 2, 3, 4]},
                ]
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_subprocess", fake_run)

    parsed = agent.run(
        engine="mineru",
        pdf_path=str(pdf_path),
        selection={"selected_page_indices": selected_pages},
        output_dir=str(tmp_path / "out"),
    )

    remapped_pages = [int(item["page_idx"]) for item in parsed["mineru_content_list"]]
    assert remapped_pages == selected_pages
    assert parsed["parse_meta"]["selected_page_map"] == selected_pages


def test_parse_agent_early_abort_on_unrecoverable_error(tmp_path, monkeypatch) -> None:
    """When a profile fails with an unrecoverable error (e.g. DependencyError),
    remaining profiles should be skipped instead of wasting time."""
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "paddle": {
                        "runtime": "docker",
                        "image": "pdf-agent/paddle-runner:0.1.0",
                        "command": "paddleocr doc_parser",
                        "retry_profiles": [
                            {"name": "profile_a", "precision": "fp16"},
                            {"name": "profile_b", "precision": "fp32"},
                            {"name": "profile_c", "precision": "fp32"},
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=1)

    call_count = 0

    def fake_run(command, env=None, timeout_sec=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        # Simulate an unrecoverable DependencyError from container
        return subprocess.CompletedProcess(
            command, 1, stdout="",
            stderr="DependencyError: `PaddleOCR-VL-1.5` requires additional dependencies."
        )

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_subprocess", fake_run)

    import pytest
    with pytest.raises(RuntimeError, match="ABORT: unrecoverable error"):
        agent.run(
            engine="paddle",
            pdf_path=str(pdf_path),
            selection={"selected_page_indices": [0]},
            output_dir=str(tmp_path / "out"),
        )

    # Only the first profile should have been attempted
    assert call_count == 1, f"Expected 1 attempt but got {call_count} (should abort after first)"


def test_parse_agent_early_abort_on_unrecoverable_log_without_json(tmp_path, monkeypatch) -> None:
    """Abort should also trigger when process exits 0 but stderr contains dependency errors."""
    config_path = tmp_path / "engines.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "engines": {
                    "mineru": {
                        "runtime": "docker",
                        "image": "pdf-agent/mineru-runner:0.1.0",
                        "command": "mineru",
                        "retry_profiles": [
                            {"name": "profile_a", "backend": "hybrid-auto-engine"},
                            {"name": "profile_b", "backend": "pipeline"},
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "demo.pdf"
    _make_pdf(pdf_path, pages=1)

    call_count = 0

    def fake_run(command, env=None, timeout_sec=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'pyclipper'",
        )

    agent = ParseAgent(config_path=config_path, allow_mock_output=False)
    monkeypatch.setattr(agent, "_run_subprocess", fake_run)

    import pytest

    with pytest.raises(RuntimeError, match="ABORT: unrecoverable error detected in logs"):
        agent.run(
            engine="mineru",
            pdf_path=str(pdf_path),
            selection={"selected_page_indices": [0]},
            output_dir=str(tmp_path / "out"),
        )

    assert call_count == 1, f"Expected 1 attempt but got {call_count} (should abort after first)"


def test_parse_agent_timeout_zero_means_unlimited() -> None:
    agent = ParseAgent(allow_mock_output=True)
    timeout = agent._profile_timeout_sec(
        engine_cfg={"timeout_sec": 1800},
        profile={"name": "unlimited", "timeout_sec": 0},
    )
    assert timeout is None
