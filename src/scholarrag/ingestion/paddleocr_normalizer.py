from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
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

LABEL_TO_KIND = {
    "doc_title": BlockKind.TITLE,
    "paragraph_title": BlockKind.HEADING,
    "abstract": BlockKind.PARAGRAPH,
    "text": BlockKind.PARAGRAPH,
    "reference": BlockKind.REFERENCE,
    "reference_content": BlockKind.REFERENCE,
    "code": BlockKind.CODE,
    "page_number": BlockKind.PAGE_NUMBER,
    "display_formula": BlockKind.FORMULA,
    "formula": BlockKind.FORMULA,
    "formula_number": BlockKind.CAPTION,
    "table": BlockKind.TABLE,
    "chart": BlockKind.FIGURE,
    "image": BlockKind.FIGURE,
    "figure_title": BlockKind.CAPTION,
    "footnote": BlockKind.FOOTNOTE,
    "aside_text": BlockKind.FOOTNOTE,
    "header": BlockKind.HEADER,
    "footer": BlockKind.FOOTER,
}
VISUAL_LABELS = {"image", "chart", "table"}
HEADING_PATTERN = re.compile(r"^(#{1,6})\s*(.*)$", re.DOTALL)
IMAGE_SRC_PATTERN = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag in {"tr", "p", "div", "li", "br"}:
            self.parts.append("\n")

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"[ \t]{2,}", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


class PaddleOCRNormalizer:
    """Normalize hosted PaddleOCR document parsing results without discarding raw fields."""

    def normalize(
        self,
        payload_path: Path,
        *,
        resource_dir: Path,
        output_dir: Path,
        source_pdf: Path,
        document_id: str,
        parse_id: str,
        source_sha256: str,
        source_filename: str,
        provider_model: str,
        page_ranges: str | None = None,
    ) -> CanonicalDocument:
        payload_path = payload_path.resolve()
        resource_dir = resource_dir.resolve()
        output_dir = output_dir.resolve()
        source_pdf = source_pdf.resolve()
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        raw_pages = payload.get("pages") or []
        page_numbers = self.expand_page_ranges(page_ranges, len(raw_pages))
        output_dir.mkdir(parents=True, exist_ok=True)

        pages: list[Page] = []
        blocks: list[ContentBlock] = []
        section_stack: list[str] = []
        title: str | None = None
        abstract: str | None = None
        global_order = 0

        for result_index, (raw_page, page_number) in enumerate(
            zip(raw_pages, page_numbers, strict=True)
        ):
            pruned = raw_page.get("pruned_result") or {}
            width = self._number(pruned.get("width"))
            height = self._number(pruned.get("height"))
            entries = pruned.get("parsing_res_list") or []
            page_block_ids: list[str] = []

            for item_index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("block_label") or "unknown")
                kind = LABEL_TO_KIND.get(label, BlockKind.UNKNOWN)
                raw_content = str(entry.get("block_content") or "")
                text, markdown, html_value, latex, heading_level = self._content_fields(
                    label, raw_content
                )
                heading_provenance: dict[str, object] = {}
                if label == "doc_title" and text:
                    title = title or text
                if label == "abstract" and text:
                    abstract = abstract or text
                if kind == BlockKind.HEADING and text:
                    classification = classify_heading(text, heading_level or 1)
                    kind = classification.kind
                    heading_level = classification.level
                    heading_provenance = heading_metadata(classification)
                    if kind == BlockKind.HEADING and heading_level is not None:
                        section_stack = section_stack[: max(0, heading_level - 1)]
                        section_stack.append(text)

                raw_bbox = entry.get("block_bbox")
                bbox = self._normalized_bbox(raw_bbox, width=width, height=height)
                image_path = self._image_path(
                    resource_dir,
                    page_result_index=result_index,
                    label=label,
                    raw_content=raw_content,
                    raw_bbox=raw_bbox,
                )
                if label in VISUAL_LABELS and image_path is None and bbox is not None:
                    image_path = self._crop_visual(
                        source_pdf,
                        page_number=page_number,
                        bbox=bbox,
                        output_dir=output_dir,
                        label=label,
                        item_index=item_index,
                    )
                if image_path is not None and markdown:
                    markdown = self._localize_image_reference(markdown, image_path)

                provider_block_id = entry.get("block_id")
                block_id = self._stable_id(
                    parse_id, page_number, item_index, label, provider_block_id, text
                )
                raw_pointer = f"/pages/{result_index}/pruned_result/parsing_res_list/{item_index}"
                source = SourcePointer(
                    document_id=document_id,
                    parse_id=parse_id,
                    provider="paddleocr",
                    provider_block_id=f"p{page_number}-b{provider_block_id}",
                    page_number=page_number,
                    bbox=bbox,
                    raw_artifact=str(payload_path),
                    raw_json_pointer=raw_pointer,
                )
                block = ContentBlock(
                    id=block_id,
                    kind=kind,
                    text=text,
                    markdown=markdown,
                    html=html_value,
                    latex=latex,
                    image_path=str(image_path) if image_path is not None else None,
                    reading_order=global_order,
                    section_path=list(section_stack),
                    source=source,
                    metadata={
                        "provider_label": label,
                        "provider_block_order": entry.get("block_order"),
                        "provider_group_id": entry.get("group_id"),
                        "provider_raw_order": item_index,
                        "provider_raw_bbox": raw_bbox,
                        "provider_polygon_points": entry.get("block_polygon_points"),
                        **(
                            heading_provenance
                            if heading_provenance
                            else {
                                "provider_heading_level": heading_level,
                                "heading_level": heading_level,
                            }
                        ),
                        "content_sha256": hashlib.sha256(raw_content.encode()).hexdigest(),
                    },
                )
                blocks.append(block)
                page_block_ids.append(block_id)
                global_order += 1

            pages.append(
                Page(
                    page_number=page_number,
                    width=width,
                    height=height,
                    block_ids=page_block_ids,
                )
            )

        raw_artifacts = [str(payload_path)]
        if resource_dir.exists():
            raw_artifacts.extend(
                str(path) for path in sorted(resource_dir.rglob("*")) if path.is_file()
            )
        link_visual_captions(blocks)
        return CanonicalDocument(
            document_id=document_id,
            parse_id=parse_id,
            source_sha256=source_sha256,
            source_filename=source_filename,
            provider="paddleocr",
            provider_model=provider_model,
            title=title,
            abstract=abstract,
            pages=pages,
            blocks=blocks,
            raw_artifacts=raw_artifacts,
            metadata={
                "normalizer": "paddleocr-document-parsing-v1",
                "coordinate_space": "normalized_0_1000",
                "provider_job_id": payload.get("job_id") or payload.get("jobId"),
                "page_ranges": page_ranges,
                "provider_data_info": payload.get("data_info") or {},
            },
        )

    @staticmethod
    def expand_page_ranges(page_ranges: str | None, returned_pages: int) -> list[int]:
        if not page_ranges:
            return list(range(1, returned_pages + 1))
        selected: list[int] = []
        for part in page_ranges.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
                if start < 1 or end < start:
                    raise ValueError(f"Invalid page range: {page_ranges!r}")
                selected.extend(range(start, end + 1))
            else:
                value = int(part)
                if value < 1:
                    raise ValueError(f"Invalid page range: {page_ranges!r}")
                selected.append(value)
        if len(selected) != returned_pages or len(set(selected)) != len(selected):
            raise ValueError(
                f"Page range {page_ranges!r} selects {len(selected)} pages, "
                f"but PaddleOCR returned {returned_pages}"
            )
        return selected

    def _content_fields(
        self, label: str, content: str
    ) -> tuple[str, str | None, str | None, str | None, int | None]:
        content = content.strip()
        heading_level = None
        if label in {"doc_title", "paragraph_title"}:
            match = HEADING_PATTERN.match(content)
            if match:
                heading_level = len(match.group(1))
                content = match.group(2).strip()
        if label in {"display_formula", "formula"}:
            latex = self._formula(content)
            return latex, f"$$\n{latex}\n$$", None, latex, None

        html_value = content if "<" in content and ">" in content else None
        text = self._html_text(content) if html_value else content
        text = re.sub(r"\s+\n", "\n", text).strip()
        markdown = content or None
        if label in {"doc_title", "paragraph_title"} and text:
            prefix = "#" * (heading_level or (1 if label == "doc_title" else 2))
            markdown = f"{prefix} {text}"
        return text, markdown, html_value, None, heading_level

    @staticmethod
    def _html_text(value: str) -> str:
        parser = _TextHTMLParser()
        parser.feed(value)
        parser.close()
        return parser.text()

    @staticmethod
    def _formula(value: str) -> str:
        value = value.strip()
        if value.startswith("$$") and value.endswith("$$") and len(value) >= 4:
            value = value[2:-2]
        return value.strip()

    @staticmethod
    def _normalized_bbox(
        value: Any, *, width: float | None, height: float | None
    ) -> BoundingBox | None:
        if (
            not isinstance(value, list)
            or len(value) != 4
            or not width
            or not height
            or width <= 0
            or height <= 0
        ):
            return None
        x0, y0, x1, y1 = (float(item) for item in value)
        return BoundingBox(
            x0=max(0.0, min(1000.0, x0 / width * 1000.0)),
            y0=max(0.0, min(1000.0, y0 / height * 1000.0)),
            x1=max(0.0, min(1000.0, x1 / width * 1000.0)),
            y1=max(0.0, min(1000.0, y1 / height * 1000.0)),
            coordinate_space="normalized_0_1000",
        )

    @staticmethod
    def _image_path(
        resource_dir: Path,
        *,
        page_result_index: int,
        label: str,
        raw_content: str,
        raw_bbox: Any,
    ) -> Path | None:
        page_dir = resource_dir / f"page-{page_result_index + 1:04d}" / "markdown"
        match = IMAGE_SRC_PATTERN.search(raw_content)
        if match:
            relative = PurePosixPath(match.group(1))
            if not relative.is_absolute() and ".." not in relative.parts:
                candidate = page_dir / Path(*relative.parts)
                if candidate.is_file():
                    return candidate
        if not isinstance(raw_bbox, list) or len(raw_bbox) != 4 or not page_dir.exists():
            return None
        coords = "_".join(str(int(float(value))) for value in raw_bbox)
        label_aliases = {"display_formula": "formula", "figure": "image"}
        resource_label = label_aliases.get(label, label)
        matches = list(page_dir.rglob(f"img_in_{resource_label}_box_{coords}.*"))
        return matches[0] if matches else None

    @staticmethod
    def _crop_visual(
        source_pdf: Path,
        *,
        page_number: int,
        bbox: BoundingBox,
        output_dir: Path,
        label: str,
        item_index: int,
    ) -> Path | None:
        import pymupdf

        target = output_dir / "assets" / f"page-{page_number:04d}" / f"{item_index:04d}-{label}.png"
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(source_pdf) as pdf:
            if page_number < 1 or page_number > pdf.page_count:
                return None
            page = pdf.load_page(page_number - 1)
            rect = page.rect
            clip = pymupdf.Rect(
                bbox.x0 / 1000.0 * rect.width,
                bbox.y0 / 1000.0 * rect.height,
                bbox.x1 / 1000.0 * rect.width,
                bbox.y1 / 1000.0 * rect.height,
            )
            if clip.is_empty or clip.is_infinite:
                return None
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False)
            pixmap.save(target)
        return target

    @staticmethod
    def _localize_image_reference(markdown: str, image_path: Path) -> str:
        return IMAGE_SRC_PATTERN.sub(
            lambda match: match.group(0).replace(match.group(1), str(image_path)), markdown
        )

    @staticmethod
    def _number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _stable_id(
        parse_id: str,
        page_number: int,
        item_index: int,
        label: str,
        provider_block_id: Any,
        text: str,
    ) -> str:
        value = (
            f"{parse_id}:{page_number}:{item_index}:{label}:{provider_block_id}:{text}"
        ).encode()
        return f"blk_{hashlib.sha256(value).hexdigest()[:20]}"
