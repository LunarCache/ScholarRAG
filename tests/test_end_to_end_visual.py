"""Phase 5: end-to-end visual evidence chain.

Fake PDF → prepare candidate (bbox backfill + quality report) → catalog →
retrieve (mocked) → generate with visual evidence attached to the message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fitz
import pytest

from scholarrag.config import Settings
from scholarrag.domain.models import (
    BlockKind,
    BoundingBox,
    CanonicalDocument,
    ContentBlock,
    Page,
    RetrievedChunk,
    SourcePointer,
)
from scholarrag.ingestion.service import IngestionService
from scholarrag.rag.service import RAGService
from scholarrag.storage.catalog import Catalog


class FakeProvider:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] | None = None
        self.answer = "依据图 C1，公式为 E=mc²，表格准确率为 0.91 [C1]"

    async def chat(self, messages: list[dict[str, object]]) -> Any:
        self.messages = messages
        return type(
            "Resp",
            (),
            {
                "data": {
                    "choices": [{"message": {"content": self.answer}}],
                    "usage": {},
                },
                "trace_id": "trace-e2e",
            },
        )()

    async def chat_stream(self, messages: list[dict[str, object]]) -> None:
        raise AssertionError("unexpected stream")

    async def close(self) -> None:
        pass


def make_pdf(source: Path) -> None:
    with fitz.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        page.draw_rect(fitz.Rect(150, 300, 450, 480), color=(0.1, 0.2, 0.8), width=3)
        page.insert_text((170, 340), "E = mc^2", fontsize=18)
        page.insert_text((72, 72), "Intro paragraph about training.", fontsize=11)
        pdf.save(source)


def make_document() -> CanonicalDocument:
    parse_id = "parse-e2e"
    blocks: list[ContentBlock] = [
        ContentBlock(
            id=f"{parse_id}-p",
            kind=BlockKind.PARAGRAPH,
            text="The training details are described below. Accuracy reached 0.91.",
            reading_order=0,
            section_path=["Intro"],
            source=SourcePointer(
                document_id="doc-e2e",
                parse_id=parse_id,
                provider="mineru",
                page_number=1,
                raw_artifact="/immutable/raw.json",
            ),
        ),
        ContentBlock(
            id=f"{parse_id}-fig",
            kind=BlockKind.FIGURE,
            text="Figure 1: training curve",
            reading_order=1,
            section_path=["Results"],
            source=SourcePointer(
                document_id="doc-e2e",
                parse_id=parse_id,
                provider="mineru",
                page_number=1,
                bbox=BoundingBox(
                    x0=240, y0=375, x1=745, y1=610, coordinate_space="normalized_0_1000"
                ),
                raw_artifact="/immutable/raw.json",
            ),
        ),
    ]
    return CanonicalDocument(
        document_id="doc-e2e",
        parse_id=parse_id,
        source_sha256="e" * 64,
        source_filename="e2e.pdf",
        provider="mineru",
        provider_model="vlm",
        pages=[Page(page_number=1, width=612, height=792, block_ids=[b.id for b in blocks])],
        blocks=blocks,
    )


@pytest.mark.asyncio
async def test_end_to_end_visual_evidence_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        siliconflow_api_key="test-key",
    )
    source = tmp_path / "e2e.pdf"
    make_pdf(source)

    # 1. Prepare candidate: chunking + bbox backfill + quality report
    service = IngestionService(settings)
    parse_dir = tmp_path / "parse"
    candidate = service._prepare_candidate(
        make_document(), parse_dir, source, manifest={"status": "parsing"}
    )
    assert len(candidate.chunks[0].visual_evidence) == 1
    evidence = candidate.chunks[0].visual_evidence[0]
    assert evidence.image_path.endswith("fig.png")
    assert Path(evidence.image_path).is_file()
    assert (parse_dir / "quality" / "report.json").is_file()

    # 2. Register in the catalog so the generation path can load the report
    catalog = Catalog(settings)
    catalog.register(
        candidate.document,
        candidate.chunks,
        source_path=source,
        artifact_dir=parse_dir,
    )

    # 3. Retrieve (mocked) returns the chunk with its visual evidence
    chunk = candidate.chunks[0]
    retrieved = [RetrievedChunk(chunk=chunk, score=0.95)]

    rag = RAGService(settings)

    async def fake_retrieve(question, document_ids=None, top_k=8, events=None):
        return retrieved, {
            "source": "fake",
            "retrieved_chunk_ids": [chunk.id],
        }

    rag.retrieval.retrieve = fake_retrieve  # type: ignore[method-assign]
    fake = FakeProvider()
    monkeypatch.setattr("scholarrag.rag.service.SiliconFlowClient", lambda s: fake)

    # 4. Answer: generation message must carry the rendered image
    answer = await rag.answer("图 1 的公式是什么？", top_k=1)

    assert "0.91" in answer.answer
    assert fake.messages is not None
    user = fake.messages[1]
    assert user["role"] == "user"
    assert isinstance(user["content"], list)
    images = [part for part in user["content"] if part.get("type") == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_end_to_end_quality_force_applies_with_disabled_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Low-quality report forces images even with the cost switch off."""
    settings = Settings(
        data_dir=tmp_path / "data",
        siliconflow_api_key="test-key",
        chat_include_visual_evidence=False,
    )
    source = tmp_path / "e2e.pdf"
    make_pdf(source)

    service = IngestionService(settings)
    parse_dir = tmp_path / "parse"
    candidate = service._prepare_candidate(
        make_document(), parse_dir, source, manifest={"status": "parsing"}
    )

    # Downgrade the report to force visuals: table representation failed
    report_path = parse_dir / "quality" / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for signal in report["signals"]:
        if signal["code"] == "table_representation":
            signal["passed"] = False
            signal["score"] = 0.0
    report_path.write_text(json.dumps(report), encoding="utf-8")

    Catalog(settings).register(
        candidate.document,
        candidate.chunks,
        source_path=source,
        artifact_dir=parse_dir,
    )

    rag = RAGService(settings)
    chunk = candidate.chunks[0]

    async def fake_retrieve(question, document_ids=None, top_k=8, events=None):
        return [RetrievedChunk(chunk=chunk, score=0.95)], {"source": "fake"}

    rag.retrieval.retrieve = fake_retrieve  # type: ignore[method-assign]
    fake = FakeProvider()
    monkeypatch.setattr("scholarrag.rag.service.SiliconFlowClient", lambda s: fake)

    await rag.answer("图 1 的公式是什么？", top_k=1)

    user = fake.messages[1]
    assert isinstance(user["content"], list)
    assert any(part.get("type") == "image_url" for part in user["content"])
