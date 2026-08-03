from __future__ import annotations

from scholarrag.domain.models import BlockKind, ContentBlock


def link_visual_captions(blocks: list[ContentBlock]) -> None:
    """Link split visual regions to nearby captions without changing extracted text."""
    candidates = [
        block
        for block in blocks
        if block.text.strip()
        and block.kind in {BlockKind.CAPTION, BlockKind.FIGURE, BlockKind.TABLE}
    ]
    for block in blocks:
        if block.kind not in {BlockKind.FIGURE, BlockKind.TABLE} or block.text.strip():
            continue
        nearby = [
            candidate
            for candidate in candidates
            if candidate.source.page_number == block.source.page_number
            and abs(candidate.reading_order - block.reading_order) <= 4
        ]
        if not nearby:
            continue
        caption = min(
            nearby,
            key=lambda candidate: (
                0 if candidate.kind == BlockKind.CAPTION else 1,
                abs(candidate.reading_order - block.reading_order),
            ),
        )
        block.metadata["caption_block_id"] = caption.id
        block.metadata["caption_text"] = caption.text
        caption.metadata.setdefault("visual_block_ids", []).append(block.id)
        if block.image_path:
            block.markdown = f"![{caption.text}]({block.image_path})"
