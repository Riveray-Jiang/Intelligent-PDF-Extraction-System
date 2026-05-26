from __future__ import annotations

import base64
import inspect
import io
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import pypdfium2 as pdfium
from pydantic import BaseModel, Field

from .pdfium_runtime import PDFIUM_LOCK
from .types import Block
from .types import DocumentIR
from .types import Page

IMAGE_BLOCK_TYPES = {
    "image",
    "figure",
    "figure_title",
    "image_body",
    "image_caption",
}
IMAGE_AGENT_BLOCK_TYPE = "image_interpretation"
IMAGE_AGENT_LANGUAGE_ZH = "zh"
IMAGE_AGENT_LANGUAGE_EN = "en"
IMAGE_AGENT_KIND_MAP = "map"
IMAGE_AGENT_KIND_WORKFLOW = "workflow"
IMAGE_AGENT_KIND_TABLE = "table"
IMAGE_AGENT_KIND_DIAGRAM = "diagram"
IMAGE_AGENT_PROMPT_VERSION = "image-agent-v4-progressive-image-reading"

_LOCALIZED_COPY = {
    IMAGE_AGENT_LANGUAGE_ZH: {
        "key_elements": "关键内容：",
        "relationships": "关系/流程：",
        "notes": "说明：",
    },
    IMAGE_AGENT_LANGUAGE_EN: {
        "key_elements": "Key elements:",
        "relationships": "Flow or relationships:",
        "notes": "Notes:",
    },
}


def _load_repo_env_file() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    env_path = repo_root / ".env"
    if not env_path.exists():
        return

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_repo_env_file()


def block_type_has_image_content(block_type: str) -> bool:
    return block_type.strip().lower() in IMAGE_BLOCK_TYPES


def page_has_image_content(page: Page) -> bool:
    return any(block_type_has_image_content(block.type) for block in page.blocks)


class ImageInterpretationPayload(BaseModel):
    has_meaningful_image: bool = False
    summary: str = ""
    key_elements: list[str] = Field(default_factory=list)
    relationships_or_flow: list[str] = Field(default_factory=list)
    notes_or_uncertainty: list[str] = Field(default_factory=list)


class ImageAgent:
    """Use a vision-capable model to enrich image-heavy pages."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-4o",
        render_dpi: int = 150,
        timeout_sec: float = 45.0,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
        self.model = model
        self.render_dpi = render_dpi
        self.timeout_sec = timeout_sec

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def capability_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "name": "Image Agent",
            "model": self.model if self.enabled else None,
            "image_pages_detected": 0,
            "image_pages_enriched": 0,
            "image_pages_failed": 0,
        }

    def enrich_document(
        self,
        document_ir: DocumentIR,
        *,
        pdf_path: str | Path,
        page_indices: list[int] | None = None,
    ) -> tuple[DocumentIR, dict[str, Any]]:
        target_pages = {int(page_index) for page_index in page_indices} if page_indices is not None else None
        stats = self.capability_snapshot()
        if target_pages is not None and not target_pages:
            return document_ir, stats

        source_pdf = Path(pdf_path).resolve()
        source_name = source_pdf.name
        pdf_doc: pdfium.PdfDocument | None = None
        pages_out: list[Page] = []
        try:
            for page in document_ir.pages:
                if target_pages is not None and page.page_index not in target_pages:
                    pages_out.append(page)
                    continue

                cleaned_page = self._strip_image_agent_blocks(page)
                if not page_has_image_content(cleaned_page):
                    pages_out.append(cleaned_page)
                    continue

                stats["image_pages_detected"] = int(stats["image_pages_detected"]) + 1
                if not self.enabled:
                    pages_out.append(cleaned_page)
                    continue

                try:
                    with PDFIUM_LOCK:
                        if pdf_doc is None:
                            pdf_doc = pdfium.PdfDocument(str(source_pdf))
                        image_data_url = self._render_page_data_url(pdf_doc, cleaned_page.page_index)
                    interpretation = self._request_image_interpretation_for_page(
                        image_data_url,
                        cleaned_page,
                        source_name=source_name,
                    )
                    if interpretation.has_meaningful_image:
                        cleaned_page = self._append_image_agent_block(
                            cleaned_page,
                            interpretation,
                            source_name=source_name,
                        )
                        stats["image_pages_enriched"] = int(stats["image_pages_enriched"]) + 1
                except Exception:
                    stats["image_pages_failed"] = int(stats["image_pages_failed"]) + 1
                pages_out.append(cleaned_page)
        finally:
            if pdf_doc is not None:
                try:
                    pdf_doc.close()
                except Exception:
                    pass

        updated = document_ir.model_copy(update={"pages": pages_out})
        return updated, stats

    def generate_page_record(
        self,
        page: Page,
        *,
        pdf_path: str | Path,
        source_name: str | None = None,
        force: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        stats = self.capability_snapshot()
        cleaned_page = self._strip_image_agent_blocks(page)
        source_filename = source_name or Path(pdf_path).name
        language = self._infer_output_language(cleaned_page, source_name=source_filename)
        image_kind = self._infer_image_kind(cleaned_page)
        record: dict[str, Any] = {
            "generated": True,
            "has_meaningful_image": False,
            "summary": None,
            "markdown": None,
            "language": language,
            "image_kind": image_kind,
            "model": self.model if self.enabled else None,
            "prompt_version": IMAGE_AGENT_PROMPT_VERSION,
        }

        if not force and not page_has_image_content(cleaned_page):
            record["generated"] = False
            return record, stats

        stats["image_pages_detected"] = 1
        if not self.enabled:
            record["generated"] = False
            return record, stats

        pdf_doc: pdfium.PdfDocument | None = None
        try:
            with PDFIUM_LOCK:
                pdf_doc = pdfium.PdfDocument(str(Path(pdf_path).resolve()))
                image_data_url = self._render_page_data_url(pdf_doc, cleaned_page.page_index)
            interpretation = self._request_image_interpretation_for_page(
                image_data_url,
                cleaned_page,
                source_name=source_filename,
            )
            if interpretation.has_meaningful_image:
                record.update(
                    {
                        "has_meaningful_image": True,
                        "summary": " ".join(interpretation.summary.split()).strip() or None,
                        "markdown": self._format_interpretation_markdown(
                            interpretation,
                            language=language,
                            image_kind=image_kind,
                        )
                        or None,
                    }
                )
                stats["image_pages_enriched"] = 1
            return record, stats
        except Exception:
            stats["image_pages_failed"] = 1
            raise
        finally:
            if pdf_doc is not None:
                try:
                    pdf_doc.close()
                except Exception:
                    pass

    def _request_image_interpretation(
        self,
        image_data_url: str,
        page: Page,
        *,
        source_name: str | None = None,
    ) -> ImageInterpretationPayload:
        language = self._infer_output_language(page, source_name=source_name)
        prompt = self._build_prompt(page, source_name=source_name)
        result = self._post_image_interpretation_request(image_data_url, prompt, language=language)
        if not result.has_meaningful_image and self._should_retry_empty_image_reading(page):
            retry_prompt = (
                prompt
                + "\n\nEmpty-response correction: the user clicked Image Agent to progressively disclose the "
                "original visual itself. Do not return has_meaningful_image=false for site photos, factory photos, "
                "equipment photos, screenshots, scanned forms, stamps, signatures, maps, charts, floor plans, "
                "layouts, tables rendered as images, or process diagrams. If the visual has no deep inference, "
                "still provide a concise visible-content reading. Return false only for blank, purely decorative, "
                "logo-only, watermark-only, border-only, or repeated letterhead/header/footer content."
            )
            result = self._post_image_interpretation_request(image_data_url, retry_prompt, language=language)
        if not self._payload_matches_language(result, language):
            retry_prompt = (
                prompt
                + "\n\nLanguage correction: your previous response mixed languages. "
                + self._language_lock_instruction(language)
            )
            result = self._post_image_interpretation_request(image_data_url, retry_prompt, language=language)
        if not self._payload_matches_language(result, language):
            result = self._rewrite_payload_language(result, language=language)
        if not self._payload_matches_language(result, language):
            raise ValueError("Image Agent returned mixed-language output")
        return result

    @staticmethod
    def _should_retry_empty_image_reading(page: Page) -> bool:
        if page_has_image_content(page):
            return True
        haystack = "\n".join(
            " ".join(block.text.split()).strip()
            for block in page.blocks
            if block.text.strip()
        ).lower()
        return any(
            keyword in haystack
            for keyword in (
                "figure",
                "image",
                "photo",
                "map",
                "chart",
                "diagram",
                "plan",
                "layout",
                "flow",
                "stamp",
                "signature",
                "图",
                "照片",
                "现场",
                "平面",
                "流程",
                "示意",
                "布置",
                "分布",
                "签章",
                "印章",
            )
        )

    def _post_image_interpretation_request(
        self,
        image_data_url: str,
        prompt: str,
        *,
        language: str,
    ) -> ImageInterpretationPayload:
        payload = {
            "model": self.model,
            "instructions": (
                "You are Image Agent. Analyze meaningful page images and graphics in a PDF. "
                f"{self._language_lock_instruction(language)} "
                "This is on-demand progressive disclosure: the user clicked because normal Markdown cannot fully "
                "expose the original image, map, figure, plan, chart, form, stamp, or diagram. Provide a full "
                "image reading, not a caption and not a generic summary. Extract the important visible content "
                "from maps, site plans, floor plans, hazard or control-zone distributions, process flows, water or "
                "material balance diagrams, charts, plots, scanned tables, forms, stamps, signatures, layouts, "
                "screenshots, photos, and scientific figures. Be concrete and grounded in what is visible. Do not "
                "rewrite the whole surrounding page text. Ignore decorative logos, letterheads, borders, and "
                "watermarks unless they carry document meaning. Preserve readable labels, values, units, arrows, "
                "axes, legends, colors, stamps, signatures, and spatial relationships. If something is unclear, "
                "say so instead of guessing."
            ),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                            "detail": "high",
                        },
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "image_page_interpretation",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "has_meaningful_image": {"type": "boolean"},
                            "summary": {"type": "string"},
                            "key_elements": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "relationships_or_flow": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "notes_or_uncertainty": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "has_meaningful_image",
                            "summary",
                            "key_elements",
                            "relationships_or_flow",
                            "notes_or_uncertainty",
                        ],
                    },
                }
            },
            "max_output_tokens": 1200,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            response = client.post(self.endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        output_text = data.get("output_text")
        if not isinstance(output_text, str) or not output_text.strip():
            output_text = self._extract_output_text(data)
        if not output_text.strip():
            raise ValueError("Image Agent returned no structured output")
        return ImageInterpretationPayload.model_validate(json.loads(output_text))

    def _rewrite_payload_language(
        self,
        payload: ImageInterpretationPayload,
        *,
        language: str,
    ) -> ImageInterpretationPayload:
        if not payload.has_meaningful_image:
            return payload
        language_name = "Simplified Chinese" if language == IMAGE_AGENT_LANGUAGE_ZH else "English"
        request_payload = {
            "model": self.model,
            "instructions": (
                f"Rewrite the provided Image Agent JSON into {language_name}. "
                "Do not add new facts. Preserve numbers, units, proper nouns, visible labels, arrows, and uncertainty. "
                f"{self._language_lock_instruction(language)}"
            ),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(payload.model_dump(), ensure_ascii=False),
                        }
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "image_page_interpretation_language_rewrite",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "has_meaningful_image": {"type": "boolean"},
                            "summary": {"type": "string"},
                            "key_elements": {"type": "array", "items": {"type": "string"}},
                            "relationships_or_flow": {"type": "array", "items": {"type": "string"}},
                            "notes_or_uncertainty": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "has_meaningful_image",
                            "summary",
                            "key_elements",
                            "relationships_or_flow",
                            "notes_or_uncertainty",
                        ],
                    },
                }
            },
            "max_output_tokens": 900,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            response = client.post(self.endpoint, headers=headers, json=request_payload)
            response.raise_for_status()
            data = response.json()
        output_text = data.get("output_text")
        if not isinstance(output_text, str) or not output_text.strip():
            output_text = self._extract_output_text(data)
        if not output_text.strip():
            return payload
        return ImageInterpretationPayload.model_validate(json.loads(output_text))

    def _request_image_interpretation_for_page(
        self,
        image_data_url: str,
        page: Page,
        *,
        source_name: str | None = None,
    ) -> ImageInterpretationPayload:
        signature = inspect.signature(self._request_image_interpretation)
        accepts_source_name = "source_name" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if accepts_source_name:
            return self._request_image_interpretation(
                image_data_url,
                page,
                source_name=source_name,
            )
        return self._request_image_interpretation(image_data_url, page)

    @staticmethod
    def _extract_output_text(response_payload: dict[str, Any]) -> str:
        pieces: list[str] = []
        for item in response_payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for content_item in item.get("content", []):
                if not isinstance(content_item, dict):
                    continue
                if content_item.get("type") == "output_text":
                    text = str(content_item.get("text", "")).strip()
                    if text:
                        pieces.append(text)
        return "\n".join(pieces).strip()

    def _render_page_data_url(self, pdf_doc: pdfium.PdfDocument, page_index: int) -> str:
        page = pdf_doc[page_index]
        bitmap = page.render(scale=max(float(self.render_dpi) / 72.0, 0.1))
        image = bitmap.to_pil().convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=82, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    @staticmethod
    def _strip_image_agent_blocks(page: Page) -> Page:
        return page.model_copy(
            update={
                "blocks": [
                    block for block in page.blocks if block.type.strip().lower() != IMAGE_AGENT_BLOCK_TYPE
                ]
            }
        )

    def _append_image_agent_block(
        self,
        page: Page,
        interpretation: ImageInterpretationPayload,
        *,
        source_name: str | None = None,
    ) -> Page:
        language = self._infer_output_language(page, source_name=source_name)
        image_kind = self._infer_image_kind(page)
        block = Block(
            id=f"p{page.page_index}_image_agent",
            type=IMAGE_AGENT_BLOCK_TYPE,
            text=self._format_interpretation_markdown(
                interpretation,
                language=language,
                image_kind=image_kind,
            ),
            source={
                "agent": "image-agent",
                "model": self.model,
                "prompt_version": IMAGE_AGENT_PROMPT_VERSION,
                "language": language,
                "image_kind": image_kind,
                "structured_output": interpretation.model_dump(),
            },
            page_index=page.page_index,
        )
        sorted_blocks = _sorted_blocks_for_insertion(page.blocks)
        insert_after = -1
        for index, existing_block in enumerate(sorted_blocks):
            if block_type_has_image_content(existing_block.type):
                insert_after = index

        if insert_after >= 0:
            updated_blocks = [*sorted_blocks[: insert_after + 1], block, *sorted_blocks[insert_after + 1 :]]
        else:
            updated_blocks = [*sorted_blocks, block]

        normalized_blocks: list[Block] = []
        for order, existing_block in enumerate(updated_blocks):
            normalized_blocks.append(existing_block.model_copy(update={"order": order}))
        return page.model_copy(update={"blocks": normalized_blocks})

    @staticmethod
    def _clean_lines(values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            text = " ".join(str(value).split()).strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _normalize_compare_text(value: str) -> str:
        return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()

    def _dedupe_lines(
        self,
        values: list[str],
        *,
        against: list[str] | None = None,
        max_items: int,
    ) -> list[str]:
        normalized_against = [self._normalize_compare_text(value) for value in (against or []) if value]
        result: list[str] = []
        seen: set[str] = set()
        for value in self._clean_lines(values):
            normalized = self._normalize_compare_text(value)
            if not normalized or normalized in seen:
                continue
            if any(normalized in other or other in normalized for other in normalized_against if other):
                continue
            seen.add(normalized)
            result.append(value)
            if len(result) >= max_items:
                break
        return result

    def _build_prompt(self, page: Page, *, source_name: str | None = None) -> str:
        cues = self._page_cues(page)
        language = self._infer_output_language(page, source_name=source_name)
        image_kind = self._infer_image_kind(page)
        language_label = "Simplified Chinese" if language == IMAGE_AGENT_LANGUAGE_ZH else "English"
        kind_instruction = self._image_kind_prompt_instruction(image_kind)
        cue_text = "\n".join(f"- {cue}" for cue in cues) if cues else "- None"
        return (
            f"Page language: {language_label}.\n"
            f"{self._language_lock_instruction(language)}\n"
            f"Likely image type: {image_kind}.\n"
            "Decide whether this PDF page contains meaningful image information beyond decorative elements.\n"
            "Return has_meaningful_image=false only for blank, purely decorative, logo-only, watermark-only, border-only, or repeated letterhead/header/footer visuals.\n"
            "Photos, site photos, factory photos, equipment photos, screenshots, scanned forms, stamps, signatures, maps, charts, floor plans, layouts, tables rendered as images, and process diagrams are meaningful even when a caption already exists.\n"
            "If yes, produce an on-demand full image reading that helps a user audit the original page.\n"
            "Output format rules are strict:\n"
            "- summary: exactly 1 short sentence. Explain the whole image, its document purpose, and the main takeaway. This must be more useful than a figure caption.\n"
            "- key_elements: 4 to 10 concrete items when visible. Preserve important labels, named regions, floors, rooms, nodes, axes, categories, legends, colors, measured values, units, stamps, signatures, locations, or objects. Do not include generic furniture.\n"
            "- relationships_or_flow: 2 to 10 factual items when visible. Capture arrows, sequence, source-to-target flow, spatial relationship, trend, comparison, hierarchy, grouping, risk concentration, dependency, or before/after relation.\n"
            "- notes_or_uncertainty: 0 to 3 items. Use only for unreadable text, ambiguous arrows, cropped content, low confidence, or limits of the image.\n"
            "Do not fill fields with boilerplate. Empty arrays are preferred over repetitive filler. Avoid vague phrases like 'the diagram shows information'. If the image is simple, be short; if it contains dense flow/data/layout information, be usefully detailed.\n"
            "Do not repeat surrounding OCR/body text. Do preserve text inside the image when it is needed to understand the image.\n"
            "For numbers, keep units exactly as visible when readable. For arrows, prefer A -> B wording. For maps/plans, name the subject, boundary or zone relation, marked location, legend meaning, and where important areas cluster when visible. For flow or balance diagrams, do not stop at naming nodes; walk through every visible branch from source to sink.\n"
            f"{kind_instruction}\n"
            "Use these extracted page cues only as hints:\n"
            f"{cue_text}"
        )

    @staticmethod
    def _language_lock_instruction(language: str) -> str:
        if language == IMAGE_AGENT_LANGUAGE_ZH:
            return (
                "Output language lock: write summary, key_elements, relationships_or_flow, and "
                "notes_or_uncertainty entirely in Simplified Chinese. Do not use English prose. "
                "Translate labels like red mark, flattened area, map, warehouse, or flow into Chinese unless the "
                "English phrase is an exact original label visible in the image."
            )
        return (
            "Output language lock: write summary, key_elements, relationships_or_flow, and "
            "notes_or_uncertainty entirely in English. Do not use Chinese prose unless it is an exact original "
            "label visible in the image."
        )

    @staticmethod
    def _payload_language_text(payload: ImageInterpretationPayload) -> str:
        return "\n".join(
            [
                payload.summary,
                *payload.key_elements,
                *payload.relationships_or_flow,
                *payload.notes_or_uncertainty,
            ]
        )

    def _payload_matches_language(self, payload: ImageInterpretationPayload, language: str) -> bool:
        if not payload.has_meaningful_image:
            return True
        text = self._payload_language_text(payload)
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
        latin_words = re.findall(r"[A-Za-z]{3,}", text)
        if language == IMAGE_AGENT_LANGUAGE_ZH:
            if cjk_count < 8:
                return False
            # Allow exact labels, units, and abbreviations, but reject English prose dominating the answer.
            return len(latin_words) <= max(6, cjk_count // 8)
        if cjk_count > 12:
            return False
        return True

    @staticmethod
    def _page_cues(page: Page) -> list[str]:
        cues: list[str] = []
        fallback: list[str] = []
        for block in page.blocks:
            block_type = block.type.strip().lower()
            text = " ".join(block.text.split()).strip()
            if not text:
                continue
            if block_type in {"figure_title", "image_caption", "paragraph_title", "title", "section_title"}:
                cues.append(text[:220])
            elif block_type in {"text", "paragraph", "body", "table"} and len(fallback) < 4:
                fallback.append(text[:220])
            if len(cues) >= 6:
                break
        if len(cues) < 6:
            cues.extend(fallback[: 6 - len(cues)])
        return cues[:6]

    def _format_interpretation_markdown(
        self,
        interpretation: ImageInterpretationPayload,
        *,
        language: str,
        image_kind: str,
    ) -> str:
        sections: list[str] = []
        copy = _LOCALIZED_COPY.get(language, _LOCALIZED_COPY[IMAGE_AGENT_LANGUAGE_EN])
        summary = " ".join(interpretation.summary.split()).strip()
        if summary:
            sections.append(summary)

        key_elements = self._dedupe_lines(interpretation.key_elements, against=[summary], max_items=10)
        if key_elements:
            sections.append(copy["key_elements"] + "\n" + "\n".join(f"- {item}" for item in key_elements))

        relationships = self._dedupe_lines(
            interpretation.relationships_or_flow,
            against=[summary, *key_elements],
            max_items=self._relationship_item_limit(image_kind),
        )
        if relationships:
            sections.append(copy["relationships"] + "\n" + "\n".join(f"- {item}" for item in relationships))

        notes = self._dedupe_lines(
            interpretation.notes_or_uncertainty,
            against=[summary, *key_elements, *relationships],
            max_items=3,
        )
        if notes:
            sections.append(copy["notes"] + "\n" + "\n".join(f"- {item}" for item in notes))

        return "\n\n".join(sections).strip()

    @staticmethod
    def _relationship_item_limit(image_kind: str) -> int:
        if image_kind == IMAGE_AGENT_KIND_WORKFLOW:
            return 10
        if image_kind == IMAGE_AGENT_KIND_MAP:
            return 1
        return 4

    def _infer_output_language(self, page: Page, *, source_name: str | None = None) -> str:
        sample = "\n".join(self._page_cues(page))
        if not sample:
            sample = "\n".join(
                " ".join(block.text.split()).strip()
                for block in page.blocks
                if block.type.strip().lower() != IMAGE_AGENT_BLOCK_TYPE and block.text.strip()
            )
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", sample))
        latin_count = len(re.findall(r"[A-Za-z]", sample))
        if cjk_count >= 8 and cjk_count >= max(4, int(latin_count * 0.35)):
            return IMAGE_AGENT_LANGUAGE_ZH
        if latin_count >= 20 and latin_count >= cjk_count * 2:
            return IMAGE_AGENT_LANGUAGE_EN

        if source_name:
            source_stem = Path(source_name).stem
            if re.search(r"[\u3400-\u9fff]", source_stem):
                return IMAGE_AGENT_LANGUAGE_ZH
            if re.search(r"[A-Za-z]", source_stem):
                return IMAGE_AGENT_LANGUAGE_EN
        return IMAGE_AGENT_LANGUAGE_EN

    def _infer_image_kind(self, page: Page) -> str:
        haystack = "\n".join(self._page_cues(page)).lower()
        if any(
            keyword in haystack
            for keyword in (
                "map",
                "location",
                "site",
                "region",
                "distribution",
                "plan",
                "layout",
                "boundary",
                "zone",
                "所在地区",
                "项目所在地",
                "位置图",
                "区位",
                "分布图",
                "红线",
                "生态",
                "总平面",
                "平面布置",
                "厂区",
                "场地",
            )
        ):
            return IMAGE_AGENT_KIND_MAP
        if any(
            keyword in haystack
            for keyword in (
                "workflow",
                "flow",
                "process",
                "pipeline",
                "balance",
                "water balance",
                "material balance",
                "sankey",
                "步骤",
                "流程",
                "工艺",
                "路线",
                "示意流程",
                "水平衡",
                "物料平衡",
                "去向",
                "用水",
                "废水",
            )
        ):
            return IMAGE_AGENT_KIND_WORKFLOW
        if any(
            keyword in haystack
            for keyword in (
                "table",
                "tables",
                "chart",
                "graph",
                "plot",
                "axis",
                "bar chart",
                "line chart",
                "pie chart",
                "breakdown",
                "trend",
                "co2",
                "emission",
                "usage",
                "energy",
                "数据",
                "统计",
                "趋势",
                "曲线",
                "柱状",
                "饼图",
                "坐标",
                "排放",
                "用量",
                "表 ",
                "表格",
            )
        ) or any(block.type.strip().lower() == "table" for block in page.blocks):
            return IMAGE_AGENT_KIND_TABLE
        return IMAGE_AGENT_KIND_DIAGRAM

    @staticmethod
    def _image_kind_prompt_instruction(image_kind: str) -> str:
        if image_kind == IMAGE_AGENT_KIND_MAP:
            return (
                "For maps, site plans, floor plans, and distribution layouts, read the whole graphic. Identify the subject "
                "and purpose; group visible areas by floor, zone, building, or region; explain legend colors and symbols; "
                "name marked project/site positions, boundaries, hazard/control/sensitive zones, nearby areas, orientation, "
                "and scale when visible. Explain where important areas are concentrated and what that implies for review."
            )
        if image_kind == IMAGE_AGENT_KIND_WORKFLOW:
            return (
                "For workflows, process diagrams, water balance diagrams, and material balance diagrams, read the diagram "
                "as a process walkthrough. In relationships_or_flow, list every visible main branch separately from source "
                "to final sink, including intermediate nodes, treatment steps, reuse/recirculation loops, discharge paths, "
                "losses, and entrusted disposal paths. Preserve arrow direction and important quantities/units in each "
                "branch. Do not only summarize the diagram; explain the full flow so the user can audit where each input "
                "goes."
            )
        if image_kind == IMAGE_AGENT_KIND_TABLE:
            return (
                "For tables, charts, and plots, identify the dataset, rows/categories/series, axes, units, main trend or "
                "comparison, outliers, and the most important readable values. Explain what changed, which category is "
                "largest/smallest, or what comparison matters. Do not reproduce an entire large table unless only a few "
                "cells are visible."
            )
        return (
            "For general figures, photos, scanned forms, seals, signatures, screenshots, and scientific diagrams, identify "
            "the object or evidence shown, important readable labels, annotations, marked regions, document fields, seal or "
            "signature text, spatial/technical relationships, and any limitations caused by blur, cropping, occlusion, or "
            "overlapping stamps."
        )


def _sorted_blocks_for_insertion(blocks: list[Block]) -> list[Block]:
    return sorted(
        blocks,
        key=lambda block: (
            block.order is None,
            block.order if block.order is not None else 10**9,
        ),
    )
