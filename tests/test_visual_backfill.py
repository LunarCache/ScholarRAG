"""Phase 2: visual evidence backfill — render figure/table regions from the PDF."""

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
    SourcePointer,
)
from scholarrag.ingestion.service import IngestionService
from scholarrag.ingestion.visual_backfill import VisualEvidenceBackfill


def make_pdf_with_figure(source: Path) -> None:
    """One-page PDF with a rectangle and text inside the figure region."""
    with fitz.open() as pdf:
        page = pdf.new_page(width=612, height=792)  # US Letter in PDF points
        page.draw_rect(fitz.Rect(150, 300, 450, 480), color=(0.1, 0.2, 0.8), width=3)
        page.insert_text((170, 340), "Figure content 42", fontsize=18)
        pdf.save(source)


def make_document(
    *,
    figure_bbox: BoundingBox | None,
    table_image: str | None = None,
    extra_blocks: list[ContentBlock] | None = None,
) -> CanonicalDocument:
    parse_id = "parse-visual"
    blocks: list[ContentBlock] = [
        ContentBlock(
            id=f"{parse_id}-fig",
            kind=BlockKind.FIGURE,
            text="Figure 1: a figure without an extracted image",
            reading_order=0,
            section_path=["Results"],
            source=SourcePointer(
                document_id="doc-visual",
                parse_id=parse_id,
                provider="test",
                page_number=1,
                bbox=figure_bbox,
                raw_artifact="/immutable/raw.json",
            ),
        ),
        ContentBlock(
            id=f"{parse_id}-tab",
            kind=BlockKind.TABLE,
            html="<table><tr><td>acc</td><td>0.91</td></tr></table>",
            image_path=table_image,
            reading_order=1,
            section_path=["Results"],
            source=SourcePointer(
                document_id="doc-visual",
                parse_id=parse_id,
                provider="test",
                page_number=1,
                bbox=BoundingBox(
                    x0=100, y0=500, x1=500, y1=600, coordinate_space="normalized_0_1000"
                ),
                raw_artifact="/immutable/raw.json",
            ),
        ),
    ]
    blocks.extend(extra_blocks or [])
    return CanonicalDocument(
        document_id="doc-visual",
        parse_id=parse_id,
        source_sha256="a" * 64,
        source_filename="visual.pdf",
        provider="test",
        provider_model="test-vlm",
        pages=[Page(page_number=1, width=612, height=792)],
        blocks=blocks,
    )


def test_backfill_renders_figure_without_image_path(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    make_pdf_with_figure(source)
    document = make_document(
        figure_bbox=BoundingBox(
            x0=240, y0=375, x1=745, y1=610, coordinate_space="normalized_0_1000"
        )
    )
    chunks = [
        Chunk(
            id="chunk_a",
            document_id=document.document_id,
            parse_id=document.parse_id,
            text="figure",
            contextual_text="figure",
            block_ids=[block.id for block in document.blocks],
            page_numbers=[1],
            section_path=["Results"],
            token_estimate=10,
            sources=[block.source for block in document.blocks],
        )
    ]
    out = tmp_path / "visual"

    added = VisualEvidenceBackfill().fill(chunks, document, source, out)

    assert added == 2  # figure and table both lack parser images
    fig_evidence = [
        e
        for e in chunks[0].visual_evidence
        if e.image_path.endswith(f"{document.blocks[0].id}.png")
    ]
    assert len(fig_evidence) == 1
    evidence = fig_evidence[0]
    assert evidence.kind == BlockKind.FIGURE
    assert evidence.page_number == 1
    assert "Figure 1" in evidence.caption
    rendered = Path(evidence.image_path)
    assert rendered.is_file()
    with fitz.open(rendered) as img:
        pix = img.load_page(0).get_pixmap()
        assert pix.width >= 100 and pix.height >= 100


def test_backfill_keeps_parser_provided_images_untouched(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    make_pdf_with_figure(source)
    existing = tmp_path / "table.png"
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40), False)
    pix.save(existing)
    document = make_document(
        figure_bbox=None, table_image=str(existing)
    )  # figure without bbox, table with image
    chunks = [
        Chunk(
            id="chunk_b",
            document_id=document.document_id,
            parse_id=document.parse_id,
            text="figure",
            contextual_text="figure",
            block_ids=[block.id for block in document.blocks],
            page_numbers=[1],
            section_path=["Results"],
            token_estimate=10,
            sources=[block.source for block in document.blocks],
        )
    ]
    out = tmp_path / "visual"

    added = VisualEvidenceBackfill().fill(chunks, document, source, out)

    assert added == 0
    assert chunks[0].visual_evidence == []


def test_backfill_skips_missing_bbox_and_missing_pdf_page(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    make_pdf_with_figure(source)
    document = make_document(
        figure_bbox=BoundingBox(
            x0=100, y0=100, x1=500, y1=500, coordinate_space="normalized_0_1000"
        ),
        extra_blocks=[
            ContentBlock(
                id="parse-visual-page9",
                kind=BlockKind.FIGURE,
                text="on a page beyond the pdf",
                reading_order=2,
                section_path=[],
                source=SourcePointer(
                    document_id="doc-visual",
                    parse_id="parse-visual",
                    provider="test",
                    page_number=9,
                    bbox=BoundingBox(
                        x0=100, y0=100, x1=500, y1=500, coordinate_space="normalized_0_1000"
                    ),
                    raw_artifact="/immutable/raw.json",
                ),
            )
        ],
    )
    chunks = [
        Chunk(
            id="chunk_c",
            document_id=document.document_id,
            parse_id=document.parse_id,
            text="figure",
            contextual_text="figure",
            block_ids=[block.id for block in document.blocks],
            page_numbers=[1, 9],
            section_path=[],
            token_estimate=10,
            sources=[block.source for block in document.blocks],
        )
    ]

    added = VisualEvidenceBackfill().fill(chunks, document, source, tmp_path / "visual")

    assert added == 2  # page-1 figure + table rendered; page-9 skipped
    assert all(e.page_number == 1 for e in chunks[0].visual_evidence)
    assert not any("page9" in e.image_path for e in chunks[0].visual_evidence)


def test_backfill_empty_chunks_returns_zero(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    make_pdf_with_figure(source)
    document = make_document(
        figure_bbox=BoundingBox(
            x0=100, y0=100, x1=500, y1=500, coordinate_space="normalized_0_1000"
        )
    )

    added = VisualEvidenceBackfill().fill([], document, source, tmp_path / "visual")

    assert added == 0


def test_prepare_candidate_runs_backfill_when_enabled(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    make_pdf_with_figure(source)
    document = make_document(
        figure_bbox=BoundingBox(
            x0=240, y0=375, x1=745, y1=610, coordinate_space="normalized_0_1000"
        )
    )
    service = IngestionService(Settings(data_dir=tmp_path / "data"))
    parse_dir = tmp_path / "parse"
    manifest: dict[str, object] = {"status": "parsing"}

    candidate = service._prepare_candidate(document, parse_dir, source, manifest=manifest)

    assert len(candidate.chunks[0].visual_evidence) == 2
    evidence = candidate.chunks[0].visual_evidence[0]
    assert Path(evidence.image_path).is_file()
    assert (parse_dir / "visual").is_dir()
    # persisted chunks carry the backfilled evidence
    stored = candidate.chunks[0].model_dump(mode="json")
    assert stored["visual_evidence"][0]["image_path"] == evidence.image_path


def test_prepare_candidate_skips_backfill_when_disabled(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    make_pdf_with_figure(source)
    document = make_document(
        figure_bbox=BoundingBox(
            x0=240, y0=375, x1=745, y1=610, coordinate_space="normalized_0_1000"
        )
    )
    service = IngestionService(
        Settings(
            data_dir=tmp_path / "data",
            ingest_visual_backfill=False,
        )
    )

    candidate = service._prepare_candidate(
        document, tmp_path / "parse", source, manifest={"status": "parsing"}
    )

    assert candidate.chunks[0].visual_evidence == []


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        (
            BoundingBox(x0=0, y0=0, x1=1000, y1=1000, coordinate_space="normalized_0_1000"),
            None,
        ),  # full page
        (
            BoundingBox(x0=-10, y0=-10, x1=1010, y1=1010, coordinate_space="normalized_0_1000"),
            None,
        ),  # clamped to page
    ],
)
def test_backfill_clamps_bbox_to_page(tmp_path: Path, bbox: BoundingBox, expected) -> None:
    source = tmp_path / "paper.pdf"
    make_pdf_with_figure(source)
    document = make_document(figure_bbox=bbox)
    chunks = [
        Chunk(
            id="chunk_d",
            document_id=document.document_id,
            parse_id=document.parse_id,
            text="figure",
            contextual_text="figure",
            block_ids=[document.blocks[0].id],
            page_numbers=[1],
            section_path=["Results"],
            token_estimate=10,
            sources=[document.blocks[0].source],
        )
    ]

    added = VisualEvidenceBackfill().fill(chunks, document, source, tmp_path / "visual")

    assert added == 1
    assert expected is None  # clamp must not fail rendering
