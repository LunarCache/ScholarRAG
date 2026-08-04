"""Backfill visual evidence for figure/table blocks that lack extracted images.

Parsers (MinerU/PaddleOCR) sometimes fail to export the image file for a
figure/table block even though the block bounding box is correct. Instead of
improving the parser, we render the region straight from the source PDF with
PyMuPDF and attach it as visual evidence, so the vision LLM can read the
original figure/table directly.
"""

from __future__ import annotations

import logging
from pathlib import Path

import fitz

from scholarrag.domain.models import (
    BlockKind,
    BoundingBox,
    CanonicalDocument,
    Chunk,
    ContentBlock,
    VisualEvidence,
)

logger = logging.getLogger(__name__)

# Matches the @2x page images used by the rest of the application.
_RENDER_ZOOM = 2.0
# Small relative margin so the crop does not clip content at the edges.
_BBOX_PADDING_RATIO = 0.02
_CAPTION_MAX_CHARS = 500


class VisualEvidenceBackfill:
    """Render figure/table regions from the source PDF for blocks lacking images."""

    def __init__(self, *, zoom: float = _RENDER_ZOOM) -> None:
        self._zoom = zoom

    def fill(
        self,
        chunks: list[Chunk],
        document: CanonicalDocument,
        source_pdf: str | Path,
        output_dir: str | Path,
    ) -> int:
        """Attach cropped visual evidence to chunks.

        Only FIGURE/TABLE blocks without an ``image_path`` but with a usable
        bounding box are rendered. Returns the number of evidence items added.
        """
        if not chunks:
            return 0
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        blocks_by_id = {block.id: block for block in document.blocks}
        added = 0
        with fitz.open(Path(source_pdf)) as pdf:
            for chunk in chunks:
                for block_id in chunk.block_ids:
                    block = blocks_by_id.get(block_id)
                    if block is None or block.kind not in {BlockKind.FIGURE, BlockKind.TABLE}:
                        continue
                    if block.image_path:
                        continue  # parser already provided an image
                    evidence = self._render_evidence(block, pdf, output)
                    if evidence is None:
                        continue
                    chunk.visual_evidence.append(evidence)
                    added += 1
        if added:
            logger.info("Visual backfill: rendered %d evidence image(s)", added)
        return added

    def _render_evidence(
        self, block: ContentBlock, pdf: fitz.Document, output_dir: Path
    ) -> VisualEvidence | None:
        page_number = block.source.page_number
        if block.source.bbox is None or page_number > pdf.page_count:
            return None
        rect = self._to_pdf_rect(block.source.bbox, pdf.load_page(page_number - 1).rect)
        if rect is None or rect.is_empty:
            return None
        try:
            pix = pdf.load_page(page_number - 1).get_pixmap(
                clip=rect, matrix=fitz.Matrix(self._zoom, self._zoom), alpha=False
            )
        except Exception:  # noqa: BLE001 — a render failure must not break ingestion
            logger.warning("Visual backfill: failed to render block %s", block.id)
            return None
        if pix.width < 2 or pix.height < 2:
            return None
        target = output_dir / f"{block.id}.png"
        pix.save(target)
        caption = (block.text or block.markdown or str(block.metadata.get("caption_text") or ""))[
            :_CAPTION_MAX_CHARS
        ]
        return VisualEvidence(
            kind=block.kind,
            image_path=str(target),
            page_number=page_number,
            bbox=block.source.bbox,
            caption=caption,
        )

    @staticmethod
    def _to_pdf_rect(bbox: BoundingBox, page_rect: fitz.Rect) -> fitz.Rect | None:
        """Convert a block bbox into PDF point coordinates, padded and clamped.

        ``normalized_0_1000`` (used by both parsers) scales to the page size;
        ``pdf_points`` is used as-is; ``pixels`` cannot be mapped without a dpi.
        """
        if bbox.coordinate_space == "normalized_0_1000":
            rect = fitz.Rect(
                bbox.x0 / 1000 * page_rect.width,
                bbox.y0 / 1000 * page_rect.height,
                bbox.x1 / 1000 * page_rect.width,
                bbox.y1 / 1000 * page_rect.height,
            )
        elif bbox.coordinate_space == "pdf_points":
            rect = fitz.Rect(bbox.x0, bbox.y0, bbox.x1, bbox.y1)
        else:
            return None
        padding = max(1.0, rect.width * _BBOX_PADDING_RATIO)
        rect += (-padding, -padding, padding, padding)
        return rect & page_rect  # clamp into the page
