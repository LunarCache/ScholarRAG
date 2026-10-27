from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, Field

from scholarrag.authorization import check_active_authorization, current_guard
from scholarrag.config import Settings
from scholarrag.domain.access import AccessDenied
from scholarrag.evaluation.transport import AuthorizedJudgeTransport
from scholarrag.rag.runs import LeaseLost

if TYPE_CHECKING:
    from scholarrag.evaluation.jobs import EvaluationMetricJournal
from scholarrag.domain.models import Answer
from scholarrag.evaluation.visual_metrics import extract_visual_usage
from scholarrag.rag.runs import EventSink, PipelineTracer, QueryRunStore
from scholarrag.rag.service import RAGService
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog


class EvaluationCase(BaseModel):
    case_id: str = Field(default_factory=lambda: f"case_{uuid.uuid4().hex}")
    question: str = Field(min_length=1)
    document_ids: list[str] | None = None
    reference: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_chunk_ids: list[str] = Field(default_factory=list)
    source_block_ids: list[str] = Field(default_factory=list)
    review_status: Literal["candidate", "synthetic", "reviewed", "rejected"] = "reviewed"
    generator: dict[str, Any] = Field(default_factory=dict)


class MetricScore(BaseModel):
    value: float | bool | str | None
    reason: str | None = None
    elapsed_seconds: float | None = None


class EvaluationRun(BaseModel):
    run_id: str
    created_at: datetime
    case: EvaluationCase
    answer: str
    answer_trace_id: str
    retrieved_chunk_ids: list[str]
    retrieved_contexts: list[str]
    metrics: dict[str, MetricScore]
    evaluation_config: dict[str, Any] = Field(default_factory=dict)
    artifact_path: str
    parent_query_run_id: str | None = None


class RagasEvaluator:
    def __init__(self, settings: Settings) -> None:
        if settings.siliconflow_api_key is None:
            raise RuntimeError("SILICONFLOW_API_KEY is required for Ragas evaluation")
        self.settings = settings
        self.catalog = Catalog(settings)
        self.store = ArtifactStore(settings)
        self.checkpoints: EvaluationMetricJournal | None = None

    async def evaluate_rag(
        self,
        case: EvaluationCase,
        *,
        events: EventSink | None = None,
    ) -> EvaluationRun:
        answer = await RAGService(self.settings).answer(
            case.question, document_ids=case.document_ids
        )
        return await self.evaluate_answer(case=case, answer=answer, events=events)

    async def evaluate_answer(
        self,
        *,
        case: EvaluationCase,
        answer: Answer,
        parent_query_run_id: str | None = None,
        events: EventSink | None = None,
    ) -> EvaluationRun:
        guard = current_guard(self.settings)
        chunk_ids = list(answer.retrieval_debug.get("retrieved_chunk_ids", []))
        chunks = self.catalog.chunks_by_ids(chunk_ids)
        if guard is not None and (
            case.document_ids != list(guard.document_ids)
            or any(
                chunk_id not in chunks or chunks[chunk_id].document_id not in guard.document_ids
                for chunk_id in chunk_ids
            )
            or any(citation.document_id not in guard.document_ids for citation in answer.citations)
        ):
            raise AccessDenied("Evaluation evidence outside authorized scope")
        contexts = [chunks[chunk_id].text for chunk_id in chunk_ids if chunk_id in chunks]
        metrics = await self.evaluate_response(
            question=case.question,
            response=answer.answer,
            contexts=contexts,
            reference=case.reference,
            events=events,
        )
        check_active_authorization()
        metrics.update(
            self._retrieval_metrics(
                retrieved_chunk_ids=chunk_ids,
                expected_chunk_ids=case.source_chunk_ids,
                answer=answer,
            )
        )
        metrics.update(self._visual_metrics(parent_query_run_id))
        run_id = f"eval_{uuid.uuid4().hex}"
        root = (
            self.settings.data_dir
            if guard is None
            else self.settings.data_dir / "workspaces" / guard.context.workspace_id
        )
        artifact = root / "evaluations" / f"{run_id}.json"
        run = EvaluationRun(
            run_id=run_id,
            created_at=datetime.now(UTC),
            case=case,
            answer=answer.answer,
            answer_trace_id=answer.trace_id,
            retrieved_chunk_ids=chunk_ids,
            retrieved_contexts=contexts,
            metrics=metrics,
            evaluation_config={
                "judge_model": self.settings.ragas_judge_model,
                "judge_context_window_tokens": (self.settings.ragas_judge_context_window_tokens),
                "judge_output_limit": None,
                "judge_output_limit_source": "unset_in_client_request",
                "judge_enable_thinking": self.settings.ragas_judge_enable_thinking,
                "embedding_model": self.settings.unified_embedding_model,
                "siliconflow_base_url": self.settings.siliconflow_base_url,
            },
            artifact_path=str(artifact),
            parent_query_run_id=parent_query_run_id,
        )
        self.store.write_json(artifact, run.model_dump(mode="json"))
        return run

    async def evaluate_response(
        self,
        *,
        question: str,
        response: str,
        contexts: list[str],
        reference: str | None = None,
        events: EventSink | None = None,
    ) -> dict[str, MetricScore]:
        current_guard(self.settings)
        if self.checkpoints:
            self.checkpoints.initialize()
        try:
            from openai import AsyncOpenAI
            from ragas.embeddings import OpenAIEmbeddings
            from ragas.llms import llm_factory
            from ragas.metrics.collections import (
                AnswerRelevancy,
                ContextPrecision,
                ContextRecall,
                FactualCorrectness,
                Faithfulness,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Install evaluation dependencies with `uv sync --extra eval`"
            ) from exc

        client = AsyncOpenAI(
            api_key=self.settings.siliconflow_api_key.get_secret_value(),
            base_url=self.settings.siliconflow_base_url,
            timeout=180.0,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=AuthorizedJudgeTransport(self.settings)),
        )
        try:
            llm = self._remove_judge_output_limits(
                llm_factory(
                    self.settings.ragas_judge_model,
                    client=client,
                    temperature=self.settings.ragas_judge_temperature,
                    extra_body={
                        "enable_thinking": self.settings.ragas_judge_enable_thinking,
                    },
                )
            )
            embeddings = OpenAIEmbeddings(
                client=client,
                model=self.settings.unified_embedding_model,
            )
            response_without_citations = re.sub(r"\[C\d+\]", "", response)
            tracer = PipelineTracer(events)
            metrics: dict[str, MetricScore] = {}
            async with tracer.stage(
                "evaluation.citation_coverage",
                "评估 Citation Coverage",
                parent_stage_id="evaluation.ragas",
                component="scholarrag.evaluation",
                input={"response_chars": len(response)},
            ) as stage:
                citation_score = self._citation_coverage(response)
                metrics["citation_coverage"] = MetricScore(
                    value=citation_score, elapsed_seconds=0.0
                )
                stage.complete(
                    output={"metric": "citation_coverage", "score": citation_score},
                    summary=f"citation_coverage = {citation_score:.3f}",
                )
            scorers: list[tuple[str, Any, dict[str, Any]]] = [
                (
                    "faithfulness",
                    Faithfulness(llm=llm),
                    {
                        "user_input": question,
                        "response": response_without_citations,
                        "retrieved_contexts": contexts,
                    },
                ),
                (
                    "answer_relevancy",
                    AnswerRelevancy(llm=llm, embeddings=embeddings, strictness=1),
                    {"user_input": question, "response": response_without_citations},
                ),
            ]
            if reference:
                scorers.extend(
                    [
                        (
                            "context_precision",
                            ContextPrecision(llm=llm),
                            {
                                "user_input": question,
                                "reference": reference,
                                "retrieved_contexts": contexts,
                            },
                        ),
                        (
                            "context_recall",
                            ContextRecall(llm=llm),
                            {
                                "user_input": question,
                                "reference": reference,
                                "retrieved_contexts": contexts,
                            },
                        ),
                        (
                            "factual_correctness",
                            FactualCorrectness(llm=llm),
                            {"response": response_without_citations, "reference": reference},
                        ),
                    ]
                )
            for name, scorer, kwargs in scorers:
                metrics[name] = await self._score_metric(
                    name,
                    lambda scorer=scorer, kwargs=kwargs: scorer.ascore(**kwargs),
                    tracer=tracer,
                    input={"context_count": len(contexts), "has_reference": reference is not None},
                )
        finally:
            await client.close()
        return metrics

    async def _score_metric(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        tracer: PipelineTracer,
        input: dict,
    ) -> MetricScore:
        from scholarrag.evaluation.jobs import EvaluationUncertain

        check_active_authorization()
        if self.checkpoints:
            saved = self.checkpoints.get(name)
            if saved is not None:
                if tracer.sink:
                    await tracer.sink.emit(
                        event_type="evaluation.metric.restored",
                        stage_id=f"evaluation.{name}",
                        label=f"复用已完成指标 {name}",
                        status="warning" if saved.value is None else "success",
                        summary="从指标检查点恢复，未再次调用 Judge",
                        output={"metric": name, "score": saved.value, "checkpoint_reused": True},
                    )
                return saved
            self.checkpoints.begin(name)
        start = time.perf_counter()
        async with tracer.stage(
            f"evaluation.{name}",
            f"评估 {name}",
            parent_stage_id="evaluation.ragas",
            component="ragas",
            model=self.settings.ragas_judge_model,
            input=input,
        ) as stage:
            try:
                result = await operation()
                check_active_authorization()
                value = (
                    float(result.value) if isinstance(result.value, int | float) else result.value
                )
                score = MetricScore(
                    value=value,
                    reason=result.reason,
                    elapsed_seconds=round(time.perf_counter() - start, 3),
                )
                stage.complete(output={"metric": name, "score": value}, summary=f"{name} = {value}")
            except (AccessDenied, LeaseLost, EvaluationUncertain):
                raise
            except Exception as exc:  # noqa: BLE001 - preserve independent metric failures without exposing provider data
                check_active_authorization()
                reason = (
                    "Judge metric failed"
                    if current_guard(self.settings) is not None
                    else f"{type(exc).__name__}: {exc}"[:1000]
                )
                score = MetricScore(
                    value=None, reason=reason, elapsed_seconds=round(time.perf_counter() - start, 3)
                )
                stage.complete(
                    output={"metric": name, "score": None},
                    summary=f"{name} 评估失败，其他指标继续执行",
                    status="warning",
                    trace={"error": reason},
                )
            if self.checkpoints:
                self.checkpoints.finish(name, score)
            return score

    @staticmethod
    def _remove_judge_output_limits(llm: Any) -> Any:
        """Remove output caps injected by Ragas before any provider request is made."""
        model_args = getattr(llm, "model_args", None)
        if not isinstance(model_args, dict):
            raise TypeError(
                "The active Ragas LLM adapter does not expose mutable model_args; "
                "cannot guarantee an uncapped judge request."
            )
        limit_keys = ("max_tokens", "max_completion_tokens", "max_output_tokens")
        for key in limit_keys:
            model_args.pop(key, None)

        map_provider_params = getattr(llm, "_map_provider_params", None)
        if callable(map_provider_params):
            mapped_args = map_provider_params()
            leaked = {key for key in limit_keys if key in mapped_args}
            if leaked:
                raise RuntimeError(
                    "Ragas reintroduced judge output limits: " + ", ".join(sorted(leaked))
                )
        return llm

    @staticmethod
    def _citation_coverage(response: str) -> float:
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[。！？.!?])\s+|\n+", response)
            if len(sentence.strip()) >= 12
        ]
        if not sentences:
            return 0.0
        cited = sum(bool(re.search(r"\[C\d+\]", sentence)) for sentence in sentences)
        return cited / len(sentences)

    @staticmethod
    def _retrieval_metrics(
        *,
        retrieved_chunk_ids: list[str],
        expected_chunk_ids: list[str],
        answer: Answer,
    ) -> dict[str, MetricScore]:
        metrics: dict[str, MetricScore] = {}
        if expected_chunk_ids:
            expected = set(expected_chunk_ids)
            hits = [chunk_id for chunk_id in retrieved_chunk_ids if chunk_id in expected]
            recall = len(set(hits)) / len(expected)
            first_rank = next(
                (
                    rank
                    for rank, chunk_id in enumerate(retrieved_chunk_ids, start=1)
                    if chunk_id in expected
                ),
                None,
            )
            dcg = sum(
                1.0 / math.log2(rank + 1)
                for rank, chunk_id in enumerate(retrieved_chunk_ids, start=1)
                if chunk_id in expected
            )
            ideal_hits = min(len(expected), len(retrieved_chunk_ids))
            idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
            metrics["retrieval_recall_at_k"] = MetricScore(value=recall)
            metrics["retrieval_mrr"] = MetricScore(value=(1.0 / first_rank if first_rank else 0.0))
            metrics["retrieval_ndcg_at_k"] = MetricScore(value=(dcg / idcg if idcg else 0.0))
        cited_ids = {citation.citation_id for citation in answer.citations}
        referenced_ids = set(re.findall(r"\[(C\d+)\]", answer.answer))
        metrics["citation_validity"] = MetricScore(
            value=(len(cited_ids & referenced_ids) / len(referenced_ids) if referenced_ids else 0.0)
        )
        factual_claims = [claim for claim in answer.claims if len(claim.text) >= 12]
        metrics["claim_citation_completeness"] = MetricScore(
            value=(
                sum(bool(claim.citation_ids) for claim in factual_claims) / len(factual_claims)
                if factual_claims
                else 0.0
            )
        )
        metrics["claim_evidence_span_rate"] = MetricScore(
            value=(
                sum(bool(claim.evidence_spans) for claim in factual_claims) / len(factual_claims)
                if factual_claims
                else 0.0
            )
        )
        return metrics

    def _visual_metrics(self, run_id: str | None) -> dict[str, MetricScore]:
        """Visual usage counters from the query run that produced this answer.

        Answers generated without a recorded query run (e.g. CLI evaluation)
        have no visual metrics to report.
        """
        guard = current_guard(self.settings)
        store = QueryRunStore(
            self.settings, workspace_id=guard.context.workspace_id if guard else None
        )
        run = store.get(run_id) if run_id else None
        if run is None:
            return {}
        events = [event.model_dump() for event in store.list_events(run.run_id)]
        usage = extract_visual_usage(events)
        return {
            "visual_images_included": MetricScore(
                value=usage.included, reason="生成阶段实际携带的图片数"
            ),
            "visual_images_skipped": MetricScore(
                value=usage.skipped, reason="因图片加载失败跳过的图片数"
            ),
        }


def load_cases(path: Path) -> list[EvaluationCase]:
    if path.suffix.lower() == ".jsonl":
        return [
            EvaluationCase.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [EvaluationCase.model_validate(item) for item in payload]
