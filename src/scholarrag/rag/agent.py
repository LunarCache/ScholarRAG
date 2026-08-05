from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from scholarrag.config import Settings
from scholarrag.domain.models import Answer, RetrievedChunk
from scholarrag.providers.siliconflow import SiliconFlowClient
from scholarrag.rag.runs import EventSink, PipelineTracer, StageStatus, TraceEvent
from scholarrag.rag.service import SYSTEM_PROMPT, RAGService
from scholarrag.rag.tool_agent import ToolAgentService
from scholarrag.rag.visual_routing import (
    load_quality_reports,
    merge_routing_decisions,
    visual_routing_policy,
)
from scholarrag.rag.visuals import build_multimodal_content
from scholarrag.retrieval.service import RetrievalService
from scholarrag.storage.catalog import Catalog


class AgentPlan(BaseModel):
    mode: Literal["controlled_rag", "multi_step"]
    intent: Literal["fact", "method", "formula", "visual", "comparison", "synthesis"]
    difficulty: Literal["simple", "complex"]
    subqueries: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    max_steps: int
    reason: str


class PrefixedEventSink:
    def __init__(self, sink: EventSink, prefix: str, parent_stage_id: str) -> None:
        self.sink = sink
        self.prefix = prefix
        self.parent_stage_id = parent_stage_id

    async def emit(
        self,
        *,
        event_type: str,
        stage_id: str,
        label: str,
        status: StageStatus,
        parent_stage_id: str | None = None,
        duration_ms: float | None = None,
        component: str | None = None,
        model: str | None = None,
        summary: str | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TraceEvent:
        return await self.sink.emit(
            event_type=event_type,
            stage_id=f"{self.prefix}.{stage_id}",
            label=label,
            status=status,
            parent_stage_id=(
                f"{self.prefix}.{parent_stage_id}" if parent_stage_id else self.parent_stage_id
            ),
            duration_ms=duration_ms,
            component=component,
            model=model,
            summary=summary,
            input=input,
            output=output,
            trace=trace,
            error=error,
        )


class AgenticRAGService:
    """Bounded, auditable planner that degrades to the controlled RAG workflow."""

    def __init__(
        self, settings: Settings, *, tool_calling: bool | None = None
    ) -> None:
        self.settings = settings
        # None defers to the server-side setting; the API can override per request.
        self.tool_calling = (
            settings.agent_tool_calling if tool_calling is None else tool_calling
        )
        self.retrieval = RetrievalService(settings)
        self.controlled = RAGService(settings)
        self.tool_agent = ToolAgentService(settings)

    async def answer(
        self,
        question: str,
        *,
        document_ids: list[str] | None = None,
        top_k: int = 8,
        events: EventSink | None = None,
        trace_id: str | None = None,
    ) -> Answer:
        pipeline = PipelineTracer(events)
        async with pipeline.stage(
            "agent.classify",
            "问题分类与难度判断",
            component="agent.question_analyzer",
            input={"question_chars": len(question), "document_count": len(document_ids or [])},
        ) as stage:
            plan = self.plan(question, max_steps=self.settings.agent_max_steps)
            stage.complete(
                output={
                    "intent": plan.intent,
                    "difficulty": plan.difficulty,
                    "mode": plan.mode,
                },
                summary=f"{plan.intent} / {plan.difficulty}",
            )
        async with pipeline.stage(
            "agent.plan",
            "生成可审计检索计划",
            component="agent.planner",
            input={"max_steps": plan.max_steps},
        ) as stage:
            stage.complete(
                output=plan.model_dump(mode="json"),
                summary=f"{len(plan.subqueries)} 个子问题，工具：{', '.join(plan.tools)}",
            )
        if self.tool_calling:
            # An explicit tool-calling request always uses the tool loop, even
            # for "simple" questions: the model itself decides how many search
            # rounds to run, and the trace stays consistent across turns (the
            # rule-based planner would otherwise downgrade short factual first
            # questions to controlled RAG while follow-ups — whose context is
            # padded with the parent answer — always stayed multi-step).
            # The tool loop manages its own timeout budget and falls back to
            # the controlled workflow internally, so no outer timeout here.
            answer = await self.tool_agent.answer(
                question,
                document_ids=document_ids,
                top_k=top_k,
                events=events,
                trace_id=trace_id,
            )
            if "agent_plan" not in answer.retrieval_debug:
                answer.retrieval_debug["agent_plan"] = plan.model_dump(mode="json")
            return answer

        if plan.mode == "controlled_rag":
            await pipeline.skip(
                "agent.evidence_gap",
                "证据缺口检测",
                component="agent",
                summary="简单问题降级到受控单步 RAG",
            )
            answer = await self.controlled.answer(
                question,
                document_ids=document_ids,
                top_k=top_k,
                events=events,
                trace_id=trace_id,
            )
            answer.retrieval_debug["agent_plan"] = plan.model_dump(mode="json")
            answer.retrieval_debug["agent_stop_reason"] = "controlled_rag_complete"
            return answer

        try:
            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                return await self._multi_step(
                    question,
                    plan=plan,
                    document_ids=document_ids,
                    top_k=top_k,
                    events=events,
                    trace_id=trace_id,
                )
        except TimeoutError:
            if events:
                await events.emit(
                    event_type="agent.timeout",
                    stage_id="agent.stop",
                    label="Agent 达到超时预算",
                    status="warning",
                    component="agent",
                    summary="已停止多步规划并降级到受控 RAG",
                    output={"timeout_seconds": self.settings.agent_timeout_seconds},
                )
            answer = await self.controlled.answer(
                question,
                document_ids=document_ids,
                top_k=top_k,
                events=events,
                trace_id=trace_id,
            )
            answer.retrieval_debug["agent_stop_reason"] = "timeout_fallback"
            return answer

    async def _multi_step(
        self,
        question: str,
        *,
        plan: AgentPlan,
        document_ids: list[str] | None,
        top_k: int,
        events: EventSink | None,
        trace_id: str | None,
    ) -> Answer:
        all_results: list[RetrievedChunk] = []
        step_debug: list[dict[str, Any]] = []
        for index, subquery in enumerate(plan.subqueries[: plan.max_steps], start=1):
            step_id = f"agent.step.{index}"
            started = time.perf_counter()
            if events:
                await events.emit(
                    event_type="agent.step.started",
                    stage_id=step_id,
                    label=f"子问题 {index}",
                    status="running",
                    component="agent",
                    input={"subquery": subquery, "tools": plan.tools},
                )
                step_events: EventSink | None = PrefixedEventSink(events, step_id, step_id)
            else:
                step_events = None
            results, debug = await self.retrieval.retrieve(
                subquery,
                document_ids=document_ids,
                top_k=max(top_k, 8),
                events=step_events,
            )
            all_results.extend(results)
            step_debug.append({"subquery": subquery, "retrieval": debug})
            if events:
                await events.emit(
                    event_type="agent.step.completed",
                    stage_id=step_id,
                    label=f"子问题 {index}",
                    status="success" if results else "warning",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    component="agent",
                    summary=f"获得 {len(results)} 个候选证据",
                    output={
                        "subquery": subquery,
                        "candidate_count": len(results),
                        "chunk_ids": [item.chunk.id for item in results],
                    },
                )
        deduplicated: dict[str, RetrievedChunk] = {}
        for item in all_results:
            previous = deduplicated.get(item.chunk.id)
            if previous is None or item.score > previous.score:
                deduplicated[item.chunk.id] = item
        expected_documents = set(document_ids or [])
        ranked = sorted(deduplicated.values(), key=lambda item: item.score, reverse=True)
        context_top_k = self._context_budget(
            plan,
            requested_top_k=top_k,
            document_count=len(expected_documents),
        )
        selected = self._select_with_document_coverage(
            ranked,
            top_k=context_top_k,
            expected_documents=expected_documents,
        )
        covered_documents = {item.chunk.document_id for item in selected}
        missing_documents = sorted(expected_documents - covered_documents)
        sufficient = len(selected) >= min(3, context_top_k) and not (
            plan.intent == "comparison" and missing_documents
        )
        pipeline = PipelineTracer(events)
        async with pipeline.stage(
            "agent.evidence_gap",
            "证据缺口检测",
            component="agent.evidence_validator",
            input={
                "candidate_count": len(selected),
                "requested_top_k": top_k,
                "adaptive_context_top_k": context_top_k,
            },
        ) as stage:
            stage.complete(
                output={
                    "sufficient": sufficient,
                    "covered_document_ids": sorted(covered_documents),
                    "missing_document_ids": missing_documents,
                    "adaptive_context_top_k": context_top_k,
                },
                status="success" if sufficient else "warning",
                summary=("证据覆盖满足回答条件" if sufficient else "证据不足，将明确限制回答范围"),
            )
        if not selected:
            return Answer(
                answer="当前论文库中没有检索到足以回答该问题的证据。",
                citations=[],
                claims=[],
                trace_id=trace_id or uuid.uuid4().hex,
                retrieval_debug={
                    "agent_plan": plan.model_dump(mode="json"),
                    "agent_steps": step_debug,
                    "agent_stop_reason": "insufficient_evidence",
                },
            )
        return await self._synthesize(
            question,
            selected,
            plan=plan,
            sufficient=sufficient,
            step_debug=step_debug,
            events=events,
            trace_id=trace_id,
        )

    @staticmethod
    def _context_budget(
        plan: AgentPlan,
        *,
        requested_top_k: int,
        document_count: int,
    ) -> int:
        if plan.intent == "comparison" and document_count:
            return min(16, max(requested_top_k, document_count * 3))
        return requested_top_k

    @staticmethod
    def _select_with_document_coverage(
        ranked: list[RetrievedChunk],
        *,
        top_k: int,
        expected_documents: set[str],
    ) -> list[RetrievedChunk]:
        """Reserve one context slot per requested paper before diversity filling."""
        if not ranked or top_k <= 0:
            return []
        ranked_document_ids = list(dict.fromkeys(item.chunk.document_id for item in ranked))
        required = [
            document_id for document_id in ranked_document_ids if document_id in expected_documents
        ]
        covered: list[RetrievedChunk] = []
        seen_chunks: set[str] = set()
        for document_id in required[:top_k]:
            candidate = next(
                (item for item in ranked if item.chunk.document_id == document_id),
                None,
            )
            if candidate is not None:
                covered.append(candidate)
                seen_chunks.add(candidate.chunk.id)
        remaining = [item for item in ranked if item.chunk.id not in seen_chunks]
        filled = RetrievalService._diversify(
            remaining,
            top_k=max(0, top_k - len(covered)),
        )
        return covered + filled

    async def _synthesize(
        self,
        question: str,
        retrieved: list[RetrievedChunk],
        *,
        plan: AgentPlan,
        sufficient: bool,
        step_debug: list[dict[str, Any]],
        events: EventSink | None,
        trace_id: str | None,
    ) -> Answer:
        contexts = "\n\n".join(
            RAGService._format_context(item, index) for index, item in enumerate(retrieved, start=1)
        )[: self.settings.agent_max_context_chars]
        pipeline = PipelineTracer(events)
        provider = SiliconFlowClient(self.settings)
        try:
            async with pipeline.stage(
                "agent.synthesize",
                "跨步骤证据综合与回答",
                component="siliconflow",
                model=self.settings.chat_model,
                input={"evidence_count": len(retrieved), "context_chars": len(contexts)},
            ) as stage:
                limitation = (
                    "证据缺口检测未完全通过，必须明确指出无法覆盖的范围。" if not sufficient else ""
                )
                prompt_text = (
                    f"问题：{question}\n\n{limitation}\n"
                    f"请综合多步检索证据回答，不要输出规划过程。\n\n证据：\n{contexts}"
                )
                reports = load_quality_reports(
                    Catalog(self.settings),
                    {item.chunk.document_id for item in retrieved},
                )
                routing = merge_routing_decisions(
                    [visual_routing_policy(report) for report in reports]
                )
                visual = build_multimodal_content(
                    prompt_text,
                    retrieved,
                    self.settings,
                    force=routing.force_visual,
                )
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": visual.parts},
                ]
                if events:
                    parts: list[str] = []
                    pending_delta = ""
                    last_emit = time.perf_counter()
                    async with provider.chat_stream(messages) as stream:
                        async for chunk in stream.chunks():
                            choices = chunk.get("choices") or []
                            delta = choices[0].get("delta", {}).get("content") if choices else None
                            if delta:
                                parts.append(str(delta))
                                pending_delta += str(delta)
                                now = time.perf_counter()
                                if len(pending_delta) < 80 and now - last_emit < 0.12:
                                    continue
                                await events.emit(
                                    event_type="generation.delta",
                                    stage_id="agent.synthesize",
                                    label="跨步骤证据综合与回答",
                                    status="running",
                                    component="siliconflow",
                                    model=self.settings.chat_model,
                                    output={
                                        "text_delta": pending_delta,
                                        "answer_chars": sum(map(len, parts)),
                                    },
                                )
                                pending_delta = ""
                                last_emit = now
                        if pending_delta:
                            await events.emit(
                                event_type="generation.delta",
                                stage_id="agent.synthesize",
                                label="跨步骤证据综合与回答",
                                status="running",
                                component="siliconflow",
                                model=self.settings.chat_model,
                                output={
                                    "text_delta": pending_delta,
                                    "answer_chars": sum(map(len, parts)),
                                },
                            )
                        usage = stream.usage
                        provider_trace_id = stream.trace_id
                    answer_text = "".join(parts)
                else:
                    response = await provider.chat(messages)
                    answer_text = response.data["choices"][0]["message"]["content"]
                    usage = response.data.get("usage") or {}
                    provider_trace_id = response.trace_id
                stage.complete(
                    output={
                        "answer_chars": len(answer_text),
                        "usage": usage,
                        "visual_images_included": visual.images_included,
                        "visual_images_skipped": visual.images_skipped,
                        "visual_force": routing.force_visual,
                        "visual_routing_reasons": routing.reasons,
                    },
                    trace={"provider_trace_id": provider_trace_id},
                    summary=f"综合生成 {len(answer_text)} 字符",
                )
        finally:
            await provider.close()
        referenced = sorted(
            {
                int(value)
                for value in re.findall(r"\[C(\d+)\]", answer_text)
                if 1 <= int(value) <= len(retrieved)
            }
        )
        citations = [
            RAGService._citation(retrieved[index - 1], index, catalog=Catalog(self.settings))
            for index in referenced
        ]
        claims = RAGService._claims(answer_text, retrieved)
        if events:
            await events.emit(
                event_type="agent.stopped",
                stage_id="agent.stop",
                label="Agent 停止",
                status="success" if sufficient else "warning",
                component="agent",
                summary=("完成计划且证据充分" if sufficient else "达到步骤预算，保留证据不足声明"),
                output={
                    "stop_reason": "plan_complete" if sufficient else "budget_or_evidence_gap",
                    "steps": len(step_debug),
                    "citation_count": len(citations),
                },
            )
        return Answer(
            answer=answer_text,
            citations=citations,
            claims=claims,
            trace_id=trace_id or uuid.uuid4().hex,
            retrieval_debug={
                "retrieved_chunk_ids": [item.chunk.id for item in retrieved],
                "agent_plan": plan.model_dump(mode="json"),
                "agent_steps": step_debug,
                "agent_stop_reason": "plan_complete" if sufficient else "budget_or_evidence_gap",
                "generation_trace_id": provider_trace_id,
                "generation_usage": usage,
            },
        )

    @staticmethod
    def plan(question: str, *, max_steps: int = 4) -> AgentPlan:
        lowered = question.casefold()
        comparison = bool(re.search(r"比较|对比|差异|共同|versus|\bvs\.?\b|compare", lowered))
        formula = bool(re.search(r"公式|推导|符号|equation|formula|derive", lowered))
        visual = bool(re.search(r"图\s*\d|表\s*\d|图表|曲线|figure|table|chart", lowered))
        method = bool(re.search(r"方法|模型|架构|算法|method|model|architecture", lowered))
        conjunctions = len(re.findall(r"以及|并且|同时|和|与|and|;|；", lowered))
        complex_question = comparison or conjunctions >= 2 or len(question) > 120
        if comparison:
            intent = "comparison"
            subqueries = [
                question,
                "分别概括各论文的方法、核心假设与关键模块",
                "分别列出各论文的实验设置、数据集、指标与主要结果",
                "比较各方法的限制、消融证据与适用条件",
            ]
        elif formula:
            intent = "formula"
            subqueries = [question, "定位相关公式、符号定义和前后假设", "说明公式的用途与实验验证"]
        elif visual:
            intent = "visual"
            subqueries = [question, "定位相关图表及其图注", "查找正文对该图表趋势和数值的解释"]
        elif method:
            intent = "method"
            subqueries = [question, "定位方法总览、关键模块与训练目标"]
        elif complex_question:
            intent = "synthesis"
            subqueries = [question, "查找支撑结论的方法证据", "查找对应实验结果和限制"]
        else:
            return AgentPlan(
                mode="controlled_rag",
                intent="fact",
                difficulty="simple",
                subqueries=[question],
                tools=["dense", "lexical", "visual", "rerank"],
                max_steps=max_steps,
                reason="单一事实问题无需多步规划",
            )
        return AgentPlan(
            mode="multi_step",
            intent=intent,
            difficulty="complex",
            subqueries=subqueries[:max_steps],
            tools=["dense", "lexical", "visual", "metadata", "rerank"],
            max_steps=max_steps,
            reason="问题需要跨证据类型或跨论文综合",
        )
