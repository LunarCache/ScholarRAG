"""Tool-calling agentic RAG loop.

The planner is replaced by OpenAI-compatible function calling: the chat
model receives tool schemas (paper discovery, hybrid retrieval, chunk
reading) and decides the search order itself. Every tool call is streamed
as trace events (``agent.tool_call.started`` / ``agent.tool_call.completed``),
thinking deltas are streamed as ``agent.thinking.delta``, and the final
answer is synthesized either from the model's own draft or, when visual
evidence is present, through the same multimodal generation path as the
controlled RAG workflow.

On timeout or provider failure the service degrades to the controlled
single-shot RAG so the API never returns an empty answer.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

from scholarrag.config import Settings
from scholarrag.domain.models import Answer
from scholarrag.providers.siliconflow import SiliconFlowClient
from scholarrag.rag.runs import EventSink, PipelineTracer
from scholarrag.rag.service import SYSTEM_PROMPT, RAGService
from scholarrag.rag.tools import EvidenceRegistry, ToolRuntime, ToolSpec, build_tool_specs
from scholarrag.rag.visual_routing import (
    load_quality_reports,
    merge_routing_decisions,
    visual_routing_policy,
)
from scholarrag.rag.visuals import build_multimodal_content
from scholarrag.storage.catalog import Catalog

TOOL_SYSTEM_PROMPT = f"""{SYSTEM_PROMPT}

你是 ScholarRAG 检索代理：先通过工具收集证据，再直接输出最终回答。
工作流程：
1. 不确定论文范围时，先用 list_papers / get_paper 确认可检索的论文。
2. 用 retrieve 检索证据；检索预览不够精确（公式、数值、图表）时，用 read_chunk 读取完整正文。
3. 证据不足或覆盖不完整时，改写查询再次检索，直到证据充分或达到工具调用预算。
4. 证据充分后直接输出最终回答，不要再调用任何工具。
引用要求：
- 用工具结果中的证据编号标注事实，格式 [C#] 或 [C1][C3]。
- 只能引用 retrieve / read_chunk 返回的编号，不要自行编造编号。
- 证据不足时明确说明无法覆盖的范围。"""


class ToolAgentService:
    """Autonomous tool-loop agent with controlled-RAG fallback."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.controlled = RAGService(settings)

    async def answer(
        self,
        question: str,
        *,
        document_ids: list[str] | None = None,
        top_k: int = 8,
        events: EventSink | None = None,
        trace_id: str | None = None,
    ) -> Answer:
        specs = build_tool_specs()
        registry = EvidenceRegistry()
        runtime = ToolRuntime(
            self.settings,
            registry,
            document_ids=document_ids,
            top_k=top_k,
        )
        pipeline = PipelineTracer(events)
        steps: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        provider_trace_id: str | None = None
        try:
            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                async with pipeline.stage(
                    "agent.tool.loop",
                    "工具调用循环",
                    component="agent.tool_executor",
                    input={
                        "question_chars": len(question),
                        "document_count": len(document_ids or []),
                        "tools": [spec.name for spec in specs],
                        "max_calls": self.settings.agent_tool_max_calls,
                    },
                ) as stage:
                    draft, steps, usage, provider_trace_id, stop_reason = await self._tool_loop(
                        question,
                        specs=specs,
                        runtime=runtime,
                        events=events,
                    )
                    stage.complete(
                        output={
                            "tool_calls": len(steps),
                            "evidence_count": len(registry),
                            "draft_chars": len(draft),
                            "usage": usage,
                        },
                        trace={"provider_trace_id": provider_trace_id},
                        summary=f"{len(steps)} 次工具调用，{len(registry)} 条证据",
                    )
        except TimeoutError:
            if events:
                await events.emit(
                    event_type="agent.timeout",
                    stage_id="agent.stop",
                    label="Agent 达到超时预算",
                    status="warning",
                    component="agent",
                    summary="已停止工具循环，使用已收集证据继续",
                    output={"timeout_seconds": self.settings.agent_timeout_seconds},
                )
            if registry.pool:
                return await self._finalize(
                    question,
                    draft="",
                    registry=registry,
                    steps=_steps_debug(steps),
                    events=events,
                    trace_id=trace_id,
                    stop_reason="timeout_with_evidence",
                    usage=usage,
                    provider_trace_id=provider_trace_id,
                )
            return await self._fallback(question, document_ids, top_k, events, trace_id, "timeout_fallback")
        except Exception as exc:  # noqa: BLE001 - degrade instead of failing the run
            if events:
                await events.emit(
                    event_type="agent.error",
                    stage_id="agent.stop",
                    label="工具调用循环失败",
                    status="warning",
                    component="agent",
                    summary="已降级到受控 RAG",
                    error=f"{type(exc).__name__}: {exc}",
                )
            return await self._fallback(
                question, document_ids, top_k, events, trace_id, "fallback_controlled_rag"
            )

        if registry.pool:
            return await self._finalize(
                question,
                draft=draft,
                registry=registry,
                steps=_steps_debug(steps),
                events=events,
                trace_id=trace_id,
                stop_reason=stop_reason,
                usage=usage,
                provider_trace_id=provider_trace_id,
            )
        return Answer(
            answer="当前论文库中没有检索到足以回答该问题的证据。",
            citations=[],
            claims=[],
            trace_id=trace_id or uuid.uuid4().hex,
            retrieval_debug={
                "agent_plan": {
                    "mode": "tool_calling",
                    "tools": [spec.name for spec in specs],
                    "max_calls": self.settings.agent_tool_max_calls,
                },
                "agent_steps": _steps_debug(steps),
                "agent_stop_reason": "insufficient_evidence",
            },
        )

    async def _tool_loop(
        self,
        question: str,
        *,
        specs: list[ToolSpec],
        runtime: ToolRuntime,
        events: EventSink | None,
    ) -> tuple[
        str,
        list[dict[str, Any]],
        dict[str, Any],
        str | None,
        str,
    ]:
        by_name = {spec.name: spec for spec in specs}
        tool_schemas = [spec.openai_schema() for spec in specs]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": TOOL_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        provider = SiliconFlowClient(self.settings)
        steps: list[dict[str, Any]] = []
        total_usage: dict[str, int] = {}
        provider_trace_id: str | None = None
        try:
            while len(steps) < self.settings.agent_tool_max_calls:
                (
                    content,
                    tool_calls,
                    usage,
                    provider_trace_id,
                ) = await self._chat_with_tools(
                    provider,
                    messages,
                    tool_schemas,
                    events=events,
                )
                for key, value in usage.items():
                    total_usage[key] = total_usage.get(key, 0) + int(value)
                if not tool_calls:
                    return content, steps, total_usage, provider_trace_id, "tool_loop_complete"
                assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_message["tool_calls"] = [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                        for call in tool_calls
                    ]
                messages.append(assistant_message)
                started_at = time.perf_counter()
                for index, call in enumerate(tool_calls):
                    call_stage_id = f"agent.tool.{call['name']}.{len(steps) + index + 1}"
                    if events:
                        await events.emit(
                            event_type="agent.tool_call.started",
                            stage_id=call_stage_id,
                            label=f"调用工具 {call['name']}",
                            status="running",
                            component="agent.tool_executor",
                            input={
                                "call_id": call["id"],
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        )
                results = await asyncio.gather(
                    *(
                        self._execute_tool(specs=by_name, name=call["name"], args=call["arguments"], runtime=runtime)
                        for call in tool_calls
                    )
                )
                completed_steps: list[dict[str, Any]] = []
                for index, (call, (result, error)) in enumerate(
                    zip(tool_calls, results, strict=True)
                ):
                    duration_ms = (time.perf_counter() - started_at) * 1000
                    call_stage_id = f"agent.tool.{call['name']}.{len(steps) + index + 1}"
                    if error:
                        step = {
                            "call_id": call["id"],
                            "name": call["name"],
                            "arguments": call["arguments"],
                            "result_summary": f"error: {error}",
                            "result_chars": 0,
                            "duration_ms": duration_ms,
                        }
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": json.dumps({"error": error}, ensure_ascii=False),
                            }
                        )
                        if events:
                            await events.emit(
                                event_type="agent.tool_call.completed",
                                stage_id=call_stage_id,
                                label=f"调用工具 {call['name']}",
                                status="failed",
                                duration_ms=duration_ms,
                                component="agent.tool_executor",
                                summary=f"工具 {call['name']} 执行失败",
                                output={
                                    "call_id": call["id"],
                                    "name": call["name"],
                                    "error": error,
                                },
                                error=error,
                            )
                    else:
                        result_text = json.dumps(result, ensure_ascii=False)
                        step = {
                            "call_id": call["id"],
                            "name": call["name"],
                            "arguments": call["arguments"],
                            "result_summary": _result_summary(result),
                            "result_chars": len(result_text),
                            "duration_ms": duration_ms,
                        }
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": result_text,
                            }
                        )
                        if events:
                            await events.emit(
                                event_type="agent.tool_call.completed",
                                stage_id=call_stage_id,
                                label=f"调用工具 {call['name']}",
                                status="success",
                                duration_ms=duration_ms,
                                component="agent.tool_executor",
                                summary=f"返回 {_result_summary(result)}",
                                output={
                                    "call_id": call["id"],
                                    "name": call["name"],
                                    "result_chars": len(result_text),
                                    "result_summary": step["result_summary"],
                                },
                            )
                    completed_steps.append(step)
                # Append after the completion loop so that started/completed
                # events of parallel calls share the same stage id sequence.
                steps.extend(completed_steps)
            return "", steps, total_usage, provider_trace_id, "tool_budget_exhausted"
        finally:
            await provider.close()

    async def _chat_with_tools(
        self,
        provider: SiliconFlowClient,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        *,
        events: EventSink | None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any], str | None]:
        """Stream one chat turn, accumulating content, thinking and tool calls."""
        accumulated: dict[int, dict[str, Any]] = {}
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        pending_thinking = ""
        last_emit = time.perf_counter()
        usage: dict[str, Any] = {}
        provider_trace_id: str | None = None
        async with provider.chat_stream(messages, tools=tool_schemas) as stream:
            async for chunk in stream.chunks():
                provider_trace_id = stream.trace_id or provider_trace_id
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    thinking_parts.append(str(reasoning))
                    pending_thinking += str(reasoning)
                    now = time.perf_counter()
                    if events and (
                        len(pending_thinking) >= 60 or now - last_emit >= 0.15
                    ):
                        await events.emit(
                            event_type="agent.thinking.delta",
                            stage_id="agent.think",
                            label="Agent 思考",
                            status="running",
                            component="siliconflow",
                            model=self.settings.chat_model,
                            output={
                                "text_delta": pending_thinking,
                                "thinking_chars": sum(map(len, thinking_parts)),
                            },
                        )
                        pending_thinking = ""
                        last_emit = now
                content = delta.get("content")
                if content:
                    content_parts.append(str(content))
                for tool_call in delta.get("tool_calls") or []:
                    index = int(tool_call.get("index") or 0)
                    bucket = accumulated.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if tool_call.get("id"):
                        bucket["id"] = str(tool_call["id"])
                    function = tool_call.get("function") or {}
                    if function.get("name"):
                        bucket["name"] = str(function["name"])
                    if function.get("arguments"):
                        bucket["arguments"] += str(function["arguments"])
        if events and pending_thinking:
            await events.emit(
                event_type="agent.thinking.delta",
                stage_id="agent.think",
                label="Agent 思考",
                status="running",
                component="siliconflow",
                model=self.settings.chat_model,
                output={
                    "text_delta": pending_thinking,
                    "thinking_chars": sum(map(len, thinking_parts)),
                },
            )
        tool_calls = [
            {"id": bucket["id"], "name": bucket["name"], "arguments": bucket["arguments"]}
            for _, bucket in sorted(accumulated.items())
            if bucket["name"]
        ]
        return "".join(content_parts), tool_calls, usage, provider_trace_id

    async def _execute_tool(
        self,
        *,
        specs: dict[str, ToolSpec],
        name: str,
        args: str,
        runtime: ToolRuntime,
    ) -> tuple[dict[str, Any] | None, str | None]:
        spec = specs.get(name)
        if spec is None:
            return None, f"未知工具 {name!r}"
        try:
            parsed = json.loads(args or "{}")
            if not isinstance(parsed, dict):
                return None, "工具参数必须是 JSON 对象"
        except json.JSONDecodeError as exc:
            return None, f"工具参数 JSON 解析失败: {exc}"
        try:
            result = await spec.handler(runtime, parsed)
            return result, None
        except Exception as exc:  # noqa: BLE001 - tool errors feed back to the model
            return None, f"{type(exc).__name__}: {exc}"

    async def _finalize(
        self,
        question: str,
        *,
        draft: str,
        registry: EvidenceRegistry,
        steps: list[dict[str, Any]],
        events: EventSink | None,
        trace_id: str | None,
        stop_reason: str,
        usage: dict[str, Any],
        provider_trace_id: str | None,
    ) -> Answer:
        retrieved = registry.pool
        contexts = "\n\n".join(
            RAGService._format_context(item, index)
            for index, item in enumerate(retrieved, start=1)
        )[: self.settings.agent_max_context_chars]
        reports_sources = {item.chunk.document_id for item in retrieved}
        has_visual = any(item.chunk.visual_evidence for item in retrieved)
        pipeline = PipelineTracer(events)
        needs_generation = (
            has_visual and self.settings.chat_include_visual_evidence
        ) or not draft.strip()
        if needs_generation:
            provider = SiliconFlowClient(self.settings)
            try:
                stage_label = "多模态证据综合与回答" if has_visual else "证据综合与回答"
                async with pipeline.stage(
                    "agent.synthesize",
                    stage_label,
                    component="siliconflow",
                    model=self.settings.chat_model,
                    input={"evidence_count": len(retrieved), "context_chars": len(contexts)},
                ) as stage:
                    if has_visual:
                        base = draft or (
                            f"请综合以下证据回答：{question}\n\n不要输出规划过程，直接给出最终回答。"
                        )
                        prompt_text = (
                            f"以下是检索代理的草稿回答，请结合附带的图表图片精修为最终回答，"
                            f"保留并修正 [C#] 引用编号，不要改变引用含义。\n\n草稿：\n{base}\n\n"
                            f"完整证据：\n{contexts}"
                        )
                    else:
                        prompt_text = (
                            f"问题：{question}\n\n"
                            f"检索代理已达到工具调用预算，请基于已有证据直接给出最终回答，"
                            f"不要输出规划过程。证据不足时明确说明。\n\n证据：\n{contexts}"
                        )
                    reports = load_quality_reports(Catalog(self.settings), reports_sources)
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
                    answer_text = await _stream_generation(
                        provider,
                        messages,
                        events=events,
                        model=self.settings.chat_model,
                    )
                    final_usage = usage
                    final_trace_id = provider_trace_id
                    stage.complete(
                        output={
                            "answer_chars": len(answer_text),
                            "usage": final_usage,
                            "visual_images_included": visual.images_included,
                            "visual_images_skipped": visual.images_skipped,
                            "visual_force": routing.force_visual,
                        },
                        trace={"provider_trace_id": final_trace_id},
                        summary=f"{stage_label}生成 {len(answer_text)} 字符",
                    )
            finally:
                await provider.close()
        else:
            answer_text = draft
            final_usage = usage
            final_trace_id = provider_trace_id
            if events:
                await events.emit(
                    event_type="stage.completed",
                    stage_id="agent.synthesize",
                    label="证据综合与回答",
                    status="success",
                    component="agent",
                    duration_ms=0.0,
                    summary=f"工具循环直接给出草稿回答 · {len(answer_text)} 字符",
                    input={"evidence_count": len(retrieved), "context_chars": len(contexts)},
                    output={
                        "answer_chars": len(answer_text),
                        "usage": final_usage,
                        "visual_images_included": 0,
                        "visual_images_skipped": 0,
                        "visual_force": False,
                    },
                    trace={"provider_trace_id": final_trace_id},
                )
        if not answer_text.strip():
            answer_text = "当前论文库中没有检索到足以回答该问题的证据。"
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
                status="success",
                component="agent",
                summary=f"完成 {len(steps)} 次工具调用并生成回答",
                output={
                    "stop_reason": stop_reason,
                    "tool_calls": len(steps),
                    "evidence_count": len(retrieved),
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
                "agent_plan": {
                    "mode": "tool_calling",
                    "tools": [item["name"] for item in steps],
                    "max_calls": self.settings.agent_tool_max_calls,
                },
                "agent_steps": steps,
                "agent_stop_reason": stop_reason,
                "generation_trace_id": final_trace_id,
                "generation_usage": final_usage,
            },
        )

    async def _fallback(
        self,
        question: str,
        document_ids: list[str] | None,
        top_k: int,
        events: EventSink | None,
        trace_id: str | None,
        stop_reason: str,
    ) -> Answer:
        answer = await self.controlled.answer(
            question,
            document_ids=document_ids,
            top_k=top_k,
            events=events,
            trace_id=trace_id,
        )
        answer.retrieval_debug["agent_stop_reason"] = stop_reason
        return answer


def _steps_debug(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "call_id": step["call_id"],
            "name": step["name"],
            "arguments": step["arguments"],
            "result_summary": step["result_summary"],
        }
        for step in steps
    ]


def _result_summary(result: dict[str, Any]) -> str:
    if "error" in result:
        return f"错误：{result['error']}"
    if "papers" in result:
        return f"{result.get('total', len(result['papers']))} 篇论文"
    if "candidates" in result:
        return f"{result.get('candidate_count', 0)} 个候选证据"
    if "chunk" in result and result["chunk"] is None:
        return "未找到该 Chunk"
    if "text" in result:
        return f"Chunk 全文 {len(result['text'])} 字符，{len(result.get('visuals') or [])} 个图表"
    return f"{len(str(result))} 字符"


async def _stream_generation(
    provider: SiliconFlowClient,
    messages: list[dict[str, Any]],
    *,
    events: EventSink | None,
    model: str,
) -> str:
    """Stream a generation turn, emitting ``generation.delta`` events."""
    parts: list[str] = []
    pending_delta = ""
    last_emit = time.perf_counter()
    async with provider.chat_stream(messages) as stream:
        async for chunk in stream.chunks():
            choices = chunk.get("choices") or []
            delta = choices[0].get("delta", {}).get("content") if choices else None
            if not delta:
                continue
            parts.append(str(delta))
            pending_delta += str(delta)
            now = time.perf_counter()
            if events and (len(pending_delta) >= 80 or now - last_emit >= 0.12):
                await events.emit(
                    event_type="generation.delta",
                    stage_id="agent.synthesize",
                    label="多模态证据综合与回答",
                    status="running",
                    component="siliconflow",
                    model=model,
                    output={
                        "text_delta": pending_delta,
                        "answer_chars": sum(map(len, parts)),
                    },
                )
                pending_delta = ""
                last_emit = now
    if events and pending_delta:
        await events.emit(
            event_type="generation.delta",
            stage_id="agent.synthesize",
            label="多模态证据综合与回答",
            status="running",
            component="siliconflow",
            model=model,
            output={
                "text_delta": pending_delta,
                "answer_chars": sum(map(len, parts)),
            },
        )
    return "".join(parts)
