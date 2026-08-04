"""Visual evidence helpers: attach block images to generation messages.

OCR text describes and locates content; these helpers let the vision LLM
read the original figure/table images directly, compensating for lossy
OCR transcription of formulas, tables, and figures.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import fitz

from scholarrag.config import Settings
from scholarrag.domain.models import RetrievedChunk, VisualEvidence

_MAX_PART_TEXT = 80


@dataclass(frozen=True)
class MessageContent:
    """Result of building a user message with optional image parts.

    ``parts`` is a plain text string when images are disabled/unavailable,
    and a list of OpenAI-compatible parts when images are attached.
    """

    parts: str | list[dict[str, object]]
    images_included: int
    images_skipped: int


def image_to_data_url(
    path: str | Path, *, max_edge: int = 1024, jpeg_quality: int = 85
) -> str | None:
    """Resize an image to fit within ``max_edge`` and return a JPEG data URL.

    Returns ``None`` on any failure so the caller can degrade to text-only.
    """
    try:
        pix = fitz.Pixmap(str(path))
        if max(pix.width, pix.height) > max_edge:
            scale = max_edge / max(pix.width, pix.height)
            width = max(1, int(pix.width * scale))
            height = max(1, int(pix.height * scale))
            pix = fitz.Pixmap(pix, width, height)
        if pix.alpha:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        data = pix.tobytes("jpeg", jpg_quality=jpeg_quality)
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    except Exception:  # noqa: BLE001 — any image failure degrades to text-only
        return None


def collect_visual_evidence(
    retrieved: list[RetrievedChunk],
) -> list[tuple[int, VisualEvidence]]:
    """Collect (chunk index, evidence) pairs in ranking order, without a budget.

    A chunk contributes its first visual evidence so images spread across
    chunks instead of one chunk dominating the message. Duplicate images
    (e.g. the same figure on a parent chunk and its child) are skipped so
    the same picture is never attached twice.
    """
    collected: list[tuple[int, VisualEvidence]] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(retrieved, start=1):
        for evidence in item.chunk.visual_evidence:
            if evidence.image_path in seen_paths:
                continue
            seen_paths.add(evidence.image_path)
            collected.append((index, evidence))
            break
    return collected


def build_multimodal_content(
    prompt_text: str,
    retrieved: list[RetrievedChunk],
    settings: Settings,
    *,
    force: bool = False,
) -> MessageContent:
    """Build the user message content for a question with evidence.

    ``prompt_text`` is the full user-side text (question plus evidence).
    With ``chat_include_visual_evidence`` enabled and images available, the
    content is a list of OpenAI-compatible parts: the text followed by the
    images (each annotated with its ``[C#]`` evidence number). Otherwise the
    content is the plain text string, preserving existing behavior.

    ``force`` (from quality routing) attaches images even when the cost
    switch is disabled. There is no image-count budget: every distinct
    figure/table image among the retrieved chunks is attached.
    """
    if not settings.chat_include_visual_evidence and not force:
        return MessageContent(parts=prompt_text, images_included=0, images_skipped=0)

    evidence = collect_visual_evidence(retrieved)
    parts: list[dict[str, object]] = [{"type": "text", "text": prompt_text}]
    included = 0
    skipped = 0
    for index, visual in evidence:
        url = image_to_data_url(visual.image_path, max_edge=settings.chat_visual_max_edge)
        if url is None:
            skipped += 1
            continue
        caption = (visual.caption or "").strip()
        label = f"[C{index} 的{'图' if visual.kind.value == 'figure' else '表'}]"
        if caption:
            label = f"{label}（{caption[:_MAX_PART_TEXT]}）"
        parts.append({"type": "text", "text": label})
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": url, "detail": "high"},
            }
        )
        included += 1
    if included == 0:
        # Every image failed to load; degrade to plain text.
        return MessageContent(parts=prompt_text, images_included=0, images_skipped=skipped)
    return MessageContent(parts=parts, images_included=included, images_skipped=skipped)


def _text_prompt(question: str, contexts: str) -> str:
    return f"问题：{question}\n\n证据：\n{contexts}"


def default_prompt(question: str, contexts: str) -> str:
    """Standard user prompt: question followed by the assembled evidence."""
    return _text_prompt(question, contexts)
