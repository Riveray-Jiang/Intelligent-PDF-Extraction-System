from pathlib import Path

import pytest

from backend.product_server import JobRecord
from backend.product_server import VISUAL_AGENT_CACHE_VERSION
from backend.product_server import _looks_like_bad_reliable_override
from backend.product_server import _parse_multipart_form_data
from backend.product_server import ensure_run_allowed
from backend.product_server import load_visual_agent_cache_record
from backend.types import Block
from backend.types import Page


def _make_job(tmp_path: Path) -> JobRecord:
    job_dir = tmp_path / "job"
    job_dir.mkdir(parents=True, exist_ok=True)
    input_pdf = job_dir / "demo.pdf"
    input_pdf.write_bytes(b"%PDF-1.4\n")
    ingestion = {
        "page_count": 4,
        "pages": [{"page_index": index} for index in range(4)],
        "outline": [],
    }
    return JobRecord(
        job_id="job_demo",
        document_id="doc_demo",
        file_version=1,
        original_filename="demo.pdf",
        input_pdf=input_pdf,
        job_dir=job_dir,
        ingestion=ingestion,
    )


def test_ensure_run_allowed_rejects_repeat_fast(monkeypatch, tmp_path: Path) -> None:
    job = _make_job(tmp_path)

    monkeypatch.setattr("backend.product_server.completed_page_set_for_run_mode", lambda current_job, mode: {2} if mode == "fast" else set())

    with pytest.raises(RuntimeError, match="Fast extraction already exists for pages 2"):
        ensure_run_allowed(job, [2, 3], "fast")


def test_ensure_run_allowed_rejects_unavailable_or_repeat_repair(monkeypatch, tmp_path: Path) -> None:
    job = _make_job(tmp_path)

    monkeypatch.setattr("backend.product_server.current_output_page_set", lambda current_job: {1, 2, 3})
    monkeypatch.setattr("backend.product_server.completed_page_set_for_run_mode", lambda current_job, mode: {2} if mode == "reliable" else set())

    with pytest.raises(RuntimeError, match="already in the current output: 4"):
        ensure_run_allowed(job, [4], "reliable")

    with pytest.raises(RuntimeError, match="Repair already exists for pages 2"):
        ensure_run_allowed(job, [2], "reliable")


def test_load_visual_agent_cache_record_migrates_legacy_run_cache(tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    legacy_output_dir = job.job_dir / "runs" / "run_demo_fast_abc123" / "output"
    legacy_output_dir.mkdir(parents=True, exist_ok=True)
    legacy_cache_path = legacy_output_dir / "visual_agent_cache.json"
    legacy_cache_path.write_text(
        (
            "{\n"
            f'  "version": {VISUAL_AGENT_CACHE_VERSION},\n'
            '  "pages": {\n'
            '    "3": {\n'
            '      "generated": true,\n'
            '      "has_meaningful_visual": true,\n'
            '      "summary": "Legacy summary",\n'
            '      "markdown": "Legacy markdown",\n'
            '      "language": "en",\n'
            '      "visual_kind": "diagram"\n'
            "    }\n"
            "  }\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    record = load_visual_agent_cache_record(job, 3, output_dir=legacy_output_dir)

    assert record is not None
    assert record["summary"] == "Legacy summary"

    migrated_cache = job.job_dir / "visual_agent_cache.json"
    assert migrated_cache.exists()
    assert '"3"' in migrated_cache.read_text(encoding="utf-8")


def test_parse_multipart_form_data_reads_file_and_fields() -> None:
    boundary = "----codex-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="replaces_job_id"\r\n\r\n'
        "job_old\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="demo.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("ascii") + b"%PDF-1.4\n" + f"\r\n--{boundary}--\r\n".encode("ascii")

    fields, files = _parse_multipart_form_data(
        body,
        f"multipart/form-data; boundary={boundary}",
    )

    assert fields["replaces_job_id"] == "job_old"
    assert files["file"] == ("demo.pdf", b"%PDF-1.4\n")


def test_bad_reliable_override_is_detected_for_single_giant_table_page() -> None:
    fast_page = Page(
        page_index=0,
        width=1000,
        height=1400,
        blocks=[
            Block(id="title", type="text", text="建设项目环境影响报告表", bbox=[120, 120, 700, 180], order=0, page_index=0),
            Block(id="subtitle", type="text", text="（污染影响类）", bbox=[240, 200, 620, 250], order=1, page_index=0),
            Block(id="stamp", type="image", text="", bbox=[150, 480, 840, 660], order=2, page_index=0),
            Block(id="footer", type="text", text="中华人民共和国生态环境部制", bbox=[260, 980, 760, 1040], order=3, page_index=0),
        ],
    )
    reliable_page = Page(
        page_index=0,
        width=1000,
        height=1400,
        blocks=[
            Block(
                id="bad_table",
                type="table",
                text="<table><tr><td>wrong</td></tr></table>",
                bbox=[90, 100, 905, 907],
                order=0,
                page_index=0,
            )
        ],
    )

    assert _looks_like_bad_reliable_override(reliable_page, fast_page) is True


def test_bad_reliable_override_does_not_trigger_for_normal_table_page() -> None:
    fast_page = Page(
        page_index=0,
        width=1000,
        height=1400,
        blocks=[
            Block(id="intro", type="text", text="编制单位和编制人员情况表", bbox=[100, 80, 700, 130], order=0, page_index=0),
            Block(id="table", type="table", text="<table><tr><td>原始表格</td></tr></table>", bbox=[120, 150, 860, 1180], order=1, page_index=0),
        ],
    )
    reliable_page = Page(
        page_index=0,
        width=1000,
        height=1400,
        blocks=[
            Block(id="heading", type="text", text="编制单位和编制人员情况表", bbox=[100, 80, 700, 130], order=0, page_index=0),
            Block(id="table", type="table", text="<table><tr><td>抽取后的表格</td></tr></table>", bbox=[120, 150, 860, 1180], order=1, page_index=0),
            Block(id="section", type="text", text="一、建设单位情况", bbox=[120, 1210, 480, 1260], order=2, page_index=0),
        ],
    )

    assert _looks_like_bad_reliable_override(reliable_page, fast_page) is False
