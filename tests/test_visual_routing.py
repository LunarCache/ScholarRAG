"""Phase 3: quality-driven visual evidence routing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scholarrag.config import Settings
from scholarrag.domain.models import (
    BlockKind,
    Chunk,
    RetrievedChunk,
    SourcePointer,
    VisualEvidence,
)
from scholarrag.ingestion.quality import ParseQualityReport, QualitySignal
from scholarrag.rag.service import RAGService
from scholarrag.rag.visual_routing import (
    load_quality_reports,
    merge_routing_decisions,
    visual_routing_policy,
)
from scholarrag.storage.catalog import Catalog

SIGNAL_CODES = [
    "page_coverage",
    "content_pages",
    "text_density",
    "bbox_coverage",
    "known_block_types",
    "raw_pointer_coverage",
    "page_block_linkage",
    "reading_order",
    "clean_text",
    "content_uniqueness",
    "heading_hierarchy",
    "formula_representation",
    "table_representation",
    "figure_caption_coverage",
]


def make_report(
    *,
    decision: str = "accept",
    failed: set[str] | None = None,
) -> ParseQualityReport:
    failed = failed or set()
    signals = [
        QualitySignal(
            code=code,
            score=0.0 if code in failed else 1.0,
            weight=0.0,
            value=None,
            threshold=None,
            passed=code not in failed,
            severity="warning" if code in failed else "info",
            message=code,
        )
        for code in SIGNAL_CODES
    ]
    return ParseQualityReport(
        document_id="doc-route",
        parse_id="parse-route",
        provider="test",
        provider_model="test-v1",
        evaluated_at=datetime.now(UTC),
        expected_page_count=2,
        parsed_page_count=2,
        score=0.9 if decision == "accept" else 0.6,
        decision=decision,  # type: ignore[arg-type]
        recommended_action="activate",  # type: ignore[arg-type]
        signals=signals,
        thresholds={"accept_score": 0.85, "review_score": 0.65},
    )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_policy_defaults_on_clean_report() -> None:
    decision = visual_routing_policy(make_report())

    assert decision.force_visual is False
    assert decision.reasons


@pytest.mark.parametrize(
    "failed_signal",
    [
        "formula_representation",
        "table_representation",
        "text_density",
        "clean_text",
        "figure_caption_coverage",
        "content_uniqueness",
    ],
)
def test_policy_forces_visual_when_text_repr_unreliable(failed_signal: str) -> None:
    decision = visual_routing_policy(make_report(failed={failed_signal}))

    assert decision.force_visual is True
    assert any(failed_signal in reason for reason in decision.reasons)


def test_policy_review_decision_is_noted_but_does_not_force() -> None:
    decision = visual_routing_policy(make_report(decision="review"))

    assert decision.force_visual is False
    assert any("review" in reason for reason in decision.reasons)


def test_merge_forces_when_any_forces() -> None:
    merged = merge_routing_decisions(
        [
            visual_routing_policy(make_report()),
            visual_routing_policy(make_report(failed={"table_representation"})),
        ]
    )

    assert merged.force_visual is True
    assert len(merged.reasons) == 2


def test_merge_empty_returns_defaults() -> None:
    merged = merge_routing_decisions([])

    assert merged.force_visual is False


# ---------------------------------------------------------------------------
# Report loading from catalog
# ---------------------------------------------------------------------------


def _register_document(tmp_path: Path, report: ParseQualityReport) -> str:
    from scholarrag.domain.models import CanonicalDocument

    settings = Settings(data_dir=tmp_path / "data", phoenix_endpoint=None)
    catalog = Catalog(settings)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_dir = artifact_dir / "quality"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps(report.model_dump(mode="json")), encoding="utf-8"
    )
    document = CanonicalDocument(
        document_id=report.document_id,
        parse_id=report.parse_id,
        source_sha256="a" * 64,
        source_filename="paper.pdf",
        provider=report.provider,
        provider_model=report.provider_model,
        pages=[],
        blocks=[],
    )
    catalog.register(
        document,
        [],
        source_path=tmp_path / "paper.pdf",
        artifact_dir=artifact_dir,
    )
    return report.document_id


def test_load_quality_reports_reads_persisted_reports(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", phoenix_endpoint=None)
    document_id = _register_document(tmp_path, make_report(failed={"text_density"}))

    reports = load_quality_reports(Catalog(settings), {document_id})

    assert len(reports) == 1
    assert reports[0].decision == "accept"
    assert not reports[0].signals[2].passed


def test_load_quality_reports_skips_missing_or_corrupt(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", phoenix_endpoint=None)
    document_id = _register_document(tmp_path, make_report())
    artifact_dir = tmp_path / "artifacts"
    (artifact_dir / "quality" / "report.json").write_text("{not json", encoding="utf-8")

    reports = load_quality_reports(Catalog(settings), {document_id, "ghost-doc"})

    assert reports == []


# ---------------------------------------------------------------------------
# End-to-end: force applies even when the cost switch is off
# ---------------------------------------------------------------------------


class FakeProvider:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] | None = None

    async def chat(self, messages: list[dict[str, object]]) -> Any:
        self.messages = messages
        return type(
            "Resp",
            (),
            {
                "data": {
                    "choices": [{"message": {"content": "依据图 C1 回答"}}],
                    "usage": {},
                },
                "trace_id": "trace-fake",
            },
        )()

    async def chat_stream(self, messages: list[dict[str, object]]) -> None:
        raise AssertionError("unexpected stream")

    async def close(self) -> None:
        pass


def make_png(path: Path) -> Path:
    import fitz

    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), False)
    pix.save(path)
    return path


def make_retrieved_chunk(evidence: list[VisualEvidence]) -> RetrievedChunk:
    chunk = Chunk(
        id="chk_route",
        document_id="doc-route",
        parse_id="parse-route",
        text="figure evidence",
        contextual_text="figure evidence",
        block_ids=["block-1"],
        page_numbers=[1],
        section_path=["Results"],
        token_estimate=10,
        sources=[
            SourcePointer(
                document_id="doc-route",
                parse_id="parse-route",
                provider="test",
                page_number=1,
                raw_artifact="/immutable/raw.json",
            )
        ],
        visual_evidence=evidence,
    )
    return RetrievedChunk(chunk=chunk, score=0.9)


@pytest.mark.asyncio
async def test_rag_service_forces_images_when_report_says_text_unreliable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = make_png(tmp_path / "fig.png")
    retrieved = [
        make_retrieved_chunk(
            [VisualEvidence(kind=BlockKind.FIGURE, image_path=str(png), page_number=1)]
        )
    ]
    cfg = Settings(
        data_dir=tmp_path / "data",
        phoenix_endpoint=None,
        siliconflow_api_key="test-key",
        chat_include_visual_evidence=False,
    )
    _register_document(tmp_path, make_report(failed={"table_representation"}))
    service = RAGService(cfg)

    async def fake_retrieve(question, document_ids=None, top_k=8, events=None):
        return retrieved, {"source": "fake"}

    service.retrieval.retrieve = fake_retrieve  # type: ignore[method-assign]
    fake = FakeProvider()
    monkeypatch.setattr("scholarrag.rag.service.SiliconFlowClient", lambda s: fake)

    await service.answer("表格数据是什么？", top_k=1)

    user = fake.messages[1]
    assert isinstance(user["content"], list)
    assert any(part.get("type") == "image_url" for part in user["content"])


@pytest.mark.asyncio
async def test_rag_service_stays_text_only_without_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = make_png(tmp_path / "fig.png")
    retrieved = [
        make_retrieved_chunk(
            [VisualEvidence(kind=BlockKind.FIGURE, image_path=str(png), page_number=1)]
        )
    ]
    cfg = Settings(
        data_dir=tmp_path / "data",
        phoenix_endpoint=None,
        siliconflow_api_key="test-key",
        chat_include_visual_evidence=False,
    )
    service = RAGService(cfg)

    async def fake_retrieve(question, document_ids=None, top_k=8, events=None):
        return retrieved, {"source": "fake"}

    service.retrieval.retrieve = fake_retrieve  # type: ignore[method-assign]
    fake = FakeProvider()
    monkeypatch.setattr("scholarrag.rag.service.SiliconFlowClient", lambda s: fake)

    await service.answer("表格数据是什么？", top_k=1)

    user = fake.messages[1]
    assert isinstance(user["content"], str)
