from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from scholarrag.config import Settings
from scholarrag.domain.models import (
    Answer,
    Citation,
    ClaimCitation,
    EvidenceSpan,
    RetrievedChunk,
)
from scholarrag.providers.siliconflow import SiliconFlowClient
from scholarrag.rag.runs import EventSink, PipelineTracer
from scholarrag.rag.visual_routing import (
    load_quality_reports,
    merge_routing_decisions,
    visual_routing_policy,
)
from scholarrag.rag.visuals import build_multimodal_content, default_prompt
from scholarrag.retrieval.service import RetrievalService
from scholarrag.storage.catalog import Catalog

# Blocks that act as structural anchors rather than evidence content. Citation
# hover/click should skip these and land on the first real content block.
_ANCHOR_KINDS = {"title", "heading", "header", "footer", "page_number"}
_CANONICAL_BLOCK_KINDS: dict[str, dict[str, str]] = {}


def _block_kinds_by_parse(catalog: Catalog, parse_id: str) -> dict[str, str]:
    """Map ``block_id -> kind`` from the canonical document, cached per parse."""
    cached = _CANONICAL_BLOCK_KINDS.get(parse_id)
    if cached is not None:
        return cached
    kinds: dict[str, str] = {}
    record = catalog.get_parse(parse_id)
    if record is not None:
        canonical = Path(str(record["artifact_dir"])) / "canonical" / "document.json"
        if canonical.is_file():
            try:
                payload = json.loads(canonical.read_text(encoding="utf-8"))
                kinds = {
                    str(block["id"]): str(block.get("kind") or "")
                    for block in payload.get("blocks", [])
                    if block.get("id")
                }
            except (OSError, ValueError):
                kinds = {}
    _CANONICAL_BLOCK_KINDS[parse_id] = kinds
    return kinds


SYSTEM_PROMPT = """你是严谨的论文阅读助手。只能根据给出的证据回答。
要求：
1. 每个可验证的事实陈述后都标注一个或多个证据编号，例如 [C1] 或 [C1][C3]。
2. 不要编造证据中没有的信息；证据不足时明确说明。
3. 保留公式、实验数值、方法名和限制条件的精确含义。
4. 可以综合多个证据，但不能引用不存在的编号。
5. 直接回答用户问题，不输出思维过程。
6. 一个句子只表达一组能被同一批引用直接蕴含的事实；不要把无证据的架构、训练细节或实验结论夹在有引用的句子中。
7. 不使用模型记忆补全论文内容，也不要从“证据未提及”推断论文一定没有某个机制。
"""


class RAGService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.retrieval = RetrievalService(settings)
        self.catalog = Catalog(settings)

    async def answer(
        self,
        question: str,
        *,
        document_ids: list[str] | None = None,
        top_k: int = 8,
        events: EventSink | None = None,
        trace_id: str | None = None,
    ) -> Answer:
        answer = await self._answer(
            question,
            document_ids=document_ids,
            top_k=top_k,
            events=events,
            trace_id=trace_id,
        )
        return answer

    async def _answer(
        self,
        question: str,
        *,
        document_ids: list[str] | None,
        top_k: int,
        events: EventSink | None,
        trace_id: str | None,
    ) -> Answer:
        answer_trace_id = trace_id or uuid.uuid4().hex
        pipeline = PipelineTracer(events)
        retrieved, debug = await self.retrieval.retrieve(
            question,
            document_ids=document_ids,
            top_k=top_k,
            events=events,
        )
        if not retrieved:
            await pipeline.skip(
                "context.assemble",
                "组装回答上下文",
                component="rag",
                summary="没有检索到候选证据",
            )
            await pipeline.skip(
                "generation.answer",
                "大模型生成回答",
                component="siliconflow",
                model=self.settings.chat_model,
                summary="没有可用上下文",
            )
            await pipeline.skip(
                "citation.validate",
                "引用提取与校验",
                component="rag",
                summary="回答未进入生成阶段",
            )
            return Answer(
                answer="当前论文库中没有检索到足以回答该问题的证据。",
                citations=[],
                trace_id=answer_trace_id,
                retrieval_debug=debug,
            )

        async with pipeline.stage(
            "context.assemble",
            "组装回答上下文",
            component="rag",
            input={"candidate_count": len(retrieved)},
        ) as stage:
            contexts = "\n\n".join(
                self._format_context(item, index) for index, item in enumerate(retrieved, 1)
            )
            stage.complete(
                output={"context_count": len(retrieved), "context_chars": len(contexts)},
                summary=f"{len(retrieved)} 条证据，{len(contexts)} 字符",
            )

        provider = SiliconFlowClient(self.settings)
        try:
            async with pipeline.stage(
                "generation.answer",
                "大模型生成回答",
                component="siliconflow",
                model=self.settings.chat_model,
                input={"context_count": len(retrieved), "context_chars": len(contexts)},
            ) as stage:
                reports = load_quality_reports(
                    Catalog(self.settings),
                    {item.chunk.document_id for item in retrieved},
                )
                routing = merge_routing_decisions(
                    [visual_routing_policy(report) for report in reports]
                )
                visual = build_multimodal_content(
                    default_prompt(question, contexts),
                    retrieved,
                    self.settings,
                    force=routing.force_visual,
                )
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": visual.parts},
                ]
                generation_usage: dict[str, object] = {}
                if events:
                    answer_parts: list[str] = []
                    pending_delta = ""
                    last_emit = time.perf_counter()
                    async with provider.chat_stream(messages) as stream:
                        async for chunk in stream.chunks():
                            choices = chunk.get("choices") or []
                            content = (
                                choices[0].get("delta", {}).get("content") if choices else None
                            )
                            if not content:
                                continue
                            answer_parts.append(str(content))
                            pending_delta += str(content)
                            now = time.perf_counter()
                            if len(pending_delta) >= 80 or now - last_emit >= 0.12:
                                await events.emit(
                                    event_type="generation.delta",
                                    stage_id="generation.answer",
                                    label="大模型生成回答",
                                    status="running",
                                    component="siliconflow",
                                    model=self.settings.chat_model,
                                    output={
                                        "text_delta": pending_delta,
                                        "answer_chars": sum(map(len, answer_parts)),
                                    },
                                )
                                pending_delta = ""
                                last_emit = now
                        if pending_delta:
                            await events.emit(
                                event_type="generation.delta",
                                stage_id="generation.answer",
                                label="大模型生成回答",
                                status="running",
                                component="siliconflow",
                                model=self.settings.chat_model,
                                output={
                                    "text_delta": pending_delta,
                                    "answer_chars": sum(map(len, answer_parts)),
                                },
                            )
                        generation_trace_id = stream.trace_id
                        generation_usage = stream.usage
                    answer_text = "".join(answer_parts)
                else:
                    response = await provider.chat(messages)
                    answer_text = response.data["choices"][0]["message"]["content"]
                    generation_trace_id = response.trace_id
                    generation_usage = response.data.get("usage") or {}
                estimated_cost = self._generation_cost(generation_usage)
                stage.complete(
                    output={
                        "answer_chars": len(answer_text),
                        "usage": generation_usage,
                        "estimated_cost_cny": estimated_cost,
                        "visual_images_included": visual.images_included,
                        "visual_images_skipped": visual.images_skipped,
                        "visual_force": routing.force_visual,
                        "visual_routing_reasons": routing.reasons,
                    },
                    trace={"provider_trace_id": generation_trace_id},
                    summary=f"生成 {len(answer_text)} 字符",
                )
        finally:
            await provider.close()

        async with pipeline.stage(
            "citation.validate",
            "引用提取与校验",
            component="rag",
            input={"available_contexts": len(retrieved)},
        ) as stage:
            referenced_indexes = [int(value) for value in re.findall(r"\[C(\d+)\]", answer_text)]
            cited_indexes = sorted(
                {index for index in referenced_indexes if 1 <= index <= len(retrieved)}
            )
            invalid_indexes = sorted(
                {index for index in referenced_indexes if not 1 <= index <= len(retrieved)}
            )
            citations = [
                self._citation(retrieved[index - 1], index, catalog=self.catalog)
                for index in cited_indexes
            ]
            claims = self._claims(answer_text, retrieved)
            stage.complete(
                output={
                    "citation_count": len(citations),
                    "cited_contexts": cited_indexes,
                    "invalid_citation_indexes": invalid_indexes,
                    "claim_count": len(claims),
                    "located_claim_count": sum(claim.evidence_located for claim in claims),
                    "supported_claim_count": sum(claim.supported for claim in claims),
                },
                status="warning" if invalid_indexes else "success",
                summary=(
                    f"{len(citations)} 个有效引用"
                    if not invalid_indexes
                    else f"{len(citations)} 个有效引用，{len(invalid_indexes)} 个无效编号"
                ),
            )
        debug.update(
            {
                "retrieved_chunk_ids": [item.chunk.id for item in retrieved],
                "generation_trace_id": generation_trace_id,
                "generation_usage": generation_usage,
                "generation_estimated_cost_cny": estimated_cost,
                "chat_model": self.settings.chat_model,
                "cited_contexts": cited_indexes,
            }
        )
        return Answer(
            answer=answer_text,
            citations=citations,
            trace_id=answer_trace_id,
            retrieval_debug=debug,
            claims=claims,
        )

    def _generation_cost(self, usage: dict[str, object]) -> float | None:
        input_rate = self.settings.chat_input_cny_per_million_tokens
        output_rate = self.settings.chat_output_cny_per_million_tokens
        if input_rate <= 0 and output_rate <= 0:
            return None
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        return round(
            (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000,
            8,
        )

    @staticmethod
    def _format_context(item: RetrievedChunk, index: int) -> str:
        pages = ",".join(str(page) for page in item.chunk.page_numbers)
        title = item.chunk.metadata.get("title") or item.chunk.document_id
        parent_text = item.chunk.metadata.get("parent_text")
        parent = f"\n章节父上下文：\n{parent_text}" if parent_text else ""
        return f"[C{index}] 论文：{title}；页码：{pages}\n{item.chunk.text}{parent}"

    @staticmethod
    def _citation(
        item: RetrievedChunk,
        index: int,
        *,
        catalog: Catalog | None = None,
    ) -> Citation:
        """Build a citation anchored on the chunk's first *content* block.

        Chunks often start with a section heading (the semantic chunker keeps
        the heading with its body), so ``sources[0]`` would point at the
        heading — clicking the citation then jumps to the title instead of
        the actual evidence. With a catalog, structural anchors (title,
        heading, header, footer, page numbers) are skipped so the citation
        lands on the same content the claim spans point at.
        """
        source = item.chunk.sources[0]
        if catalog is not None and len(item.chunk.sources) == len(item.chunk.block_ids):
            kinds = _block_kinds_by_parse(catalog, item.chunk.parse_id)
            for position, block_id in enumerate(item.chunk.block_ids):
                if kinds.get(block_id) not in _ANCHOR_KINDS:
                    source = item.chunk.sources[position]
                    break
        return Citation(
            citation_id=f"C{index}",
            document_id=item.chunk.document_id,
            chunk_id=item.chunk.id,
            page_number=source.page_number,
            block_ids=item.chunk.block_ids,
            bbox=source.bbox,
            quote=item.chunk.text[:500],
            score=item.score,
        )

    @classmethod
    def _claims(cls, answer: str, retrieved: list[RetrievedChunk]) -> list[ClaimCitation]:
        claims: list[ClaimCitation] = []
        pattern = re.compile(
            r".+?(?:(?<!\d)[。！？!?]|(?<!\d)\.(?!\d)|\n|$)",
            re.MULTILINE,
        )
        fragments = [
            (match.start(), match.end(), match.group().strip())
            for match in pattern.finditer(answer)
        ]
        position = 0
        while position < len(fragments):
            start, end, raw = fragments[position]
            position += 1
            text = re.sub(r"\s*\[C\d+\]", "", raw).strip()
            if len(text) < 6:
                continue
            # Merge heading-like fragments (list titles, bold labels, trailing
            # colons) that carry no citation with the following sentences so
            # Markdown structure does not surface as unsupported pseudo-claims.
            # Once a merge starts, keep absorbing wrapped heading lines until
            # a citation shows up or the budget is exhausted.
            fragment_like = re.search(
                r"(?:^\s*(?:[-*#+\d]+[.)、]|\*{1,2}\S))|[:：]\s*$|\*{1,2}\s*$", text
            )
            merged = 0
            while (
                merged < 6
                and len(text) < 400
                and not re.findall(r"\[C\d+\]", raw)
                and (merged > 0 or fragment_like)
                and position < len(fragments)
            ):
                _, end, next_raw = fragments[position]
                raw = f"{raw} {next_raw}"
                text = re.sub(r"\s*\[C\d+\]", "", raw).strip()
                position += 1
                merged += 1
            indexes = sorted(
                {
                    int(value)
                    for value in re.findall(r"\[C(\d+)\]", raw)
                    if 1 <= int(value) <= len(retrieved)
                }
            )
            spans = [cls._best_span(text, retrieved[index - 1], index) for index in indexes]
            claims.append(
                ClaimCitation(
                    claim_id=f"CL{len(claims) + 1}",
                    text=text,
                    answer_start_char=start,
                    answer_end_char=end,
                    citation_ids=[f"C{index}" for index in indexes],
                    evidence_spans=spans,
                    support_status="not_checked",
                )
            )
        return claims

    @staticmethod
    def _best_span(claim: str, item: RetrievedChunk, index: int) -> EvidenceSpan:
        sentences = list(
            re.finditer(
                r".+?(?:(?<!\d)[。！？!?]|(?<!\d)\.(?!\d)|\n|$)",
                item.chunk.text,
                re.MULTILINE,
            )
        )
        claim_terms = set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", claim.lower()))

        def overlap(candidate: re.Match[str]) -> tuple[float, int]:
            terms = set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", candidate.group().lower()))
            score = len(claim_terms & terms) / max(1, len(claim_terms | terms))
            return score, len(candidate.group())

        best = max(sentences, key=overlap) if sentences else None
        start = best.start() if best else 0
        end = best.end() if best else min(len(item.chunk.text), 500)
        while start < end and item.chunk.text[start].isspace():
            start += 1
        while end > start and item.chunk.text[end - 1].isspace():
            end -= 1
        source_index = 0
        exact = len(item.chunk.sources) == len(item.chunk.block_ids) == 1
        for offset in item.chunk.metadata.get("block_offsets", []):
            if offset["start"] <= start and end <= offset["end"]:
                try:
                    source_index = item.chunk.block_ids.index(offset["block_id"])
                    exact = source_index < len(item.chunk.sources)
                except ValueError:
                    exact = False
                break
        source = item.chunk.sources[source_index]
        return EvidenceSpan(
            citation_id=f"C{index}",
            chunk_id=item.chunk.id,
            block_id=(item.chunk.block_ids[source_index] if exact else None),
            page_number=source.page_number,
            bbox=source.bbox if exact else None,
            quote=item.chunk.text[start:end].strip(),
            start_char=start,
            end_char=end,
            parse_id=item.chunk.parse_id,
            location_status="exact" if exact else "approximate",
        )
