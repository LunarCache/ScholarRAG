"""Local-only browser QA fixture: synthetic PDFs, disposable identity, no worker/provider.

Run from the repo root: .venv/bin/python tests/browser_fixture.py
The printed directory is disposable; it never uses the project's data directory.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pymupdf
import uvicorn


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="scholarrag-browser-"))
    os.environ["SCHOLARRAG_DATA_DIR"] = str(root)
    os.environ["SCHOLARRAG_AUTH_MODE"] = "session"
    os.environ["SCHOLARRAG_LANGFUSE_ENABLED"] = "false"
    for key in ("MINERU_API_TOKEN", "PADDLEOCR_ACCESS_TOKEN", "SILICONFLOW_API_KEY"):
        os.environ[key] = "browser-fixture-not-a-real-key"
    from scholarrag.config import Settings

    Settings.model_config["env_file"] = None
    from scholarrag.api.main import create_app
    from scholarrag.domain.access import Role
    from scholarrag.domain.models import CanonicalDocument
    from scholarrag.storage.artifacts import ArtifactStore

    settings = Settings(data_dir=root, auth_mode="session", langfuse_enabled=False)
    app = create_app(settings)
    runtime = app.state.runtime
    store = runtime.identities
    owner = store.create_user("Fixture Owner", issuer="browser-test", subject="owner")
    reader = store.create_user("Demo Reader", issuer="browser-test", subject="reader")
    session_id, _ = store.issue_session(reader)
    token = "scholar-disposable-browser-session"
    with sqlite3.connect(settings.sqlite_path) as connection:
        connection.execute(
            "UPDATE auth_sessions SET token_hash=? WHERE session_id=?",
            (hashlib.sha256(token.encode()).hexdigest(), session_id),
        )
    principal = store.authenticate(token)
    owner_principal = store.authenticate(store.issue_session(owner)[1])
    workspaces = []
    for title, role in (("Alpha Research", Role.MEMBER), ("Beta Reading", Role.VIEWER)):
        ws = store.create_workspace(owner, title)
        store.set_member(store.context_for(owner_principal, ws), reader, role)
        workspaces.append(ws)
        source = root / f"{title.split()[0]}.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), f"Synthetic {title} paper. Browser QA only.")
            pdf.save(source)
        document = CanonicalDocument(
            document_id=f"fixture-{title.split()[0]}",
            parse_id=f"parse-{title.split()[0]}",
            title=f"Synthetic {title} Paper",
            source_filename=source.name,
            source_sha256=ArtifactStore.sha256(source),
            provider="fixture",
            provider_model="fixture",
        )
        runtime.catalog.register(document, [], source_path=source, artifact_dir=root / "artifacts")
        store.assign_document(document.document_id, ws, reader)
        if role == Role.MEMBER:
            runtime.workspace_ingestions.enqueue(
                store.context_for(principal, ws),
                filename="queued-synthetic.pdf",
                source_path=source,
                source_sha256=ArtifactStore.sha256(source),
                idempotency_key="fixture-initial",
            )
    upload = root / "browser-upload.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Synthetic browser upload. No provider will run.")
        pdf.save(upload)

    @asynccontextmanager
    async def no_worker(_):
        yield

    app.router.lifespan_context = no_worker
    print(f"Browser fixture directory: {root}", flush=True)
    print(f"Disposable login: {token}", flush=True)
    print(f"Synthetic upload: {upload}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
