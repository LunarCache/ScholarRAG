from __future__ import annotations

import re
from dataclasses import dataclass

from scholarrag.domain.models import BlockKind

_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+){0,5})\.?\s+(?=\S)")
_APPENDIX_HEADING = re.compile(r"^\s*([A-Z])(?:\.(\d+(?:\.\d+)*))?\.?\s+(?=\S)")
_ROMAN_HEADING = re.compile(r"^\s*(?:[IVXLCDM]+)\.?\s+(?=\S)", re.IGNORECASE)
_CAPTION = re.compile(
    r"^\s*(?:table|figure|fig\.?|algorithm|listing)\s+"
    r"(?:[A-Z]?\d+(?:\.\d+)*|[IVXLCDM]+)\s*[:.]",
    re.IGNORECASE,
)

_TOP_LEVEL_UNNUMBERED = {
    "abstract",
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
    "bibliography",
    "conclusion",
    "conclusions",
    "discussion",
    "ethics statement",
    "limitations",
    "references",
    "reproducibility statement",
}


@dataclass(frozen=True)
class HeadingClassification:
    kind: BlockKind
    level: int | None
    source: str
    provider_level: int | None

    @property
    def repaired(self) -> bool:
        return self.kind != BlockKind.HEADING or self.level != self.provider_level


def infer_explicit_heading_level(text: str) -> int | None:
    """Infer hierarchy only when the heading text carries explicit structure."""
    normalized = _normalize(text)
    if not normalized or is_caption_heading(normalized):
        return None
    if normalized.casefold().rstrip(":.") in _TOP_LEVEL_UNNUMBERED:
        return 1
    numbered = _NUMBERED_HEADING.match(normalized)
    if numbered:
        return min(6, numbered.group(1).count(".") + 1)
    appendix = _APPENDIX_HEADING.match(normalized)
    if appendix:
        suffix = appendix.group(2)
        return min(6, 1 if suffix is None else suffix.count(".") + 2)
    if _ROMAN_HEADING.match(normalized):
        return 1
    return None


def classify_heading(text: str, provider_level: int | None) -> HeadingClassification:
    """Repair provider heading metadata while retaining provenance for audit."""
    provider_level = _bounded_level(provider_level)
    if is_caption_heading(text):
        return HeadingClassification(
            kind=BlockKind.CAPTION,
            level=None,
            source="caption_guard",
            provider_level=provider_level,
        )
    inferred = infer_explicit_heading_level(text)
    if inferred is not None:
        source = (
            "semantic"
            if _normalize(text).casefold().rstrip(":.") in _TOP_LEVEL_UNNUMBERED
            else "numbering"
        )
        return HeadingClassification(
            kind=BlockKind.HEADING,
            level=inferred,
            source=source,
            provider_level=provider_level,
        )
    return HeadingClassification(
        kind=BlockKind.HEADING,
        level=provider_level or 1,
        source="provider" if provider_level is not None else "fallback",
        provider_level=provider_level,
    )


def is_caption_heading(text: str) -> bool:
    return bool(_CAPTION.match(_normalize(text)))


def heading_metadata(classification: HeadingClassification) -> dict[str, object]:
    return {
        "provider_heading_level": classification.provider_level,
        "heading_level": classification.level,
        "heading_level_source": classification.source,
        "heading_level_repaired": classification.repaired,
    }


def _bounded_level(value: int | None) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return min(6, value)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
