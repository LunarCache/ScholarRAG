"""Opt-in live browser acceptance server. Uses configured providers; never runs in pytest.

Creates only disposable identities/data and an authored two-page PDF. Upload and
queries must be initiated explicitly through the browser/API. Providers may bill.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pymupdf
import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resume", type=Path, help="Restart a previously created disposable fixture"
    )
    args = parser.parse_args()
    if args.resume:
        root = args.resume.resolve()
        if (
            not root.name.startswith("scholarrag-live-browser-")
            or not (root / ".live-acceptance-fixture").is_file()
        ):
            raise SystemExit("Only a marked disposable live-acceptance directory can be resumed")
    else:
        root = Path(tempfile.mkdtemp(prefix="scholarrag-live-browser-"))
        (root / ".live-acceptance-fixture").touch()
    os.environ["SCHOLARRAG_DATA_DIR"] = str(root)
    os.environ["SCHOLARRAG_AUTH_MODE"] = "session"
    os.environ["SCHOLARRAG_LANGFUSE_ENABLED"] = "false"
    from scholarrag.config import Settings

    settings = Settings(
        data_dir=root,
        auth_mode="session",
        langfuse_enabled=False,
        qdrant_url=None,
        qdrant_api_key=None,
        parse_fallback_mode="off",
        chat_enable_thinking=False,
        provider_max_retries=0,
        agent_max_steps=2,
        agent_timeout_seconds=120,
    )
    if not settings.mineru_api_token or not settings.siliconflow_api_key:
        raise SystemExit("Live acceptance requires configured MinerU and SiliconFlow credentials")
    from scholarrag.api.main import create_app
    from scholarrag.domain.access import Role

    app = create_app(settings)
    # This opt-in fixture intentionally runs the real worker classes in one
    # process so its embedded Qdrant remains completely isolated. Normal API
    # startup has no workers; deployed processes use a shared Qdrant service.
    from scholarrag.evaluation.worker import EvaluationWorker
    from scholarrag.ingestion.worker import IngestionWorker
    from scholarrag.worker import QueryWorker

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def fixture_workers(application):
        async with original_lifespan(application):
            runtime = application.state.runtime
            tasks = [
                asyncio.create_task(QueryWorker(runtime).run()),
                asyncio.create_task(IngestionWorker(runtime).run()),
                asyncio.create_task(EvaluationWorker(runtime).run()),
            ]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = fixture_workers
    if args.resume:
        print(f"Resuming disposable acceptance data: {root}", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
        return
    store = app.state.runtime.identities
    owner = store.create_user("Acceptance Owner", issuer="live-test", subject="owner")
    reader = store.create_user("Acceptance Reader", issuer="live-test", subject="reader")
    outsider = store.create_user("Isolation Reader", issuer="live-test", subject="outsider")
    owner_principal = store.authenticate(store.issue_session(owner)[1])
    spaces = {}
    for name in ("Live Acceptance", "Isolation Space"):
        ws = store.create_workspace(owner, name)
        spaces[name] = ws
        context = store.context_for(owner_principal, ws)
        store.set_member(context, reader, Role.MEMBER if name == "Live Acceptance" else Role.VIEWER)
        if name == "Live Acceptance":
            store.set_member(context, outsider, Role.MEMBER)
    for user, token in (
        (reader, "scholar-live-acceptance-session"),
        (outsider, "scholar-live-isolation-session"),
    ):
        session_id, _ = store.issue_session(user)
        with sqlite3.connect(settings.sqlite_path) as connection:
            connection.execute(
                "UPDATE auth_sessions SET token_hash=? WHERE session_id=?",
                (hashlib.sha256(token.encode()).hexdigest(), session_id),
            )
    source = root / "acceptance-study.pdf"
    pages = [
        "ScholarRAG Acceptance Study\n\nAuthored test document. These are synthetic evaluation values, not claims about real research.\n\n1. Method\nA retrieval evaluation uses 120 fixed questions over six documents.\nThe dense baseline achieves recall@5 of 0.62.\nHybrid retrieval achieves recall@5 of 0.87.\nHybrid retrieval with reranking achieves recall@5 of 0.91.\n\nThe comparison uses identical questions and source documents for all three methods.\nThe aim is to test document ingestion, evidence retrieval and citations.",
        "2. Authorization and Interpretation\n\nEvery query is restricted to the selected documents inside the current workspace.\nPrivate notes, collections and conversations belong to their author.\nA citation locates source evidence; it does not itself prove semantic support.\n\n3. Results\nThe absolute gain of hybrid retrieval over the dense baseline is 0.25.\nThe relative gain is approximately 40.3 percent: (0.87 - 0.62) / 0.62.\nReranking adds a further 0.04 absolute recall@5.\n\nThis document is created only for acceptance testing. No personal or third-party paper content is included.",
    ]
    with pymupdf.open() as pdf:
        for text in pages:
            page = pdf.new_page()
            page.insert_textbox(pymupdf.Rect(60, 60, 535, 780), text, fontsize=13)
        pdf.save(source)
    print(f"Live acceptance data: {root}", flush=True)
    print(f"Authored test PDF: {source}", flush=True)
    print("Disposable login: scholar-live-acceptance-session", flush=True)
    print(f"Workspaces: {spaces}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
