from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Block(BaseModel):
    id: str
    type: str
    text: str = ""
    bbox: list[float] | None = None
    order: int | None = None
    confidence: float | None = None
    semantic_type: str | None = None
    heading_level: int | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    page_index: int


class Page(BaseModel):
    page_index: int
    width: int | None = None
    height: int | None = None
    blocks: list[Block] = Field(default_factory=list)


class DocumentIR(BaseModel):
    doc_id: str
    source_file: str
    source_engine: str
    generated_at: str
    pages: list[Page] = Field(default_factory=list)


class ValidationReport(BaseModel):
    empty_page_rate: float
    order_anomaly_rate: float
    table_anomaly_rate: float
    coverage_rate: float
    non_blank_pages: int
    pages_with_content: int
    empty_pages: int
    anomalous_order_pages: int
    total_tables: int
    anomalous_tables: int
    failed_pages: list[int] = Field(default_factory=list)
    pass_quality_floor: bool
