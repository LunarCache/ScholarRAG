"""Only these routes are enabled in the initial session-mode API."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from scholarrag.api.schemas import QueryRequest
from scholarrag.api.uploads import save_pdf_upload
from scholarrag.config import Settings
from scholarrag.domain.access import AccessDenied, Principal, Role
from scholarrag.domain.library import (
    WorkspaceCollection,
    WorkspaceCollectionRequest,
    WorkspaceCollectionUpdate,
    WorkspacePaperState,
    WorkspaceStateRequest,
)
from scholarrag.evaluation.workspaces import (
    WorkspaceEvaluationRequest,
    WorkspaceEvaluations,
    public_evaluation,
)
from scholarrag.ingestion.workspace_reviews import WorkspaceParseActivation, WorkspaceParseReviews
from scholarrag.ingestion.workspaces import WorkspaceIngestions
from scholarrag.rag.workspaces import WorkspaceQueries, public_run
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog
from scholarrag.storage.identity import IdentityStore
from scholarrag.storage.workspace_library import WorkspaceLibrary


class MemberRequest(BaseModel):
    role: Role


class TransferRequest(BaseModel):
    user_id: str


UPLOAD_FILE = File(...)


def workspace_router(
    settings: Settings,
    identities: IdentityStore,
    catalog: Catalog,
    queries: WorkspaceQueries,
    ingestions: WorkspaceIngestions,
    evaluations: WorkspaceEvaluations,
) -> APIRouter:
    library = WorkspaceLibrary(identities)
    parse_reviews = WorkspaceParseReviews(settings, identities)

    def principal(request: Request) -> Principal:
        if settings.auth_mode != "session":
            raise HTTPException(status_code=404, detail="Not found")
        identity = getattr(request.state, "principal", None)
        if identity is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return identity

    identity_dependency = Depends(principal)
    router = APIRouter(prefix="/api", dependencies=[identity_dependency])

    @router.post("/workspaces/{workspace_id}/uploads", status_code=202)
    async def upload(
        workspace_id: str,
        file: UploadFile = UPLOAD_FILE,
        idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
        identity: Principal = identity_dependency,
    ) -> dict:
        context = identities.context_for(identity, workspace_id)
        identities.authorize(context, "upload")
        if not idempotency_key.strip():
            raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
        filename, path = await save_pdf_upload(
            file,
            settings,
            upload_dir=settings.data_dir / "workspaces" / context.workspace_id / "uploads",
        )
        retained = False
        try:
            source_hash = await asyncio.to_thread(ArtifactStore.sha256, path)
            # No cancellation point between the atomic enqueue and retained-path assignment.
            result, retained = ingestions.enqueue(
                context,
                filename=filename,
                source_path=path,
                source_sha256=source_hash,
                idempotency_key=idempotency_key,
            )
            return result
        finally:
            if not retained:
                path.unlink(missing_ok=True)

    @router.get("/workspaces/{workspace_id}/ingestion-jobs")
    def ingestion_jobs(
        workspace_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        identity: Principal = identity_dependency,
    ) -> list[dict]:
        return ingestions.list(identities.context_for(identity, workspace_id), limit=limit)

    @router.get("/workspaces/{workspace_id}/ingestion-jobs/{job_id}")
    def ingestion_job(
        workspace_id: str,
        job_id: str,
        identity: Principal = identity_dependency,
    ) -> dict:
        return ingestions.read(identities.context_for(identity, workspace_id), job_id)

    @router.get("/workspaces/{workspace_id}/ingestion-jobs/{job_id}/event-log")
    def ingestion_events(
        workspace_id: str,
        job_id: str,
        identity: Principal = identity_dependency,
    ) -> list[dict]:
        return ingestions.events(identities.context_for(identity, workspace_id), job_id)

    @router.post("/workspaces/{workspace_id}/ingestion-jobs/{job_id}/cancel")
    def cancel_ingestion(
        workspace_id: str,
        job_id: str,
        identity: Principal = identity_dependency,
    ) -> dict:
        return ingestions.cancel(identities.context_for(identity, workspace_id), job_id)

    @router.get("/me")
    def profile(identity: Principal = identity_dependency) -> dict:
        return identities.profile(identity)

    @router.get("/workspaces")
    def list_workspaces(identity: Principal = identity_dependency) -> list[dict]:
        return identities.list_workspaces(identity)

    @router.delete("/me/session", status_code=204)
    def logout(identity: Principal = identity_dependency) -> Response:
        identities.revoke_current(identity)
        return Response(status_code=204)

    @router.get("/workspaces/{workspace_id}/members")
    def members(workspace_id: str, identity: Principal = identity_dependency) -> list[dict]:
        return identities.list_members(identities.context_for(identity, workspace_id))

    @router.put("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
    def set_member(
        workspace_id: str,
        user_id: str,
        body: MemberRequest,
        identity: Principal = identity_dependency,
    ) -> Response:
        identities.set_member(identities.context_for(identity, workspace_id), user_id, body.role)
        return Response(status_code=204)

    @router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
    def remove_member(
        workspace_id: str,
        user_id: str,
        identity: Principal = identity_dependency,
    ) -> Response:
        identities.remove_member(identities.context_for(identity, workspace_id), user_id)
        return Response(status_code=204)

    @router.post("/workspaces/{workspace_id}/transfer-owner", status_code=204)
    def transfer_owner(
        workspace_id: str,
        body: TransferRequest,
        identity: Principal = identity_dependency,
    ) -> Response:
        identities.transfer_owner(identities.context_for(identity, workspace_id), body.user_id)
        return Response(status_code=204)

    @router.get("/workspaces/{workspace_id}/documents")
    def documents(workspace_id: str, identity: Principal = identity_dependency) -> list[dict]:
        context = identities.context_for(identity, workspace_id)
        allowed = set(identities.document_ids(context))
        return [
            {
                key: doc.get(key)
                for key in (
                    "document_id",
                    "parse_id",
                    "title",
                    "source_filename",
                    "provider",
                    "provider_model",
                    "chunk_count",
                )
            }
            for doc in catalog.list_documents(sorted(allowed))
        ]

    @router.get("/workspaces/{workspace_id}/documents/{document_id}/parse-review")
    def parse_review(
        workspace_id: str, document_id: str, identity: Principal = identity_dependency
    ):
        return parse_reviews.snapshot(identities.context_for(identity, workspace_id), document_id)

    @router.post("/workspaces/{workspace_id}/documents/{document_id}/parses/{parse_id}/activate")
    def activate_parse(
        workspace_id: str,
        document_id: str,
        parse_id: str,
        body: WorkspaceParseActivation,
        identity: Principal = identity_dependency,
    ):
        return parse_reviews.activate(
            identities.context_for(identity, workspace_id), document_id, parse_id, body
        )

    @router.get("/workspaces/{workspace_id}/documents/{document_id}/pdf")
    def pdf(
        workspace_id: str,
        document_id: str,
        identity: Principal = identity_dependency,
    ) -> FileResponse:
        context = identities.context_for(identity, workspace_id)
        if document_id not in identities.document_ids(context):
            raise AccessDenied("Document unavailable")
        doc = catalog.get_document(document_id)
        if doc is None or not Path(doc["source_path"]).is_file():
            raise HTTPException(status_code=404, detail="Document unavailable")
        return FileResponse(doc["source_path"], media_type="application/pdf")

    @router.post("/workspaces/{workspace_id}/query-runs", status_code=202)
    async def create_run(
        workspace_id: str,
        body: QueryRequest,
        identity: Principal = identity_dependency,
    ) -> dict:
        context = identities.context_for(identity, workspace_id)
        return public_run(await queries.create(context, body))

    @router.get("/workspaces/{workspace_id}/query-runs")
    def list_runs(
        workspace_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        identity: Principal = identity_dependency,
    ) -> list[dict]:
        return queries.list(identities.context_for(identity, workspace_id), limit=limit)

    @router.get("/workspaces/{workspace_id}/query-runs/{run_id}")
    def get_run(workspace_id: str, run_id: str, identity: Principal = identity_dependency) -> dict:
        return public_run(queries.read(identities.context_for(identity, workspace_id), run_id))

    @router.get("/workspaces/{workspace_id}/query-runs/{run_id}/event-log")
    def event_log(
        workspace_id: str, run_id: str, identity: Principal = identity_dependency
    ) -> list[dict]:
        context = identities.context_for(identity, workspace_id)
        queries.read(context, run_id)
        events = queries.store(workspace_id).list_events(run_id)
        queries.read(context, run_id)
        return [event.model_dump(mode="json") for event in events]

    @router.get("/workspaces/{workspace_id}/query-runs/{run_id}/events")
    async def stream_events(
        workspace_id: str,
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        identity: Principal = identity_dependency,
    ) -> StreamingResponse:
        context = identities.context_for(identity, workspace_id)
        queries.read(context, run_id)
        try:
            cursor = int(last_event_id or "0")
            if cursor < 0:
                raise ValueError("negative cursor")
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Last-Event-ID must be a nonnegative integer"
            ) from exc
        return StreamingResponse(
            queries.events(context, run_id, after_seq=cursor, disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @router.post("/workspaces/{workspace_id}/query-runs/{run_id}/cancel")
    async def cancel_run(
        workspace_id: str, run_id: str, identity: Principal = identity_dependency
    ) -> dict:
        return public_run(
            await queries.cancel(identities.context_for(identity, workspace_id), run_id)
        )

    @router.get("/workspaces/{workspace_id}/query-runs/{run_id}/export")
    def export_run(
        workspace_id: str, run_id: str, identity: Principal = identity_dependency
    ) -> Response:
        context = identities.context_for(identity, workspace_id)
        run = queries.read(context, run_id)
        if run.answer is None:
            raise HTTPException(status_code=409, detail="Query run has no answer")
        evidence = "\n".join(
            f"- {citation.citation_id} · p.{citation.page_number}: {citation.quote}"
            for citation in run.answer.citations
        )
        report = f"# ScholarRAG\n\n{run.question}\n\n{run.answer.answer}\n\n{evidence}\n"
        queries.read(context, run_id)
        return Response(
            report,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="scholarrag-{run_id}.md"',
            },
        )

    @router.post("/workspaces/{workspace_id}/evaluations", status_code=202)
    async def create_evaluation(
        workspace_id: str,
        request: WorkspaceEvaluationRequest,
        idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
        identity: Principal = identity_dependency,
    ):
        if not idempotency_key.strip():
            raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
        return public_evaluation(
            await evaluations.create(
                identities.context_for(identity, workspace_id), request, idempotency_key
            )
        )

    @router.get("/workspaces/{workspace_id}/evaluations/{job_id}")
    def get_evaluation(workspace_id: str, job_id: str, identity: Principal = identity_dependency):
        return public_evaluation(
            evaluations.read(identities.context_for(identity, workspace_id), job_id)
        )

    @router.get("/workspaces/{workspace_id}/query-runs/{run_id}/evaluations/latest")
    def latest_evaluation(
        workspace_id: str, run_id: str, identity: Principal = identity_dependency
    ):
        return public_evaluation(
            evaluations.latest(identities.context_for(identity, workspace_id), run_id)
        )

    @router.post("/workspaces/{workspace_id}/evaluations/{job_id}/cancel")
    async def cancel_evaluation(
        workspace_id: str, job_id: str, identity: Principal = identity_dependency
    ):
        return public_evaluation(
            await evaluations.cancel(identities.context_for(identity, workspace_id), job_id)
        )

    @router.get("/workspaces/{workspace_id}/evaluations/{job_id}/event-log")
    def evaluation_log(workspace_id: str, job_id: str, identity: Principal = identity_dependency):
        context = identities.context_for(identity, workspace_id)
        evaluations.read(context, job_id)
        events = evaluations.store(workspace_id).list_events(job_id)
        evaluations.read(context, job_id)
        return [event.model_dump(mode="json") for event in events]

    @router.get("/workspaces/{workspace_id}/evaluations/{job_id}/events")
    async def evaluation_stream(
        workspace_id: str,
        job_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        identity: Principal = identity_dependency,
    ):
        context = identities.context_for(identity, workspace_id)
        evaluations.read(context, job_id)
        try:
            cursor = int(last_event_id or "0")
            if cursor < 0:
                raise ValueError("negative cursor")
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Last-Event-ID must be a nonnegative integer"
            ) from exc
        return StreamingResponse(
            evaluations.events(
                context, job_id, after_seq=cursor, disconnected=request.is_disconnected
            ),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @router.get("/workspaces/{workspace_id}/conversations")
    def conversations(workspace_id: str, identity: Principal = identity_dependency) -> list[dict]:
        return identities.owned_conversations(identities.context_for(identity, workspace_id))

    @router.get("/workspaces/{workspace_id}/conversations/{conversation_id}")
    def conversation(
        workspace_id: str,
        conversation_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        identity: Principal = identity_dependency,
    ) -> list[dict]:
        return queries.list(
            identities.context_for(identity, workspace_id),
            limit=limit,
            conversation_id=conversation_id,
        )

    @router.get(
        "/workspaces/{workspace_id}/documents/{document_id}/personal",
        response_model=WorkspacePaperState,
    )
    def personal_state(
        workspace_id: str, document_id: str, identity: Principal = identity_dependency
    ):
        return library.get_state(identities.context_for(identity, workspace_id), document_id)

    @router.put(
        "/workspaces/{workspace_id}/documents/{document_id}/personal",
        response_model=WorkspacePaperState,
    )
    def save_personal_state(
        workspace_id: str,
        document_id: str,
        body: WorkspaceStateRequest,
        identity: Principal = identity_dependency,
    ):
        return library.save_state(identities.context_for(identity, workspace_id), document_id, body)

    @router.delete("/workspaces/{workspace_id}/documents/{document_id}/personal", status_code=204)
    def delete_personal_state(
        workspace_id: str, document_id: str, identity: Principal = identity_dependency
    ):
        library.delete_state(identities.context_for(identity, workspace_id), document_id)
        return Response(status_code=204)

    @router.get("/workspaces/{workspace_id}/collections", response_model=list[WorkspaceCollection])
    def collections(
        workspace_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        identity: Principal = identity_dependency,
    ):
        return library.list_collections(identities.context_for(identity, workspace_id), limit=limit)

    @router.post(
        "/workspaces/{workspace_id}/collections",
        response_model=WorkspaceCollection,
        status_code=201,
    )
    def create_collection(
        workspace_id: str,
        body: WorkspaceCollectionRequest,
        identity: Principal = identity_dependency,
    ):
        return library.save_collection(identities.context_for(identity, workspace_id), body)

    @router.get(
        "/workspaces/{workspace_id}/collections/{collection_id}", response_model=WorkspaceCollection
    )
    def get_collection(
        workspace_id: str, collection_id: str, identity: Principal = identity_dependency
    ):
        return library.get_collection(identities.context_for(identity, workspace_id), collection_id)

    @router.put(
        "/workspaces/{workspace_id}/collections/{collection_id}", response_model=WorkspaceCollection
    )
    def update_collection(
        workspace_id: str,
        collection_id: str,
        body: WorkspaceCollectionUpdate,
        identity: Principal = identity_dependency,
    ):
        return library.save_collection(
            identities.context_for(identity, workspace_id), body, collection_id=collection_id
        )

    @router.delete("/workspaces/{workspace_id}/collections/{collection_id}", status_code=204)
    def delete_collection(
        workspace_id: str, collection_id: str, identity: Principal = identity_dependency
    ):
        library.delete_collection(identities.context_for(identity, workspace_id), collection_id)
        return Response(status_code=204)

    return router
