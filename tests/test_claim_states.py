from __future__ import annotations

import json
from pathlib import Path

import pytest

from scholarrag.domain.models import (
    BlockKind,
    CanonicalDocument,
    ClaimCitation,
    ContentBlock,
    RetrievedChunk,
    SourcePointer,
)
from scholarrag.ingestion.chunker import SemanticChunker
from scholarrag.rag.service import RAGService

CASES = json.loads((Path(__file__).parent / "fixtures/claim_cases.json").read_text())["cases"]


def document(texts):
    return CanonicalDocument(
        document_id="doc",
        parse_id="parse",
        source_sha256="a" * 64,
        source_filename="synthetic.pdf",
        provider="test",
        provider_model="test",
        blocks=[
            ContentBlock(
                id=f"block-{index}",
                kind=BlockKind.PARAGRAPH,
                text=text,
                reading_order=index,
                source=SourcePointer(
                    document_id="doc",
                    parse_id="parse",
                    provider="test",
                    page_number=index + 1,
                    raw_artifact="synthetic.json",
                ),
            )
            for index, text in enumerate(texts)
        ],
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_reference_presence_never_implies_semantic_support(case):
    chunk = SemanticChunker().chunk(document([case["evidence"]]))[0]
    claims = RAGService._claims(case["answer"], [RetrievedChunk(chunk=chunk, score=1)])
    assert len(claims) == 1
    assert claims[0].evidence_located is case["located"]
    assert claims[0].supported is False
    assert claims[0].support_status == "not_checked"


def test_new_offsets_survive_internal_paragraph_breaks_and_old_data_is_approximate():
    chunks = SemanticChunker().chunk(
        document(
            [
                "Opening paragraph.\n\nAnother paragraph in the same block.",
                "The target result is 42 percent.",
            ]
        )
    )
    chunk = chunks[0]
    claim = RAGService._claims(
        "The target result is 42 percent [C1].", [RetrievedChunk(chunk=chunk, score=1)]
    )[0]
    span = claim.evidence_spans[0]
    assert span.block_id == "block-1" and span.page_number == 2
    assert span.location_status == "exact" and span.parse_id == "parse"
    assert chunk.text[span.start_char : span.end_char] == span.quote
    chunk.metadata.pop("block_offsets")
    legacy = RAGService._claims(
        "The target result is 42 percent [C1].", [RetrievedChunk(chunk=chunk, score=1)]
    )[0].evidence_spans[0]
    assert legacy.location_status == "approximate"
    assert legacy.block_id is None and legacy.bbox is None


def test_legacy_boolean_is_not_promoted_to_verified_support():
    chunk = SemanticChunker().chunk(document(["An unrelated fact."]))[0]
    claim = RAGService._claims(
        "A different conclusion [C1].", [RetrievedChunk(chunk=chunk, score=1)]
    )[0]
    old = claim.model_dump(exclude={"support_status", "evidence_located"})
    old["supported"] = True
    restored = ClaimCitation.model_validate(old)
    assert restored.supported is False and restored.evidence_located is True
    assert restored.support_status == "not_checked"


def test_verified_status_requires_evidence():
    claim = ClaimCitation(
        claim_id="CL1",
        text="A conclusion",
        answer_start_char=0,
        answer_end_char=12,
        support_status="supported",
        supported=True,
    )
    assert claim.support_status == "insufficient" and claim.supported is False
