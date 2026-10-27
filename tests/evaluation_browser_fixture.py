"""Offline evaluation browser fixture; synthetic answer and simulated Judge only.

Run explicitly from the repo root. Creates a disposable library and starts a
single-process evaluation worker for UI checks. No real provider credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import uvicorn


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="scholarrag-evaluation-ui-"))
    os.environ["SCHOLARRAG_DATA_DIR"] = str(root)
    os.environ["SCHOLARRAG_AUTH_MODE"] = "session"
    os.environ["SCHOLARRAG_LANGFUSE_ENABLED"] = "false"
    for key in ("MINERU_API_TOKEN", "PADDLEOCR_ACCESS_TOKEN", "SILICONFLOW_API_KEY"):
        os.environ[key] = "offline-fixture-not-a-real-key"
    from scholarrag.config import Settings

    Settings.model_config["env_file"] = None
    from scholarrag.api.main import create_app
    from scholarrag.api.schemas import QueryRequest
    from scholarrag.domain.models import Answer, CanonicalDocument
    from scholarrag.evaluation.service import RagasEvaluator
    from scholarrag.evaluation.worker import EvaluationWorker
    from scholarrag.rag.runs import PipelineTracer, QueryRunStore, utc_now
    from scholarrag.storage.artifacts import ArtifactStore

    settings = Settings(
        data_dir=root,
        auth_mode="session",
        qdrant_url=None,
        langfuse_enabled=False,
        api_cors_origins=["http://127.0.0.1:3131"],
        authorization_poll_seconds=0.1,
        evaluation_worker_poll_seconds=0.05,
    )
    app = create_app(settings)
    runtime = app.state.runtime
    user = runtime.identities.create_user(
        "Evaluation Demo", issuer="offline-fixture", subject="reader"
    )
    session_id, _ = runtime.identities.issue_session(user)
    token = "evaluation-fixture-session"
    with sqlite3.connect(settings.sqlite_path) as connection:
        connection.execute(
            "UPDATE auth_sessions SET token_hash=? WHERE session_id=?",
            (hashlib.sha256(token.encode()).hexdigest(), session_id),
        )
    principal = runtime.identities.authenticate(token)
    workspace = runtime.identities.create_workspace(user, "Evaluation Sandbox")
    runtime.identities.create_workspace(user, "Empty Sandbox")
    context = runtime.identities.context_for(principal, workspace)
    source = root / "synthetic.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Synthetic document for offline browser testing.")
        pdf.save(source)
    document = CanonicalDocument(
        document_id="synthetic-ui-paper",
        parse_id="synthetic-ui-parse",
        source_sha256=ArtifactStore.sha256(source),
        source_filename=source.name,
        title="Synthetic UI Paper",
        provider="fixture",
        provider_model="fixture",
    )
    runtime.catalog.register(
        document, [], source_path=source, artifact_dir=root / "fixture-artifacts"
    )
    runtime.identities.assign_document(document.document_id, workspace, user)

    async def seed():
        run = await runtime.workspace_queries.create(
            context,
            QueryRequest(
                question="合成问答：用于评测界面验收",
                document_ids=[document.document_id],
                agentic=False,
            ),
        )
        claimed, lease = runtime.query_runs.claim_next("fixture")
        store = QueryRunStore(settings, workspace_id=workspace, lease_token=lease)
        await store.finalize(
            claimed.run_id,
            status="completed",
            completed_at=utc_now(),
            answer=Answer(
                answer="这是用于离线界面验收的合成回答，所有评测值均为模拟数据。",
                citations=[],
                trace_id=run.trace_id,
            ),
            event_type="run.completed",
            stage_id="response.persist",
            label="合成回答完成",
            event_status="success",
            summary="Fixture only",
        )

    asyncio.run(seed())

    async def synthetic_scores(self, *, question, response, contexts, reference=None, events=None):
        self.checkpoints.initialize()
        names = ["faithfulness", "answer_relevancy"] + (
            ["context_precision", "context_recall", "factual_correctness"] if reference else []
        )
        results = {}
        for name in names:

            async def operation():
                await asyncio.sleep(2)
                return SimpleNamespace(value=0.75, reason="模拟指标，仅用于界面验收")

            results[name] = await self._score_metric(
                name, operation, tracer=PipelineTracer(events), input={"synthetic": True}
            )
        return results

    RagasEvaluator.evaluate_response = synthetic_scores
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def fixture_worker(application):
        async with original_lifespan(application):
            task = asyncio.create_task(EvaluationWorker(runtime).run())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = fixture_worker
    print(f"Offline fixture directory: {root}", flush=True)
    print(f"Disposable login: {token}", flush=True)
    print(f"Workspace: {workspace}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8831, log_level="warning")


if __name__ == "__main__":
    main()
