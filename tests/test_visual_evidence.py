"""Phase 1 tests: visual evidence closed loop.

Covers the chunker attaching block images to chunks, the image-to-data-url
tool, multimodal message construction, and both generation paths (plain
RAG service and agentic synthesize) sending images to the provider.
"""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from scholarrag.config import Settings
from scholarrag.domain.models import (
    BlockKind,
    BoundingBox,
    CanonicalDocument,
    Chunk,
    ContentBlock,
    Page,
    RetrievedChunk,
    SourcePointer,
    VisualEvidence,
)
from scholarrag.ingestion.chunker import SemanticChunker
from scholarrag.providers.siliconflow import ProviderResponse
from scholarrag.rag.agent import AgenticRAGService, AgentPlan
from scholarrag.rag.service import RAGService
from scholarrag.rag.visuals import (
    build_multimodal_content,
    collect_visual_evidence,
    image_to_data_url,
)


def make_png(path: Path, *, width: int = 300, height: int = 200) -> Path:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pix.save(str(path))
    return path


def make_document(tmp_path: Path, *, parse_id: str = "parse_visual") -> CanonicalDocument:
    figure_png = make_png(tmp_path / "figure.png")
    table_png = make_png(tmp_path / "table.png")
    blocks = [
        ContentBlock(
            id=f"{parse_id}-fig",
            kind=BlockKind.FIGURE,
            text="Figure 1: architecture diagram",
            image_path=str(figure_png),
            reading_order=0,
            section_path=["Method"],
            source=SourcePointer(
                document_id="doc_visual",
                parse_id=parse_id,
                provider="test",
                page_number=1,
                bbox=BoundingBox(x0=10, y0=10, x1=200, y1=150),
                raw_artifact="/immutable/raw.json",
            ),
        ),
        ContentBlock(
            id=f"{parse_id}-tab",
            kind=BlockKind.TABLE,
            html="<table><tr><td>acc</td><td>0.91</td></tr></table>",
            image_path=str(table_png),
            reading_order=1,
            section_path=["Results"],
            source=SourcePointer(
                document_id="doc_visual",
                parse_id=parse_id,
                provider="test",
                page_number=2,
                bbox=BoundingBox(x0=10, y0=10, x1=200, y1=120),
                raw_artifact="/immutable/raw.json",
            ),
        ),
        ContentBlock(
            id=f"{parse_id}-txt",
            kind=BlockKind.PARAGRAPH,
            text="A plain paragraph without any image.",
            reading_order=2,
            section_path=["Intro"],
            source=SourcePointer(
                document_id="doc_visual",
                parse_id=parse_id,
                provider="test",
                page_number=1,
                raw_artifact="/immutable/raw.json",
            ),
        ),
    ]
    return CanonicalDocument(
        document_id="doc_visual",
        parse_id=parse_id,
        source_sha256="a" * 64,
        source_filename="visual.pdf",
        provider="test",
        provider_model="test-v1",
        pages=[
            Page(page_number=1, block_ids=[f"{parse_id}-fig", f"{parse_id}-txt"]),
            Page(page_number=2, block_ids=[f"{parse_id}-tab"]),
        ],
        blocks=blocks,
    )


def make_retrieved_chunk(
    *,
    chunk_id: str = "chk_1",
    text: str = "chunk text",
    evidence: list[VisualEvidence] | None = None,
) -> RetrievedChunk:
    chunk = Chunk(
        id=chunk_id,
        document_id="doc_visual",
        parse_id="parse_visual",
        text=text,
        contextual_text=text,
        block_ids=[f"block-{chunk_id}"],
        page_numbers=[1],
        section_path=["Results"],
        token_estimate=10,
        sources=[
            SourcePointer(
                document_id="doc_visual",
                parse_id="parse_visual",
                provider="test",
                page_number=1,
                raw_artifact="/immutable/raw.json",
            )
        ],
        visual_evidence=evidence or [],
    )
    return RetrievedChunk(chunk=chunk, score=0.9)


def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


def test_chunker_attaches_visual_evidence_for_figure_and_table_blocks(
    tmp_path: Path,
) -> None:
    document = make_document(tmp_path)
    chunks = SemanticChunker(max_chars=2000).chunk(document)

    evidence = [item for chunk in chunks for item in chunk.visual_evidence]
    assert len(evidence) == 2
    by_kind = {item.kind: item for item in evidence}
    assert BlockKind.FIGURE in by_kind
    assert BlockKind.TABLE in by_kind
    assert by_kind[BlockKind.FIGURE].image_path.endswith("figure.png")
    assert by_kind[BlockKind.FIGURE].caption == "Figure 1: architecture diagram"
    assert by_kind[BlockKind.FIGURE].page_number == 1
    assert by_kind[BlockKind.TABLE].page_number == 2


def test_chunker_skips_blocks_without_image_path(tmp_path: Path) -> None:
    document = make_document(tmp_path)
    paragraph = document.blocks[-1]
    assert paragraph.kind == BlockKind.PARAGRAPH
    chunks = SemanticChunker(max_chars=2000).chunk(document)
    for chunk in chunks:
        for item in chunk.visual_evidence:
            assert item.kind in {BlockKind.FIGURE, BlockKind.TABLE}


# ---------------------------------------------------------------------------
# Image tool
# ---------------------------------------------------------------------------


def test_image_to_data_url_resizes_and_returns_jpeg_data_url(tmp_path: Path) -> None:
    large = make_png(tmp_path / "large.png", width=2400, height=1800)
    url = image_to_data_url(large, max_edge=1024)
    assert url is not None
    assert url.startswith("data:image/jpeg;base64,")
    # Decoded payload must be valid JPEG data.
    import base64

    payload = base64.b64decode(url.split(",", 1)[1])
    assert payload[:2] == b"\xff\xd8"


def test_image_to_data_url_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert image_to_data_url(tmp_path / "missing.png") is None


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------


def test_collect_visual_evidence_takes_one_per_chunk_in_order(tmp_path: Path) -> None:
    png_a = make_png(tmp_path / "a.png")
    png_c = make_png(tmp_path / "c.png")
    first = make_retrieved_chunk(
        chunk_id="a",
        evidence=[VisualEvidence(kind=BlockKind.FIGURE, image_path=str(png_a), page_number=1)],
    )
    second = make_retrieved_chunk(chunk_id="b", text="no image")
    third = make_retrieved_chunk(
        chunk_id="c",
        evidence=[
            VisualEvidence(kind=BlockKind.FIGURE, image_path=str(png_c), page_number=3),
            VisualEvidence(kind=BlockKind.TABLE, image_path=str(png_c), page_number=4),
        ],
    )
    collected = collect_visual_evidence([first, second, third])
    assert [(index, item.kind) for index, item in collected] == [
        (1, BlockKind.FIGURE),
        (3, BlockKind.FIGURE),
    ]


def test_collect_visual_evidence_skips_duplicate_images(tmp_path: Path) -> None:
    """Parent and child chunks sharing one figure image are attached once."""
    shared = make_png(tmp_path / "shared.png")
    other = make_png(tmp_path / "other.png")
    parent = make_retrieved_chunk(
        chunk_id="parent",
        evidence=[VisualEvidence(kind=BlockKind.FIGURE, image_path=str(shared), page_number=2)],
    )
    child = make_retrieved_chunk(
        chunk_id="child",
        evidence=[VisualEvidence(kind=BlockKind.FIGURE, image_path=str(shared), page_number=2)],
    )
    third = make_retrieved_chunk(
        chunk_id="third",
        evidence=[VisualEvidence(kind=BlockKind.TABLE, image_path=str(other), page_number=3)],
    )
    collected = collect_visual_evidence([parent, child, third])
    assert [(index, Path(item.image_path).name) for index, item in collected] == [
        (1, "shared.png"),
        (3, "other.png"),
    ]


def test_collect_visual_evidence_has_no_budget(tmp_path: Path) -> None:
    """Every distinct image among retrieved chunks is collected, uncapped."""
    chunks = [
        make_retrieved_chunk(
            chunk_id=f"chk_{i}",
            evidence=[
                VisualEvidence(
                    kind=BlockKind.FIGURE,
                    image_path=str(make_png(tmp_path / f"fig_{i}.png")),
                    page_number=1,
                )
            ],
        )
        for i in range(10)
    ]
    assert len(collect_visual_evidence(chunks)) == 10


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def test_build_multimodal_content_attaches_images_with_labels(tmp_path: Path) -> None:
    png = make_png(tmp_path / "fig.png")
    retrieved = [
        make_retrieved_chunk(
            text="arch diagram",
            evidence=[VisualEvidence(kind=BlockKind.FIGURE, image_path=str(png), page_number=1)],
        )
    ]
    content = build_multimodal_content("问题：x\n\n证据：\n[C1]", retrieved, settings(tmp_path))
    assert isinstance(content.parts, list)
    types = [part["type"] for part in content.parts]
    assert types == ["text", "text", "image_url"]
    assert content.parts[1]["text"] == "[C1 的图]"
    assert content.parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content.images_included == 1
    assert content.images_skipped == 0


def test_build_multimodal_content_tracks_missing_images_as_skipped(tmp_path: Path) -> None:
    missing = make_retrieved_chunk(
        evidence=[
            VisualEvidence(
                kind=BlockKind.TABLE, image_path=str(tmp_path / "nope.png"), page_number=2
            )
        ]
    )
    content = build_multimodal_content("问题：x\n\n证据：\n[C1]", [missing], settings(tmp_path))
    assert content.images_included == 0
    assert content.images_skipped == 1
    assert isinstance(content.parts, str)


def test_build_multimodal_content_returns_plain_text_when_disabled(tmp_path: Path) -> None:
    png = make_png(tmp_path / "fig.png")
    retrieved = [
        make_retrieved_chunk(
            evidence=[VisualEvidence(kind=BlockKind.FIGURE, image_path=str(png), page_number=1)]
        )
    ]
    cfg = settings(tmp_path)
    cfg.chat_include_visual_evidence = False
    content = build_multimodal_content("问题：x\n\n证据：\n[C1]", retrieved, cfg)
    assert content.parts == "问题：x\n\n证据：\n[C1]"
    assert content.images_included == 0


# ---------------------------------------------------------------------------
# Generation paths (RAG service + agent synthesize)
# ---------------------------------------------------------------------------


class FakeProvider:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] | None = None
        self.answer = "答案是 0.91 [C1]"

    async def chat(self, messages: list[dict[str, object]]) -> ProviderResponse:
        self.messages = messages
        return ProviderResponse(
            data={"choices": [{"message": {"content": self.answer}}], "usage": {}},
            trace_id="trace-fake",
        )

    async def chat_stream(self, messages: list[dict[str, object]]) -> None:
        raise AssertionError("chat_stream should not be used without events")

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_rag_service_sends_images_in_generation_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = make_png(tmp_path / "fig.png")
    retrieved = [
        make_retrieved_chunk(
            text="Figure 1: architecture diagram",
            evidence=[VisualEvidence(kind=BlockKind.FIGURE, image_path=str(png), page_number=1)],
        )
    ]
    cfg = settings(tmp_path)
    cfg.siliconflow_api_key = "test-key"
    service = RAGService(cfg)

    async def fake_retrieve(question, document_ids=None, top_k=8, events=None):
        return retrieved, {"source": "fake"}

    service.retrieval.retrieve = fake_retrieve  # type: ignore[method-assign]
    fake = FakeProvider()
    monkeypatch.setattr("scholarrag.rag.service.SiliconFlowClient", lambda s: fake)

    answer = await service.answer("该图展示了什么？", top_k=1)

    assert "0.91" in answer.answer
    assert fake.messages is not None
    user = fake.messages[1]
    assert user["role"] == "user"
    assert isinstance(user["content"], list)
    assert any(part.get("type") == "image_url" for part in user["content"])


@pytest.mark.asyncio
async def test_rag_service_plain_text_when_visual_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = make_png(tmp_path / "fig.png")
    retrieved = [
        make_retrieved_chunk(
            evidence=[VisualEvidence(kind=BlockKind.FIGURE, image_path=str(png), page_number=1)]
        )
    ]
    cfg = settings(tmp_path)
    cfg.siliconflow_api_key = "test-key"
    cfg.chat_include_visual_evidence = False
    service = RAGService(cfg)

    async def fake_retrieve(question, document_ids=None, top_k=8, events=None):
        return retrieved, {"source": "fake"}

    service.retrieval.retrieve = fake_retrieve  # type: ignore[method-assign]
    fake = FakeProvider()
    monkeypatch.setattr("scholarrag.rag.service.SiliconFlowClient", lambda s: fake)

    await service.answer("该图展示了什么？", top_k=1)

    user = fake.messages[1]
    assert isinstance(user["content"], str)


@pytest.mark.asyncio
async def test_agent_synthesize_sends_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = make_png(tmp_path / "tab.png")
    retrieved = [
        make_retrieved_chunk(
            chunk_id="chk_agent",
            text="Table 2: results",
            evidence=[VisualEvidence(kind=BlockKind.TABLE, image_path=str(png), page_number=2)],
        )
    ]
    cfg = settings(tmp_path)
    cfg.siliconflow_api_key = "test-key"
    agent = AgenticRAGService(cfg)
    fake = FakeProvider()
    fake.answer = "表里是 0.95 [C1]"
    monkeypatch.setattr("scholarrag.rag.agent.SiliconFlowClient", lambda s: fake)

    plan = AgentPlan(
        mode="controlled_rag",
        intent="fact",
        difficulty="simple",
        subqueries=["问题：x"],
        tools=["dense", "visual"],
        max_steps=1,
        reason="test",
    )
    answer = await agent._synthesize(
        "表里结果是多少？",
        retrieved,
        plan=plan,
        sufficient=True,
        step_debug=[],
        events=None,
        trace_id="trace-1",
    )

    assert "0.95" in answer.answer
    assert fake.messages is not None
    user = fake.messages[1]
    assert isinstance(user["content"], list)
    image_urls = [
        part["image_url"]["url"] for part in user["content"] if part.get("type") == "image_url"
    ]
    assert len(image_urls) == 1
    assert image_urls[0].startswith("data:image/jpeg;base64,")
