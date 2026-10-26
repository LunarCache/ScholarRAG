"""Trusted local provisioning; never expose these commands as anonymous HTTP APIs."""

import json
import uuid

import typer

from scholarrag.config import get_settings
from scholarrag.storage.catalog import Catalog
from scholarrag.storage.identity import IdentityStore

app = typer.Typer(help="Provision identities and revoke sessions as the local administrator.")


@app.command("create-user")
def create_user(name: str, subject: str | None = None) -> None:
    subject = subject or uuid.uuid4().hex
    user_id = IdentityStore(get_settings()).create_user(name, issuer="local-admin", subject=subject)
    typer.echo(json.dumps({"user_id": user_id, "issuer": "local-admin", "subject": subject}))


@app.command("create-workspace")
def create_workspace(owner_id: str, name: str) -> None:
    workspace_id = IdentityStore(get_settings()).create_workspace(owner_id, name)
    typer.echo(json.dumps({"workspace_id": workspace_id, "owner_id": owner_id}))


@app.command("issue-session")
def issue_session(user_id: str, ttl_seconds: int = 28800) -> None:
    """Print the secret once. Store stdout securely; the database keeps only its hash."""
    session_id, token = IdentityStore(get_settings()).issue_session(
        user_id, ttl_seconds=ttl_seconds
    )
    typer.echo(json.dumps({"session_id": session_id, "token": token, "ttl_seconds": ttl_seconds}))


@app.command("revoke-session")
def revoke_session(session_id: str) -> None:
    IdentityStore(get_settings()).revoke_session(session_id)
    typer.echo(json.dumps({"session_id": session_id, "revoked": True}))


@app.command("assign-document")
def assign_document(document_id: str, workspace_id: str, owner_id: str) -> None:
    """Assign an existing local document explicitly; never silently adopt the whole library."""
    settings = get_settings()
    Catalog(settings)
    IdentityStore(settings).assign_document(document_id, workspace_id, owner_id)
    typer.echo(json.dumps({"document_id": document_id, "workspace_id": workspace_id}))


@app.command("unassign-document")
def unassign_document(document_id: str, workspace_id: str) -> None:
    IdentityStore(get_settings()).unassign_document(document_id, workspace_id)
    typer.echo(
        json.dumps({"document_id": document_id, "workspace_id": workspace_id, "unassigned": True})
    )
