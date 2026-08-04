from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class BlockKind(StrEnum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    FORMULA = "formula"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"
    CODE = "code"
    REFERENCE = "reference"
    FOOTNOTE = "footnote"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    UNKNOWN = "unknown"


class BoundingBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    coordinate_space: Literal["pixels", "normalized_0_1000", "pdf_points"] = "pixels"


class SourcePointer(BaseModel):
    document_id: str
    parse_id: str
    provider: str
    provider_block_id: str | None = None
    page_number: int = Field(ge=1)
    bbox: BoundingBox | None = None
    raw_artifact: str
    raw_json_pointer: str | None = None


class ContentBlock(BaseModel):
    id: str
    kind: BlockKind
    text: str = ""
    markdown: str | None = None
    html: str | None = None
    latex: str | None = None
    image_path: str | None = None
    reading_order: int
    section_path: list[str] = Field(default_factory=list)
    source: SourcePointer
    metadata: dict[str, Any] = Field(default_factory=dict)


class Page(BaseModel):
    page_number: int = Field(ge=1)
    width: float | None = None
    height: float | None = None
    image_path: str | None = None
    block_ids: list[str] = Field(default_factory=list)


class CanonicalDocument(BaseModel):
    schema_version: str = "1.0"
    document_id: str
    parse_id: str
    source_sha256: str
    source_filename: str
    provider: str
    provider_model: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    pages: list[Page] = Field(default_factory=list)
    blocks: list[ContentBlock] = Field(default_factory=list)
    raw_artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VisualEvidence(BaseModel):
    """A visual asset attached to a chunk, read directly by the vision LLM.

    OCR text describes and locates the content; the image carries the
    information OCR may have lost (formulas, tables, figures).
    """

    kind: BlockKind
    image_path: str
    page_number: int = Field(ge=1)
    bbox: BoundingBox | None = None
    caption: str = ""


class Chunk(BaseModel):
    id: str
    document_id: str
    parse_id: str
    text: str
    contextual_text: str
    block_ids: list[str]
    page_numbers: list[int]
    section_path: list[str] = Field(default_factory=list)
    token_estimate: int
    sources: list[SourcePointer]
    level: Literal["atomic", "parent", "child"] = "atomic"
    parent_chunk_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    visual_evidence: list[VisualEvidence] = Field(default_factory=list)


class Citation(BaseModel):
    citation_id: str
    document_id: str
    chunk_id: str
    page_number: int
    block_ids: list[str]
    bbox: BoundingBox | None = None
    quote: str
    score: float | None = None


class EvidenceSpan(BaseModel):
    citation_id: str
    chunk_id: str
    block_id: str | None = None
    page_number: int
    bbox: BoundingBox | None = None
    quote: str
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)


class ClaimCitation(BaseModel):
    claim_id: str
    text: str
    answer_start_char: int = Field(ge=0)
    answer_end_char: int = Field(ge=0)
    citation_ids: list[str] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    supported: bool = False


class RetrievedChunk(BaseModel):
    chunk: Chunk
    score: float
    dense_score: float | None = None
    lexical_score: float | None = None
    rerank_score: float | None = None


class Answer(BaseModel):
    answer: str
    citations: list[Citation]
    trace_id: str
    retrieval_debug: dict[str, Any] = Field(default_factory=dict)
    claims: list[ClaimCitation] = Field(default_factory=list)
