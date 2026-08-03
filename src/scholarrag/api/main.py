from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from scholarrag.api.schemas import (
    CollectionRequest,
    CollectionResponse,
    EvaluationJobResponse,
    EvaluationRequest,
    HumanActorRequest,
    JobStatus,
    JudgeRatingRequest,
    ParserAnnotationRequest,
    ParseReviewRequest,
    ParseReviewResponse,
    ParserReopenRequest,
    ParseVersionResponse,
    PersonalPaperStateRequest,
    PersonalPaperStateResponse,
    QAReviewRequest,
    QueryRequest,
    QueryResponse,
    QueryRunResponse,
    UploadAccepted,
)
from scholarrag.api.security import LocalSecurityMiddleware
from scholarrag.config import Settings, get_settings
from scholarrag.evaluation.dashboard import QualityDashboard
from scholarrag.evaluation.human_review import HumanReviewError, HumanReviewService
from scholarrag.evaluation.jobs import EvaluationJobStore
from scholarrag.evaluation.service import EvaluationCase, RagasEvaluator
from scholarrag.ingestion.review import ParseReviewError, ParseReviewService
from scholarrag.ingestion.service import IngestionService
from scholarrag.observability import configure_telemetry, get_tracer
from scholarrag.rag.agent import AgenticRAGService
from scholarrag.rag.runs import QueryRunStore, utc_now
from scholarrag.rag.service import RAGService
from scholarrag.retrieval.index import close_embedded_clients
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog
from scholarrag.storage.jobs import IngestionJobStore
from scholarrag.storage.library import PersonalLibraryStore, PersonalPaperState

UPLOAD_FILE = File(...)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.catalog = Catalog(settings)
        self.reviews = ParseReviewService(settings)
        self.query_runs = QueryRunStore(settings)
        self.query_tasks: dict[str, asyncio.Task[Any]] = {}
        self.evaluation_jobs = EvaluationJobStore(settings)
        self.evaluation_tasks: dict[str, asyncio.Task[Any]] = {}
        self.jobs = IngestionJobStore(settings)
        self.job_wakeup = asyncio.Event()
        self.library = PersonalLibraryStore(settings)
        self.tasks: set[asyncio.Task[Any]] = set()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_telemetry(settings)
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.query_runs.recover_interrupted()
        await runtime.query_runs.recover_interrupted_child_stages()
        await runtime.evaluation_jobs.recover_interrupted()
        runtime.jobs.recover_interrupted()
        worker = asyncio.create_task(_ingestion_worker(runtime))
        runtime.tasks.add(worker)
        runtime.job_wakeup.set()
        try:
            yield
        finally:
            for task in runtime.tasks:
                task.cancel()
            if runtime.tasks:
                await asyncio.gather(*runtime.tasks, return_exceptions=True)
            close_embedded_clients()

    app = FastAPI(title="ScholarRAG API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(LocalSecurityMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/health/ready")
    async def readiness() -> dict[str, Any]:
        try:
            await asyncio.to_thread(runtime.catalog.list_documents)
            qdrant_mode = "remote" if settings.qdrant_url else "embedded"
            return {"status": "ready", "sqlite": "ok", "qdrant_mode": qdrant_mode}
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "error": type(exc).__name__},
            ) from exc

    @app.get("/api/dashboard")
    async def dashboard() -> dict[str, Any]:
        return await asyncio.to_thread(QualityDashboard(settings).snapshot)

    @app.get("/api/reviews/summary")
    async def review_summary() -> dict[str, Any]:
        return await asyncio.to_thread(HumanReviewService(settings).summary)

    @app.get("/api/reviews/audit")
    async def review_audit(limit: int = 500) -> list[dict[str, Any]]:
        if not 1 <= limit <= 5000:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 5000")
        return await asyncio.to_thread(HumanReviewService(settings).audit_events, limit=limit)

    @app.get("/api/reviews/parser-gold")
    async def parser_gold_samples() -> list[dict[str, Any]]:
        return await asyncio.to_thread(HumanReviewService(settings).list_parser_samples)

    @app.get("/api/reviews/parser-gold/{sample_id}/image")
    async def parser_gold_image(sample_id: str) -> FileResponse:
        try:
            image = await asyncio.to_thread(HumanReviewService(settings).parser_image, sample_id)
        except HumanReviewError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(image, media_type="image/png")

    @app.put("/api/reviews/parser-gold/{sample_id}/annotation")
    async def annotate_parser_gold(
        sample_id: str, request: ParserAnnotationRequest
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                HumanReviewService(settings).annotate_parser,
                sample_id,
                actor=request.actor,
                elements=request.elements,
                qa_cases=request.qa_cases,
                notes=request.notes,
            )
        except HumanReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/reviews/parser-gold/{sample_id}/review")
    async def review_parser_gold(sample_id: str, request: HumanActorRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                HumanReviewService(settings).review_parser, sample_id, actor=request.actor
            )
        except HumanReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/reviews/parser-gold/{sample_id}/reopen")
    async def reopen_parser_gold(sample_id: str, request: ParserReopenRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                HumanReviewService(settings).reopen_parser,
                sample_id,
                actor=request.actor,
                reason=request.reason,
            )
        except HumanReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/reviews/qa")
    async def qa_review_cases() -> list[dict[str, Any]]:
        return await asyncio.to_thread(HumanReviewService(settings).qa_cases_with_evidence)

    @app.put("/api/reviews/qa/{case_id}")
    async def review_qa_case(case_id: str, request: QAReviewRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                HumanReviewService(settings).review_qa,
                case_id,
                actor=request.actor,
                decision=request.decision,
                question=request.question,
                reference=request.reference,
                tags=request.tags,
                source_chunk_ids=request.source_chunk_ids,
                source_block_ids=request.source_block_ids,
                note=request.note,
            )
        except HumanReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/reviews/judge/runs")
    async def judge_review_runs() -> list[dict[str, Any]]:
        return await asyncio.to_thread(HumanReviewService(settings).evaluation_runs)

    @app.post("/api/reviews/judge/{run_id}/ratings")
    async def submit_judge_rating(run_id: str, request: JudgeRatingRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                HumanReviewService(settings).submit_rating,
                run_id,
                rater=request.rater,
                faithfulness=request.faithfulness,
                answer_relevancy=request.answer_relevancy,
                citation_correctness=request.citation_correctness,
                notes=request.notes,
            )
        except HumanReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/documents")
    async def list_documents() -> list[dict[str, Any]]:
        documents = runtime.catalog.list_documents()
        for document in documents:
            manifest_path = Path(document["artifact_dir"]) / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                document["page_count"] = manifest.get("page_count")
                document["quality"] = manifest.get("quality")
                document["routing"] = manifest.get("routing")
        return documents

    @app.post("/api/documents", response_model=UploadAccepted, status_code=202)
    async def upload_document(file: UploadFile = UPLOAD_FILE) -> UploadAccepted:
        filename = _safe_filename(file.filename or "paper.pdf")
        if Path(filename).suffix.lower() != ".pdf":
            raise HTTPException(status_code=415, detail="Only PDF uploads are supported")
        upload_dir = settings.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / f"{uuid.uuid4().hex}-{filename}"
        async with await anyio.open_file(target, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                await output.write(chunk)
        await file.close()
        source_hash = await asyncio.to_thread(ArtifactStore.sha256, target)
        job = runtime.jobs.enqueue(
            filename=filename,
            source_path=target,
            idempotency_key=f"sha256:{source_hash}",
        )
        if Path(job.source_path) != target:
            target.unlink(missing_ok=True)
        runtime.job_wakeup.set()
        return UploadAccepted(job_id=job.job_id)

    @app.get("/api/jobs/{job_id}", response_model=JobStatus)
    async def get_job(job_id: str) -> JobStatus:
        job = runtime.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobStatus.model_validate(job.model_dump())

    @app.get("/api/jobs")
    async def list_jobs(limit: int = 100) -> list[JobStatus]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return [JobStatus.model_validate(job.model_dump()) for job in runtime.jobs.list(limit)]

    @app.get("/api/jobs/{job_id}/events")
    async def get_job_events(job_id: str) -> list[dict[str, object]]:
        if runtime.jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return runtime.jobs.events(job_id)

    @app.post("/api/jobs/{job_id}/retry", response_model=JobStatus)
    async def retry_job(job_id: str) -> JobStatus:
        try:
            job = runtime.jobs.retry(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.job_wakeup.set()
        return JobStatus.model_validate(job.model_dump())

    @app.get("/api/documents/{document_id}/pdf")
    async def document_pdf(document_id: str) -> FileResponse:
        document = runtime.catalog.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        path = Path(document["source_path"])
        if not path.is_file():
            raise HTTPException(status_code=410, detail="Preserved source is missing")
        return FileResponse(
            path, media_type="application/pdf", filename=document["source_filename"]
        )

    @app.get(
        "/api/documents/{document_id}/parses",
        response_model=list[ParseVersionResponse],
    )
    async def document_parses(document_id: str) -> list[ParseVersionResponse]:
        try:
            versions = runtime.reviews.list_versions(document_id)
        except ParseReviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not versions:
            raise HTTPException(status_code=404, detail="No parse versions found")
        return [ParseVersionResponse.model_validate(version.model_dump()) for version in versions]

    @app.get("/api/documents/{document_id}/routes")
    async def document_routes(document_id: str) -> list[dict[str, Any]]:
        try:
            return runtime.reviews.list_routes(document_id)
        except ParseReviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/documents/{document_id}/parses/{parse_id}/activate",
        response_model=ParseReviewResponse,
    )
    async def activate_document_parse(
        document_id: str,
        parse_id: str,
        request: ParseReviewRequest,
    ) -> ParseReviewResponse:
        try:
            result = await runtime.reviews.activate(
                document_id,
                parse_id,
                reason=request.reason,
                force=request.force,
            )
        except ParseReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ParseReviewResponse.model_validate(result.model_dump())

    @app.get("/api/documents/{document_id}/pages/{page_number}.png")
    async def document_page(document_id: str, page_number: int) -> FileResponse:
        document = runtime.catalog.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        if page_number < 1:
            raise HTTPException(status_code=422, detail="Page numbers start at 1")
        target = Path(document["artifact_dir"]) / "pages" / f"page-{page_number:04d}@2x.png"
        try:
            await asyncio.to_thread(
                _render_pdf_page, Path(document["source_path"]), page_number, target
            )
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="Page not found") from exc
        return FileResponse(target, media_type="image/png")

    @app.get("/api/documents/{document_id}/blocks")
    async def document_blocks(document_id: str, page: int | None = None) -> list[dict[str, Any]]:
        document = runtime.catalog.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        canonical = Path(document["artifact_dir"]) / "canonical" / "document.json"
        payload = json.loads(canonical.read_text(encoding="utf-8"))
        blocks = payload["blocks"]
        if page is not None:
            blocks = [block for block in blocks if block["source"]["page_number"] == page]
        return blocks

    @app.get("/api/documents/{document_id}/metadata")
    async def document_metadata(document_id: str) -> dict[str, Any]:
        if runtime.catalog.get_document(document_id) is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return runtime.catalog.get_metadata(document_id) or {}

    @app.get(
        "/api/documents/{document_id}/personal",
        response_model=PersonalPaperStateResponse,
    )
    async def document_personal_state(document_id: str) -> PersonalPaperStateResponse:
        if runtime.catalog.get_document(document_id) is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return PersonalPaperStateResponse.model_validate(
            runtime.library.get_state(document_id).model_dump()
        )

    @app.put(
        "/api/documents/{document_id}/personal",
        response_model=PersonalPaperStateResponse,
    )
    async def update_document_personal_state(
        document_id: str, request: PersonalPaperStateRequest
    ) -> PersonalPaperStateResponse:
        if runtime.catalog.get_document(document_id) is None:
            raise HTTPException(status_code=404, detail="Document not found")
        current = runtime.library.get_state(document_id)
        saved = runtime.library.save_state(
            PersonalPaperState.model_validate(
                {
                    **current.model_dump(),
                    **request.model_dump(),
                }
            )
        )
        return PersonalPaperStateResponse.model_validate(saved.model_dump())

    @app.get("/api/collections", response_model=list[CollectionResponse])
    async def list_collections() -> list[CollectionResponse]:
        return [
            CollectionResponse.model_validate(item.model_dump())
            for item in runtime.library.list_collections()
        ]

    @app.post("/api/collections", response_model=CollectionResponse, status_code=201)
    async def create_collection(request: CollectionRequest) -> CollectionResponse:
        known = {item["document_id"] for item in runtime.catalog.list_documents()}
        unknown = sorted(set(request.document_ids) - known)
        if unknown:
            raise HTTPException(status_code=422, detail={"unknown_document_ids": unknown})
        value = runtime.library.save_collection(request.name, request.document_ids)
        return CollectionResponse.model_validate(value.model_dump())

    @app.get("/api/knowledge-graph")
    async def knowledge_graph(document_id: list[str] | None = None) -> dict[str, Any]:
        return runtime.catalog.knowledge_graph(document_id)

    @app.post("/api/query", response_model=QueryResponse)
    async def query(request: QueryRequest) -> QueryResponse:
        service = AgenticRAGService(settings) if request.agentic else RAGService(settings)
        answer = await service.answer(
            request.question,
            document_ids=request.document_ids,
            top_k=request.top_k,
        )
        return QueryResponse.model_validate(answer.model_dump())

    @app.post("/api/query-runs", response_model=QueryRunResponse, status_code=202)
    async def create_query_run(request: QueryRequest) -> QueryRunResponse:
        run = await runtime.query_runs.create(
            question=request.question,
            document_ids=request.document_ids,
            top_k=request.top_k,
            agentic=request.agentic,
            conversation_id=request.conversation_id,
            parent_run_id=request.parent_run_id,
        )
        task = asyncio.create_task(_run_query(runtime, run.run_id))
        runtime.tasks.add(task)
        runtime.query_tasks[run.run_id] = task

        def discard(completed: asyncio.Task[Any]) -> None:
            runtime.tasks.discard(completed)
            runtime.query_tasks.pop(run.run_id, None)

        task.add_done_callback(discard)
        return QueryRunResponse.model_validate(run.model_dump())

    @app.get("/api/query-runs", response_model=list[QueryRunResponse])
    async def list_query_runs(limit: int = 50) -> list[QueryRunResponse]:
        if not 1 <= limit <= 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        return [
            QueryRunResponse.model_validate(run.model_dump())
            for run in runtime.query_runs.list(limit=limit)
        ]

    @app.get("/api/query-runs/{run_id}", response_model=QueryRunResponse)
    async def get_query_run(run_id: str) -> QueryRunResponse:
        try:
            run = runtime.query_runs.get(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="Query run not found")
        return QueryRunResponse.model_validate(run.model_dump())

    @app.get("/api/query-runs/{run_id}/events")
    async def query_run_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            run = runtime.query_runs.get(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="Query run not found")
        try:
            cursor = max(0, int(last_event_id or 0))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Last-Event-ID must be an integer") from exc

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                events = runtime.query_runs.list_events(run_id, after_seq=cursor)
                if not events:
                    current = runtime.query_runs.get(run_id)
                    if current is None or (
                        current.status in {"completed", "failed", "cancelled"}
                        and cursor >= current.event_count
                    ):
                        break
                    events = await runtime.query_runs.wait_for_events(
                        run_id, after_seq=cursor, timeout=15
                    )
                if not events:
                    yield ": heartbeat\n\n"
                    continue
                for event in events:
                    cursor = event.seq
                    yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/query-runs/{run_id}/event-log")
    async def query_run_event_log(run_id: str) -> list[dict[str, Any]]:
        try:
            run = runtime.query_runs.get(run_id)
            events = runtime.query_runs.list_events(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="Query run not found")
        return [event.model_dump(mode="json") for event in events]

    @app.get("/api/conversations")
    async def list_conversations() -> list[dict[str, Any]]:
        groups: dict[str, list[Any]] = {}
        for run in runtime.query_runs.list(limit=1000):
            if run.conversation_id:
                groups.setdefault(run.conversation_id, []).append(run)
        return [
            {
                "conversation_id": conversation_id,
                "run_count": len(runs),
                "latest_at": max(run.created_at for run in runs),
                "latest_question": max(runs, key=lambda run: run.created_at).question,
            }
            for conversation_id, runs in groups.items()
        ]

    @app.get("/api/query-runs/{run_id}/export")
    async def export_query_run(run_id: str) -> StreamingResponse:
        run = runtime.query_runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Query run not found")
        if run.answer is None:
            raise HTTPException(status_code=409, detail="Query run has no answer")
        citations = "\n".join(
            f"- **{item.citation_id} · p.{item.page_number}** — {item.quote[:500]}"
            for item in run.answer.citations
        )
        claims = "\n".join(
            f"- {claim.claim_id}: {claim.text} ({', '.join(claim.citation_ids) or 'no citation'})"
            for claim in run.answer.claims
        )
        report = (
            f"# ScholarRAG 阅读报告\n\n## 问题\n\n{run.question}\n\n"
            f"## 回答\n\n{run.answer.answer}\n\n## Claim 审计\n\n{claims or '—'}\n\n"
            f"## 引用证据\n\n{citations or '—'}\n\n## Trace\n\n"
            f"- Run: `{run.run_id}`\n- Trace: `{run.trace_id}`\n- Events: {run.event_count}\n"
        )
        return StreamingResponse(
            iter([report]),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="scholarrag-{run_id}.md"'},
        )

    @app.post("/api/query-runs/{run_id}/cancel", response_model=QueryRunResponse)
    async def cancel_query_run(run_id: str) -> QueryRunResponse:
        try:
            run = runtime.query_runs.get(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="Query run not found")
        task = runtime.query_tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            await task
        current = runtime.query_runs.get(run_id) or run
        return QueryRunResponse.model_validate(current.model_dump())

    @app.post(
        "/api/evaluations",
        response_model=EvaluationJobResponse,
        status_code=202,
    )
    async def create_evaluation(request: EvaluationRequest) -> EvaluationJobResponse:
        case = EvaluationCase.model_validate(request.model_dump(exclude={"query_run_id"}))
        if request.query_run_id:
            try:
                query_run = runtime.query_runs.get(request.query_run_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if query_run is None:
                raise HTTPException(status_code=404, detail="Query run not found")
            if query_run.status != "completed" or query_run.answer is None:
                raise HTTPException(
                    status_code=409,
                    detail="Only a completed query run can be evaluated",
                )
            case = case.model_copy(
                update={
                    "question": query_run.question,
                    "document_ids": query_run.document_ids,
                }
            )
        fingerprint = runtime.evaluation_jobs.fingerprint(
            case,
            query_run_id=request.query_run_id,
            judge_model=settings.ragas_judge_model,
            judge_enable_thinking=settings.ragas_judge_enable_thinking,
        )
        job, created = await runtime.evaluation_jobs.create_or_get_active(
            case=case,
            query_run_id=request.query_run_id,
            fingerprint=fingerprint,
        )
        if created:
            task = asyncio.create_task(_run_evaluation(runtime, job.job_id))
            runtime.tasks.add(task)
            runtime.evaluation_tasks[job.job_id] = task

            def discard(completed: asyncio.Task[Any]) -> None:
                runtime.tasks.discard(completed)
                runtime.evaluation_tasks.pop(job.job_id, None)

            task.add_done_callback(discard)
        return EvaluationJobResponse.model_validate(job.model_dump())

    @app.get("/api/evaluations/{job_id}", response_model=EvaluationJobResponse)
    async def get_evaluation(job_id: str) -> EvaluationJobResponse:
        try:
            job = runtime.evaluation_jobs.get(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if job is None:
            raise HTTPException(status_code=404, detail="Evaluation job not found")
        return EvaluationJobResponse.model_validate(job.model_dump())

    @app.get(
        "/api/query-runs/{run_id}/evaluations/latest",
        response_model=EvaluationJobResponse,
    )
    async def latest_evaluation(run_id: str) -> EvaluationJobResponse:
        try:
            query_run = runtime.query_runs.get(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if query_run is None:
            raise HTTPException(status_code=404, detail="Query run not found")
        job = runtime.evaluation_jobs.latest_for_query_run(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No evaluation found for this query run")
        return EvaluationJobResponse.model_validate(job.model_dump())

    @app.get("/api/evaluations/{job_id}/events")
    async def evaluation_events(
        job_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            job = runtime.evaluation_jobs.get(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if job is None:
            raise HTTPException(status_code=404, detail="Evaluation job not found")
        try:
            cursor = max(0, int(last_event_id or 0))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Last-Event-ID must be an integer") from exc

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                events = runtime.evaluation_jobs.list_events(job_id, after_seq=cursor)
                if not events:
                    current = runtime.evaluation_jobs.get(job_id)
                    if current is None or (
                        current.status in {"completed", "failed", "cancelled"}
                        and cursor >= current.event_count
                    ):
                        break
                    events = await runtime.evaluation_jobs.wait_for_events(
                        job_id, after_seq=cursor, timeout=15
                    )
                if not events:
                    yield ": heartbeat\n\n"
                    continue
                for event in events:
                    cursor = event.seq
                    yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/evaluations/{job_id}/cancel", response_model=EvaluationJobResponse)
    async def cancel_evaluation(job_id: str) -> EvaluationJobResponse:
        try:
            job = runtime.evaluation_jobs.get(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if job is None:
            raise HTTPException(status_code=404, detail="Evaluation job not found")
        task = runtime.evaluation_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        current = runtime.evaluation_jobs.get(job_id) or job
        if current.status in {"queued", "running"}:
            current = await runtime.evaluation_jobs.cancel(job_id)
        return EvaluationJobResponse.model_validate(current.model_dump())

    FastAPIInstrumentor.instrument_app(app)
    return app


async def _run_evaluation(runtime: Runtime, job_id: str) -> None:
    job = runtime.evaluation_jobs.get(job_id)
    if job is None:
        return
    try:
        await runtime.evaluation_jobs.start(job_id)
        parent_recorder = (
            runtime.query_runs.recorder(job.query_run_id) if job.query_run_id else None
        )
        events = runtime.evaluation_jobs.recorder(job_id, parent=parent_recorder)
        evaluator = RagasEvaluator(runtime.settings)
        if job.query_run_id:
            query_run = runtime.query_runs.get(job.query_run_id)
            if query_run is None or query_run.status != "completed" or query_run.answer is None:
                raise RuntimeError("The parent query run is no longer available for evaluation")
            result = await evaluator.evaluate_answer(
                case=job.case,
                answer=query_run.answer,
                parent_query_run_id=query_run.run_id,
                events=events,
            )
        else:
            result = await evaluator.evaluate_rag(job.case, events=events)
        await runtime.evaluation_jobs.complete(job_id, result)
    except asyncio.CancelledError:
        current = runtime.evaluation_jobs.get(job_id)
        if current and current.status in {"queued", "running"}:
            await runtime.evaluation_jobs.cancel(job_id)
    except Exception as exc:  # noqa: BLE001 - terminal state must always be persisted
        current = runtime.evaluation_jobs.get(job_id)
        if current and current.status in {"queued", "running"}:
            await runtime.evaluation_jobs.fail(job_id, f"{type(exc).__name__}: {exc}"[:2000])


async def _ingestion_worker(runtime: Runtime) -> None:
    while True:
        job = runtime.jobs.claim_next()
        if job is None:
            runtime.job_wakeup.clear()
            try:
                await asyncio.wait_for(runtime.job_wakeup.wait(), timeout=2.0)
            except TimeoutError:
                pass
            continue
        await _run_ingestion(runtime, job.job_id, Path(job.source_path))


async def _run_ingestion(runtime: Runtime, job_id: str, path: Path) -> None:
    job = runtime.jobs.get(job_id)
    if job is None:
        return
    tracer = get_tracer()
    try:
        with tracer.start_as_current_span("document.ingest") as span:
            span.set_attribute("document.filename", job.filename)
            document, _, parse_dir = await IngestionService(runtime.settings).ingest(path)
            span.set_attribute("document.id", document.document_id)
            manifest = json.loads((parse_dir / "manifest.json").read_text(encoding="utf-8"))
            quality = manifest.get("quality") or {}
            span.set_attribute("document.parse.quality_score", quality.get("score", 0.0))
            span.set_attribute(
                "document.parse.quality_decision", quality.get("decision", "unknown")
            )
        runtime.jobs.complete(
            job_id,
            document_id=document.document_id,
            parse_id=document.parse_id,
        )
    except Exception as exc:  # noqa: BLE001 - background jobs must persist terminal failure state
        runtime.jobs.fail(job_id, f"{type(exc).__name__}: {exc}")
        runtime.job_wakeup.set()


async def _run_query(runtime: Runtime, run_id: str) -> None:
    run = runtime.query_runs.get(run_id)
    if run is None:
        return
    recorder = runtime.query_runs.recorder(run_id)
    await runtime.query_runs.update(run_id, status="running", started_at=utc_now())
    await recorder.emit(
        event_type="run.started",
        stage_id="request",
        label="接收问题",
        status="success",
        summary="开始执行问答链路",
    )
    try:
        contextual_question = run.question
        if run.parent_run_id:
            parent = runtime.query_runs.get(run.parent_run_id)
            if parent and parent.answer:
                contextual_question = (
                    f"上轮问题：{parent.question}\n上轮回答：{parent.answer.answer[:4000]}\n\n"
                    f"当前追问：{run.question}"
                )
                await recorder.emit(
                    event_type="conversation.context",
                    stage_id="request.context",
                    label="加载会话上下文",
                    status="success",
                    component="query_run_store",
                    summary="已加载父问题与回答，形成可审计分支",
                    input={"parent_run_id": parent.run_id},
                )
        service = (
            AgenticRAGService(runtime.settings) if run.agentic else RAGService(runtime.settings)
        )
        answer = await service.answer(
            contextual_question,
            document_ids=run.document_ids,
            top_k=run.top_k,
            events=recorder,
            trace_id=run.trace_id,
        )
        await runtime.query_runs.finalize(
            run_id,
            status="completed",
            completed_at=utc_now(),
            answer=answer,
            event_type="run.completed",
            stage_id="response.persist",
            label="回答完成与 Trace 持久化",
            event_status="success",
            summary=f"回答完成，返回 {len(answer.citations)} 个引用",
            output={
                "answer_chars": len(answer.answer),
                "citation_count": len(answer.citations),
            },
            trace={"trace_id": answer.trace_id},
        )
    except asyncio.CancelledError:
        await runtime.query_runs.finalize(
            run_id,
            status="cancelled",
            completed_at=utc_now(),
            error="Cancelled by user",
            event_type="run.cancelled",
            stage_id="response.persist",
            label="回答已取消",
            event_status="cancelled",
            summary="用户取消了本次问答",
        )
    except Exception as exc:  # noqa: BLE001 - run artifact must record terminal failures
        error = f"{type(exc).__name__}: {exc}"
        await runtime.query_runs.finalize(
            run_id,
            status="failed",
            completed_at=utc_now(),
            error=error,
            event_type="run.failed",
            stage_id="response.persist",
            label="问答失败",
            event_status="failed",
            summary="问答链路执行失败",
        )


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", name)[:180]


def _render_pdf_page(source: Path, page_number: int, target: Path) -> None:
    if target.is_file():
        return
    import pymupdf

    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as pdf:
        if page_number > pdf.page_count:
            raise IndexError(page_number)
        page = pdf.load_page(page_number - 1)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        pixmap.save(target)


app = create_app()
