from __future__ import annotations

import hashlib

from scholarrag.domain.models import (
    BlockKind,
    CanonicalDocument,
    Chunk,
    ContentBlock,
    VisualEvidence,
)

INDEXABLE_KINDS = {
    BlockKind.TITLE,
    BlockKind.HEADING,
    BlockKind.PARAGRAPH,
    BlockKind.LIST,
    BlockKind.FORMULA,
    BlockKind.TABLE,
    BlockKind.FIGURE,
    BlockKind.CAPTION,
    BlockKind.CODE,
    BlockKind.REFERENCE,
}


class SemanticChunker:
    def __init__(self, max_chars: int = 1800, parent_max_chars: int = 6000) -> None:
        self.max_chars = max_chars
        self.parent_max_chars = parent_max_chars

    def chunk(self, document: CanonicalDocument) -> list[Chunk]:
        section_groups: list[list[ContentBlock]] = []
        current: list[ContentBlock] = []
        current_size = 0
        current_section: tuple[str, ...] | None = None

        for block in document.blocks:
            if block.kind not in INDEXABLE_KINDS or not (
                block.text or block.html or block.image_path
            ):
                continue
            block_size = len(block.text) + (len(block.html or "") // 2)
            section = tuple(block.section_path)
            section_changed = (
                current and section != current_section and block.kind == BlockKind.HEADING
            )
            too_large = current and current_size + block_size > self.parent_max_chars
            if section_changed or too_large:
                section_groups.append(current)
                current = []
                current_size = 0
            current.append(block)
            current_size += block_size
            current_section = section
        if current:
            section_groups.append(current)

        chunks: list[Chunk] = []
        sequence = 0
        for section_index, group in enumerate(section_groups):
            size = sum(len(block.text) + len(block.html or "") // 2 for block in group)
            if size <= self.max_chars:
                chunks.append(self._make_chunk(document, group, sequence, level="atomic"))
                sequence += 1
                continue
            parent = self._make_chunk(
                document,
                group,
                sequence,
                level="parent",
                metadata={"section_index": section_index},
            )
            chunks.append(parent)
            sequence += 1
            for child_blocks in self._split_children(group):
                chunks.append(
                    self._make_chunk(
                        document,
                        child_blocks,
                        sequence,
                        level="child",
                        parent_chunk_id=parent.id,
                        metadata={"section_index": section_index},
                    )
                )
                sequence += 1
        return chunks

    def _split_children(self, blocks: list[ContentBlock]) -> list[list[ContentBlock]]:
        children: list[list[ContentBlock]] = []
        current: list[ContentBlock] = []
        size = 0
        atomic_kinds = {BlockKind.TABLE, BlockKind.FORMULA, BlockKind.FIGURE}
        for block in blocks:
            block_size = len(block.text) + len(block.html or "") // 2
            if current and (size + block_size > self.max_chars or block.kind in atomic_kinds):
                children.append(current)
                current = []
                size = 0
            current.append(block)
            size += block_size
            if block.kind in atomic_kinds or size >= self.max_chars:
                children.append(current)
                current = []
                size = 0
        if current:
            children.append(current)
        return children

    @staticmethod
    def _make_chunk(
        document: CanonicalDocument,
        blocks: list[ContentBlock],
        index: int,
        *,
        level: str,
        parent_chunk_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Chunk:
        text = "\n\n".join(
            block.markdown or block.text or str(block.metadata.get("caption_text") or "")
            for block in blocks
        )
        section = blocks[-1].section_path if blocks else []
        prefix = " · ".join(part for part in (document.title, *section) if part)
        contextual = f"{prefix}\n\n{text}" if prefix else text
        identity = f"{document.parse_id}:{index}:{','.join(block.id for block in blocks)}"
        chunk_id = f"chk_{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
        pages = sorted({block.source.page_number for block in blocks})
        visual_evidence = [
            VisualEvidence(
                kind=block.kind,
                image_path=block.image_path,
                page_number=block.source.page_number,
                bbox=block.source.bbox,
                caption=(
                    block.text or block.markdown or str(block.metadata.get("caption_text") or "")
                )[:500],
            )
            for block in blocks
            if block.kind in {BlockKind.FIGURE, BlockKind.TABLE} and block.image_path
        ]
        return Chunk(
            id=chunk_id,
            document_id=document.document_id,
            parse_id=document.parse_id,
            text=text,
            contextual_text=contextual,
            block_ids=[block.id for block in blocks],
            page_numbers=pages,
            section_path=section,
            token_estimate=max(1, len(contextual) // 4),
            sources=[block.source for block in blocks],
            level=level,
            parent_chunk_id=parent_chunk_id,
            metadata={"chunk_index": index, "title": document.title, **(metadata or {})},
            visual_evidence=visual_evidence,
        )
