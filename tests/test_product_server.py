import io
import json
import zipfile
from pathlib import Path

import pytest

from backend.product_server import JobRecord
from backend.product_server import ARTIFACT_FILENAMES
from backend.product_server import IMAGE_AGENT_CACHE_VERSION
from backend.product_server import _looks_like_bad_reliable_override
from backend.product_server import _parse_multipart_form_data
from backend.product_server import artifact_paths_for_output_dir
from backend.product_server import build_page_model
from backend.product_server import build_merged_output
from backend.product_server import build_merged_output_bundle
from backend.product_server import build_pipeline_command
from backend.product_server import compute_duration_sec
from backend.product_server import default_selection_mode
from backend.product_server import ensure_run_allowed
from backend.product_server import format_merged_page_markdown
from backend.product_server import load_image_agent_cache_record
from backend.product_server import load_document_ir
from backend.product_server import load_job_manifest
from backend.product_server import make_job_id
from backend.product_server import make_run_id
from backend.product_server import page_model_to_payload
from backend.product_server import parse_utc
from backend.product_server import read_document_job_manifests
from backend.product_server import read_run_insights
from backend.product_server import resolve_page_preview_output
from backend.product_server import sanitize_filename
from backend.product_server import utc_now
from backend.document_artifacts import ARTIFACT_FILENAMES as DOCUMENT_ARTIFACT_FILENAMES
from backend.document_artifacts import artifact_paths_for_output_dir as artifact_paths_for_output_dir_from_module
from backend.document_artifacts import build_page_model as build_page_model_from_module
from backend.document_artifacts import format_merged_page_markdown as format_merged_page_markdown_from_module
from backend.document_artifacts import load_document_ir as load_document_ir_from_module
from backend.document_artifacts import page_model_to_payload as page_model_to_payload_from_module
from backend.job_utils import compute_duration_sec as compute_duration_sec_from_module
from backend.job_utils import make_job_id as make_job_id_from_module
from backend.job_utils import make_run_id as make_run_id_from_module
from backend.job_utils import parse_utc as parse_utc_from_module
from backend.job_utils import sanitize_filename as sanitize_filename_from_module
from backend.job_utils import utc_now as utc_now_from_module
from backend.job_manifests import load_job_manifest as load_job_manifest_from_module
from backend.job_manifests import read_document_job_manifests as read_document_job_manifests_from_module
from backend.pipeline_command import build_pipeline_command as build_pipeline_command_from_module
from backend.pipeline_command import default_selection_mode as default_selection_mode_from_module
from backend.run_insights import read_run_insights as read_run_insights_from_module
from backend.image_agent_cache import IMAGE_AGENT_CACHE_VERSION as IMAGE_AGENT_CACHE_MODULE_VERSION
from backend.image_agent_cache import load_image_agent_cache_record as load_image_agent_cache_record_from_module
from backend.image_agent_preview import extract_image_agent_preview as extract_image_agent_preview_from_module
from backend.local_image_fallback import apply_local_image_fallback as apply_local_image_fallback_from_module
from backend.multipart_form import parse_multipart_form_data
from backend.product_server import extract_image_agent_preview
from backend.product_server import apply_local_image_fallback
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


def _write_document_ir(output_dir: Path, page_text_by_number: dict[int, str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for page_number, text in sorted(page_text_by_number.items()):
        page_index = page_number - 1
        pages.append(
            {
                "page_index": page_index,
                "blocks": [
                    {
                        "id": f"p{page_number}_text",
                        "type": "text",
                        "text": text,
                        "page_index": page_index,
                        "order": 0,
                    }
                ],
            }
        )
    payload = {
        "doc_id": "demo",
        "source_file": "demo.pdf",
        "source_engine": "test",
        "pages": pages,
    }
    (output_dir / "document_ir.json").write_text(json.dumps(payload), encoding="utf-8")


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


def test_load_image_agent_cache_record_migrates_legacy_run_cache(tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    legacy_output_dir = job.job_dir / "runs" / "run_demo_fast_abc123" / "output"
    legacy_output_dir.mkdir(parents=True, exist_ok=True)
    legacy_cache_path = legacy_output_dir / "image_agent_cache.json"
    legacy_cache_path.write_text(
        (
            "{\n"
            f'  "version": {IMAGE_AGENT_CACHE_VERSION},\n'
            '  "pages": {\n'
            '    "3": {\n'
            '      "generated": true,\n'
            '      "has_meaningful_image": true,\n'
            '      "summary": "Legacy summary",\n'
            '      "markdown": "Legacy markdown",\n'
            '      "language": "en",\n'
            '      "image_kind": "diagram"\n'
            "    }\n"
            "  }\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    record = load_image_agent_cache_record(job, 3, output_dir=legacy_output_dir)

    assert record is not None
    assert record["summary"] == "Legacy summary"

    migrated_cache = job.job_dir / "image_agent_cache.json"
    assert migrated_cache.exists()
    assert '"3"' in migrated_cache.read_text(encoding="utf-8")


def test_image_agent_cache_module_matches_product_server_compatibility_import(tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    legacy_output_dir = job.job_dir / "runs" / "run_demo_fast_abc123" / "output"
    legacy_output_dir.mkdir(parents=True, exist_ok=True)
    (legacy_output_dir / "image_agent_cache.json").write_text(
        json.dumps(
            {
                "version": IMAGE_AGENT_CACHE_MODULE_VERSION,
                "pages": {
                    "2": {
                        "generated": True,
                        "has_meaningful_image": True,
                        "summary": "Module summary",
                        "markdown": "Module markdown",
                        "language": "en",
                        "image_kind": "diagram",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert IMAGE_AGENT_CACHE_MODULE_VERSION == IMAGE_AGENT_CACHE_VERSION
    assert load_image_agent_cache_record_from_module(job, 2, output_dir=legacy_output_dir)[
        "summary"
    ] == load_image_agent_cache_record(job, 2, output_dir=legacy_output_dir)["summary"]


def test_image_agent_preview_module_matches_product_server_compatibility_import() -> None:
    page = Page(
        page_index=0,
        blocks=[
            Block(
                id="p0_image_agent",
                type="image_interpretation",
                text="Image reading markdown",
                page_index=0,
                source={
                    "language": "en",
                    "image_kind": "diagram",
                    "structured_output": {"summary": "Image reading summary"},
                },
            )
        ],
    )

    assert extract_image_agent_preview_from_module(page) == extract_image_agent_preview(page)


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


def test_multipart_form_module_matches_product_server_compatibility_import() -> None:
    boundary = "----codex-test-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="name"\r\n\r\n'
        "value\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="demo.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("ascii") + b"%PDF-1.4\n" + f"\r\n--{boundary}--\r\n".encode("ascii")
    content_type = f"multipart/form-data; boundary={boundary}"

    assert parse_multipart_form_data(body, content_type) == _parse_multipart_form_data(
        body,
        content_type,
    )


def test_local_image_fallback_module_matches_product_server_inert_hook(tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    page = Page(
        page_index=0,
        blocks=[Block(id="image", type="image", text="", page_index=0, order=0)],
    )
    output_dir = tmp_path / "output"

    assert apply_local_image_fallback_from_module(job, output_dir, 1, page) == page
    assert apply_local_image_fallback(job, output_dir, 1, page) == page


def test_document_artifact_module_matches_product_server_exports(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    document_ir_path = output_dir / "document_ir.json"
    page_payload = {
        "page_index": 0,
        "width": 612,
        "height": 792,
        "blocks": [
            {
                "id": "b1",
                "type": "text",
                "text": "hello",
                "bbox": [1, 2, 3, 4],
                "order": 0,
                "confidence": 0.9,
                "source": {"engine": "test"},
                "semantic_type": "body",
                "heading_level": None,
            }
        ],
    }
    document_ir = {"source_engine": "test", "pages": [page_payload]}
    document_ir_path.write_text(json.dumps(document_ir), encoding="utf-8")

    page_from_module = build_page_model_from_module(page_payload)
    page_from_server = build_page_model(page_payload)

    assert DOCUMENT_ARTIFACT_FILENAMES == ARTIFACT_FILENAMES
    assert artifact_paths_for_output_dir_from_module(output_dir) == artifact_paths_for_output_dir(output_dir)
    assert load_document_ir_from_module(document_ir_path) == load_document_ir(document_ir_path) == document_ir
    assert page_from_module == page_from_server
    assert page_model_to_payload_from_module(page_from_module) == page_model_to_payload(page_from_server)
    assert format_merged_page_markdown_from_module(2, "") == format_merged_page_markdown(2, "")


def test_job_utils_module_matches_product_server_exports() -> None:
    started_at = "2026-05-26T10:00:00Z"
    finished_at = "2026-05-26T10:00:02.500000Z"

    assert compute_duration_sec_from_module(started_at, finished_at) == compute_duration_sec(started_at, finished_at) == 2.5
    assert parse_utc_from_module("not-a-date") == parse_utc("not-a-date") is None
    assert sanitize_filename_from_module(" report v1/final?.pdf ") == sanitize_filename(" report v1/final?.pdf ") == "report_v1final.pdf"

    module_now = utc_now_from_module()
    server_now = utc_now()
    assert module_now.endswith("Z")
    assert server_now.endswith("Z")

    module_job_id = make_job_id_from_module()
    server_job_id = make_job_id()
    assert module_job_id.startswith("job_")
    assert server_job_id.startswith("job_")

    module_run_id = make_run_id_from_module("reliable")
    server_run_id = make_run_id("reliable")
    assert module_run_id.startswith("run_") and "_reliable_" in module_run_id
    assert server_run_id.startswith("run_") and "_reliable_" in server_run_id


def test_pipeline_command_module_matches_product_server_exports(tmp_path: Path) -> None:
    kwargs = {
        "input_pdf": tmp_path / "input.pdf",
        "output_dir": tmp_path / "output",
        "selection_mode": "pages",
        "selection": "1-2",
        "run_mode": "reliable",
        "engine": "mineru",
        "engine_config": tmp_path / "engines.yaml",
        "cascade_engine": "paddle",
        "cascade_engine_config": tmp_path / "repair.yaml",
        "max_parse_attempts": 2,
        "max_rerun_attempts": 1,
        "max_cascade_attempts": 1,
    }

    assert default_selection_mode_from_module({"pages": []}) == default_selection_mode({"pages": []}) == "all"
    assert build_pipeline_command_from_module(**kwargs) == build_pipeline_command(**kwargs)


def test_run_insights_module_matches_product_server_export(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    job = _make_job(tmp_path)
    output_dir = job.default_output_dir
    output_dir.mkdir(parents=True)
    (output_dir / "pipeline_state.json").write_text(
        json.dumps(
            {
                "cascade_attempt": 2,
                "image_agent": {
                    "image_pages_detected": 3,
                    "image_pages_enriched": 2,
                    "image_pages_failed": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "validation_report.json").write_text(
        json.dumps({"failed_pages": [{"page": 1}, {"page": 4}]}),
        encoding="utf-8",
    )

    assert read_run_insights_from_module(job) == read_run_insights(job)


def test_job_manifests_module_matches_product_server_exports(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "jobs"
    monkeypatch.setattr("backend.product_server.DATA_ROOT", data_root)
    (data_root / "job_v1").mkdir(parents=True)
    (data_root / "job_v2").mkdir(parents=True)
    (data_root / "other").mkdir(parents=True)
    (data_root / "job_v1" / "job_manifest.json").write_text(
        json.dumps(
            {
                "job_id": "job_v1",
                "document_id": "doc_demo",
                "file_version": 1,
                "created_at": "2026-05-26T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (data_root / "job_v2" / "job_manifest.json").write_text(
        json.dumps(
            {
                "job_id": "job_v2",
                "document_id": "doc_demo",
                "file_version": 2,
                "created_at": "2026-05-26T11:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (data_root / "other" / "job_manifest.json").write_text(
        json.dumps({"job_id": "other", "document_id": "other_doc", "file_version": 1}),
        encoding="utf-8",
    )

    assert load_job_manifest_from_module("job_v1", data_root=data_root) == load_job_manifest("job_v1")
    assert read_document_job_manifests_from_module("doc_demo", data_root=data_root) == read_document_job_manifests("doc_demo")


def test_build_merged_output_bundle_contains_final_document(monkeypatch, tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    merged_ir = {
        "doc_id": "demo",
        "source_file": "demo.pdf",
        "pages": [
            {
                "page_index": 0,
                "blocks": [
                    {
                        "id": "p1_text",
                        "type": "text",
                        "text": "Hello output",
                        "page_index": 0,
                        "order": 0,
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(
        "backend.product_server.build_merged_output",
        lambda current_job: (merged_ir, "Hello output") if current_job is job else None,
    )

    bundle = build_merged_output_bundle(job)

    assert bundle is not None
    data, filename = bundle
    assert filename == "demo_output.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert {"document.md", "document_ir.json", "metadata.json", "pages/page_0001.md"} <= names
        assert archive.read("document.md").decode("utf-8") == "Hello output"
        metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
        assert metadata["output_pages"] == [1]


def test_merged_output_uses_only_requested_repair_page(monkeypatch, tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    fast_output_dir = tmp_path / "fast" / "output"
    repair_output_dir = tmp_path / "repair" / "output"
    _write_document_ir(
        fast_output_dir,
        {
            1: "FAST P1",
            2: "FAST P2",
            3: "FAST P3",
            4: "FAST P4",
        },
    )
    _write_document_ir(
        repair_output_dir,
        {
            2: "REPAIRED P2",
            3: "CONTEXT P3 SHOULD NOT ENTER FINAL OUTPUT",
        },
    )

    monkeypatch.setattr(
        "backend.product_server.completed_history_entries",
        lambda current_job: [
            {
                "run_id": "run_reliable",
                "run_mode": "reliable",
                "status": "completed",
                "selection_mode": "pagerange",
                "selection": "2",
                "output_dir": str(repair_output_dir),
            },
            {
                "run_id": "run_fast",
                "run_mode": "fast",
                "status": "completed",
                "selection_mode": "all",
                "selection": None,
                "output_dir": str(fast_output_dir),
            },
        ],
    )

    merged = build_merged_output(job)

    assert merged is not None
    merged_ir, merged_markdown = merged
    assert [page["page_index"] for page in merged_ir["pages"]] == [0, 1, 2, 3]
    assert "## Page 1" in merged_markdown
    assert "## Page 2" in merged_markdown
    assert "## Page 3" in merged_markdown
    assert "REPAIRED P2" in merged_markdown
    assert "FAST P3" in merged_markdown
    assert "CONTEXT P3 SHOULD NOT ENTER FINAL OUTPUT" not in merged_markdown


def test_merged_output_keeps_adjacent_table_pages_separate(monkeypatch, tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    output_dir = tmp_path / "fast_tables" / "output"
    payload = {
        "doc_id": "demo",
        "source_file": "demo.pdf",
        "source_engine": "test",
        "pages": [
            {
                "page_index": 0,
                "blocks": [
                    {
                        "id": "p1_table",
                        "type": "table",
                        "text": "<table><tr><td>first page table</td></tr></table>",
                        "page_index": 0,
                        "order": 0,
                    }
                ],
            },
            {
                "page_index": 1,
                "blocks": [
                    {
                        "id": "p2_table",
                        "type": "table",
                        "text": "<table><tr><td>second page table</td></tr></table>",
                        "page_index": 1,
                        "order": 0,
                    }
                ],
            },
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "document_ir.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        "backend.product_server.completed_history_entries",
        lambda current_job: [
            {
                "run_id": "run_fast",
                "run_mode": "fast",
                "status": "completed",
                "selection_mode": "pagerange",
                "selection": "1-2",
                "output_dir": str(output_dir),
            },
        ],
    )

    merged = build_merged_output(job)

    assert merged is not None
    _, merged_markdown = merged
    assert (
        "<table><tr><td>first page table</td></tr></table>\n\n"
        "## Page 2\n\n"
        "<table><tr><td>second page table</td></tr></table>"
    ) in merged_markdown


def test_page_preview_without_run_id_uses_effective_page_run(monkeypatch, tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    job.output_dir = str(tmp_path / "last_repair" / "output")
    job.run_id = "run_last_repair"
    fast_output_dir = tmp_path / "fast" / "output"
    repair_output_dir = tmp_path / "repair" / "output"
    _write_document_ir(fast_output_dir, {1: "FAST P1", 2: "FAST P2"})
    _write_document_ir(repair_output_dir, {1: "REPAIRED P1"})

    monkeypatch.setattr(
        "backend.product_server.completed_history_entries",
        lambda current_job: [
            {
                "run_id": "run_reliable_p1",
                "run_mode": "reliable",
                "status": "completed",
                "selection_mode": "pagerange",
                "selection": "1",
                "output_dir": str(repair_output_dir),
            },
            {
                "run_id": "run_fast",
                "run_mode": "fast",
                "status": "completed",
                "selection_mode": "pagerange",
                "selection": "1-2",
                "output_dir": str(fast_output_dir),
            },
        ],
    )

    output_dir, run_id = resolve_page_preview_output(job, 1)

    assert output_dir == repair_output_dir
    assert run_id == "run_reliable_p1"

    output_dir, run_id = resolve_page_preview_output(job, 2)

    assert output_dir == fast_output_dir
    assert run_id == "run_fast"


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
