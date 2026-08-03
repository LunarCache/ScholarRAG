from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from scholarrag.domain.models import (
    BlockKind,
    BoundingBox,
    CanonicalDocument,
    ContentBlock,
    Page,
    SourcePointer,
)
from scholarrag.ingestion.associations import link_visual_captions
from scholarrag.ingestion.heading_hierarchy import classify_heading, heading_metadata

TYPE_TO_KIND = {
    "title": BlockKind.HEADING,
    "paragraph": BlockKind.PARAGRAPH,
    "list": BlockKind.LIST,
    "equation_interline": BlockKind.FORMULA,
    "table": BlockKind.TABLE,
    "image": BlockKind.FIGURE,
    "chart": BlockKind.FIGURE,
    "page_footnote": BlockKind.FOOTNOTE,
    "page_header": BlockKind.HEADER,
    "page_footer": BlockKind.FOOTER,
    "page_aside_text": BlockKind.FOOTNOTE,
    "code": BlockKind.CODE,
    "page_number": BlockKind.PAGE_NUMBER,
    "reference": BlockKind.REFERENCE,
}


class MinerUNormalizer:
    def normalize(
        self,
        raw_dir: Path,
        *,
        document_id: str,
        parse_id: str,
        source_sha256: str,
        source_filename: str,
        provider_model: str,
    ) -> CanonicalDocument:
        content_path = self._single(raw_dir, "*_content_list_v2.json")
        layout_path = raw_dir / "layout.json"
        content_pages: list[list[dict[str, Any]]] = json.loads(
            content_path.read_text(encoding="utf-8")
        )
        layout = json.loads(layout_path.read_text(encoding="utf-8")) if layout_path.exists() else {}
        page_sizes = {
            int(page["page_idx"]): page.get("page_size", [None, None])
            for page in layout.get("pdf_info", [])
        }

        pages: list[Page] = []
        blocks: list[ContentBlock] = []
        section_stack: list[str] = []
        document_title: str | None = None
        order = 0

        for page_idx, entries in enumerate(content_pages):
            page_block_ids: list[str] = []
            for item_idx, entry in enumerate(entries):
                provider_type = str(entry.get("type", "unknown"))
                content = entry.get("content") or {}
                text, markdown, html, latex, image_path = self._content_fields(
                    provider_type, content
                )
                kind = TYPE_TO_KIND.get(provider_type, BlockKind.UNKNOWN)
                provider_level = int(content.get("level", 0) or 0) or None
                level: int | None = None
                heading_provenance: dict[str, object] = {}
                if provider_type == "title":
                    if document_title is None:
                        document_title = text.strip() or None
                        kind = BlockKind.TITLE
                    else:
                        classification = classify_heading(text, provider_level)
                        kind = classification.kind
                        level = classification.level
                        heading_provenance = heading_metadata(classification)
                    if kind == BlockKind.HEADING and level is not None:
                        section_stack = section_stack[: level - 1]
                        section_stack.append(text.strip())

                block_id = self._stable_id(parse_id, page_idx, item_idx, provider_type, text)
                bbox = self._bbox(entry.get("bbox"))
                raw_pointer = f"/{page_idx}/{item_idx}"
                source = SourcePointer(
                    document_id=document_id,
                    parse_id=parse_id,
                    provider="mineru",
                    provider_block_id=f"p{page_idx + 1}-b{item_idx}",
                    page_number=page_idx + 1,
                    bbox=bbox,
                    raw_artifact=str(content_path),
                    raw_json_pointer=raw_pointer,
                )
                block = ContentBlock(
                    id=block_id,
                    kind=kind,
                    text=text.strip(),
                    markdown=markdown,
                    html=html,
                    latex=latex,
                    image_path=str(raw_dir / image_path) if image_path else None,
                    reading_order=order,
                    section_path=list(section_stack),
                    source=source,
                    metadata={
                        "provider_type": provider_type,
                        **(
                            heading_provenance
                            if heading_provenance
                            else {
                                "provider_heading_level": provider_level,
                                "heading_level": None,
                            }
                        ),
                    },
                )
                blocks.append(block)
                page_block_ids.append(block_id)
                order += 1

            size = page_sizes.get(page_idx, [None, None])
            pages.append(
                Page(
                    page_number=page_idx + 1,
                    width=size[0],
                    height=size[1],
                    block_ids=page_block_ids,
                )
            )

        link_visual_captions(blocks)
        return CanonicalDocument(
            document_id=document_id,
            parse_id=parse_id,
            source_sha256=source_sha256,
            source_filename=source_filename,
            provider="mineru",
            provider_model=provider_model,
            title=document_title,
            abstract=self._find_abstract(blocks),
            pages=pages,
            blocks=blocks,
            raw_artifacts=[str(path) for path in sorted(raw_dir.rglob("*")) if path.is_file()],
            metadata={
                "normalizer": "mineru-content-list-v2",
                "coordinate_space": "normalized_0_1000",
            },
        )

    @staticmethod
    def _single(root: Path, pattern: str) -> Path:
        matches = list(root.glob(pattern))
        if len(matches) != 1:
            raise ValueError(f"Expected one {pattern} in {root}, found {len(matches)}")
        return matches[0]

    @staticmethod
    def _stable_id(parse_id: str, page_idx: int, item_idx: int, kind: str, text: str) -> str:
        value = f"{parse_id}:{page_idx}:{item_idx}:{kind}:{text}".encode()
        return f"blk_{hashlib.sha256(value).hexdigest()[:20]}"

    @staticmethod
    def _bbox(value: Any) -> BoundingBox | None:
        if not isinstance(value, list) or len(value) != 4:
            return None
        return BoundingBox(
            x0=float(value[0]),
            y0=float(value[1]),
            x1=float(value[2]),
            y1=float(value[3]),
            coordinate_space="normalized_0_1000",
        )

    def _content_fields(
        self, provider_type: str, content: dict[str, Any]
    ) -> tuple[str, str | None, str | None, str | None, str | None]:
        image_path = (content.get("image_source") or {}).get("path")
        html = content.get("html")
        latex = content.get("math_content") if provider_type == "equation_interline" else None
        if latex:
            return latex, f"$$\n{latex}\n$$", None, latex, image_path

        keys = {
            "title": "title_content",
            "paragraph": "paragraph_content",
            "page_header": "page_header_content",
            "page_footer": "page_footer_content",
            "page_footnote": "page_footnote_content",
            "page_aside_text": "page_aside_text_content",
            "code": "code_content",
            "page_number": "page_number_content",
            "reference": "reference_content",
            "image": "image_caption",
            "chart": "chart_caption",
            "table": "table_caption",
        }
        if provider_type == "list":
            parts = [
                self._inline(item.get("item_content", [])) for item in content.get("list_items", [])
            ]
            text = "\n".join(part for part in parts if part)
        else:
            text = self._inline(content.get(keys.get(provider_type, "content"), []))

        if html:
            table_text = re.sub(r"<[^>]+>", " ", html)
            table_text = re.sub(r"\s+", " ", table_text).strip()
            text = "\n".join(part for part in (text, table_text) if part)
        markdown = text or None
        if image_path and text:
            markdown = f"![{text}]({image_path})"
        return text, markdown, html, None, image_path

    def _inline(self, values: Any) -> str:
        if isinstance(values, str):
            return values
        if isinstance(values, dict):
            item_type = values.get("type")
            content = values.get("content", "")
            if item_type == "equation_inline":
                return f"${content}$"
            return self._inline(content)
        if isinstance(values, list):
            return "".join(self._inline(value) for value in values)
        return ""

    @staticmethod
    def _find_abstract(blocks: list[ContentBlock]) -> str | None:
        for index, block in enumerate(blocks[:-1]):
            if (
                block.kind in {BlockKind.TITLE, BlockKind.HEADING}
                and block.text.lower() == "abstract"
            ):
                candidate = blocks[index + 1]
                if candidate.kind == BlockKind.PARAGRAPH:
                    return candidate.text
        return None
