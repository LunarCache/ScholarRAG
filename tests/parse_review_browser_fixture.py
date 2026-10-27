"""Explicit offline parse-review browser fixture; no workers or provider calls."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

import pymupdf
import uvicorn


def main():
    root = Path(tempfile.mkdtemp(prefix="scholarrag-review-ui-"))
    os.environ["SCHOLARRAG_DATA_DIR"] = str(root)
    os.environ["SCHOLARRAG_AUTH_MODE"] = "session"
    os.environ["SCHOLARRAG_LANGFUSE_ENABLED"] = "false"
    for key in ("MINERU_API_TOKEN", "PADDLEOCR_ACCESS_TOKEN", "SILICONFLOW_API_KEY"):
        os.environ[key] = "offline-fixture-not-a-real-key"
    from scholarrag.config import Settings

    Settings.model_config["env_file"] = None
    from scholarrag.api.main import create_app
    from scholarrag.domain.access import Role
    from scholarrag.domain.models import CanonicalDocument
    from scholarrag.storage.artifacts import ArtifactStore

    settings = Settings(
        data_dir=root,
        auth_mode="session",
        qdrant_url=None,
        langfuse_enabled=False,
        api_cors_origins=["http://127.0.0.1:3132"],
    )
    app = create_app(settings)
    runtime = app.state.runtime
    store = runtime.identities
    owner = store.create_user("Review Owner", issuer="fixture", subject="owner")
    viewer = store.create_user("Review Viewer", issuer="fixture", subject="viewer")
    for user, token in ((owner, "review-owner-fixture"), (viewer, "review-viewer-fixture")):
        session_id, _ = store.issue_session(user)
        with sqlite3.connect(settings.sqlite_path) as connection:
            connection.execute(
                "UPDATE auth_sessions SET token_hash=? WHERE session_id=?",
                (hashlib.sha256(token.encode()).hexdigest(), session_id),
            )
    workspace = store.create_workspace(owner, "Parse Review Fixture")
    context = store.context_for(store.authenticate("review-owner-fixture"), workspace)
    store.set_member(context, viewer, Role.VIEWER)
    store.create_workspace(owner, "Empty Isolation Space")
    source = root / "synthetic.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Synthetic parse review acceptance document.")
        pdf.save(source)
    artifacts = ArtifactStore(settings)
    for name in ("old", "new", "pending"):
        document = CanonicalDocument(
            document_id="doc_review_fixture",
            parse_id=f"parse_{name}",
            title=f"Synthetic {name} parse",
            source_filename="synthetic.pdf",
            source_sha256=ArtifactStore.sha256(source),
            provider="fixture",
            provider_model="offline",
        )
        directory = artifacts.parse_dir(document.document_id, document.parse_id)
        artifacts.write_json(
            directory / "canonical/document.json", document.model_dump(mode="json")
        )
        artifacts.write_json(directory / "chunks/chunks.json", [])
        artifacts.write_json(
            directory / "manifest.json",
            {
                "document_id": document.document_id,
                "parse_id": document.parse_id,
                "status": "ready",
                "provider": "fixture",
                "provider_model": "offline",
                "page_count": 1,
                "quality": {"score": 0.9, "decision": "accept"},
            },
        )
        if name != "pending":
            runtime.catalog.register(document, [], source_path=source, artifact_dir=directory)
    store.assign_document("doc_review_fixture", workspace, owner)
    print(f"Disposable fixture: {root}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8832, log_level="warning")


if __name__ == "__main__":
    main()
