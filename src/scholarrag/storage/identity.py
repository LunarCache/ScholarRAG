"""SQLite identity foundation; no passwords or implicit legacy-data adoption.

Provisioning methods are for a trusted local administrator. HTTP management
uses AccessContext and rechecks the session and membership within each transaction.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from scholarrag.config import Settings
from scholarrag.domain.access import AccessContext, AccessDenied, IdentityConflict, Principal, Role

_PERMISSIONS = {
    "read": set(Role),
    "query": set(Role),
    "upload": {Role.MEMBER, Role.ADMIN, Role.OWNER},
    "write_personal": {Role.MEMBER, Role.ADMIN, Role.OWNER},
    "manage_documents": {Role.ADMIN, Role.OWNER},
    "manage_members": {Role.ADMIN, Role.OWNER},
    "manage_workspace": {Role.OWNER},
}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class IdentityStore:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.sqlite_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            # Explicit migration: discard obsolete single-user tasks and their events.
            # Sources, parses and catalog records are intentionally untouched.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ingestion_jobs)")}
            if columns and "workspace_id" not in columns:
                connection.execute("DROP TABLE IF EXISTS ingestion_job_events")
                connection.execute("DROP TABLE ingestion_jobs")
            # Execute individually so migration and version marker commit together.
            for statement in (
                """CREATE TABLE IF NOT EXISTS auth_schema_versions (
                    version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS auth_users (
                    user_id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
                    display_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(issuer, subject))""",
                """CREATE TABLE IF NOT EXISTS auth_workspaces (
                    workspace_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1)""",
                """CREATE TABLE IF NOT EXISTS auth_memberships (
                    workspace_id TEXT NOT NULL REFERENCES auth_workspaces(workspace_id),
                    user_id TEXT NOT NULL REFERENCES auth_users(user_id),
                    role TEXT NOT NULL CHECK(role IN ('viewer','member','admin','owner')),
                    active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(workspace_id,user_id))""",
                """CREATE UNIQUE INDEX IF NOT EXISTS auth_one_owner
                    ON auth_memberships(workspace_id) WHERE role='owner' AND active=1""",
                """CREATE TABLE IF NOT EXISTS auth_sessions (
                    session_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL REFERENCES auth_users(user_id),
                    expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""",
                """CREATE TABLE IF NOT EXISTS auth_resources (
                    resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL REFERENCES auth_workspaces(workspace_id),
                    owner_id TEXT NOT NULL REFERENCES auth_users(user_id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('private','workspace')),
                    PRIMARY KEY(resource_type,resource_id))""",
                """CREATE INDEX IF NOT EXISTS auth_resource_workspace
                    ON auth_resources(workspace_id, resource_type)""",
                """CREATE TABLE IF NOT EXISTS auth_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL,
                    actor_id TEXT NOT NULL, workspace_id TEXT,
                    action TEXT NOT NULL, target_id TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS auth_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES auth_workspaces(workspace_id),
                    owner_id TEXT NOT NULL REFERENCES auth_users(user_id),
                    created_at INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS auth_query_runs (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES auth_workspaces(workspace_id),
                    owner_id TEXT NOT NULL REFERENCES auth_users(user_id),
                    conversation_id TEXT NOT NULL REFERENCES auth_conversations(conversation_id),
                    actor_session_id TEXT NOT NULL REFERENCES auth_sessions(session_id),
                    document_ids TEXT NOT NULL, created_at INTEGER NOT NULL)""",
                """CREATE INDEX IF NOT EXISTS auth_owned_runs
                    ON auth_query_runs(workspace_id,owner_id,created_at)""",
                """CREATE TABLE IF NOT EXISTS auth_personal_paper_state (
                    workspace_id TEXT NOT NULL REFERENCES auth_workspaces(workspace_id),
                    owner_id TEXT NOT NULL REFERENCES auth_users(user_id), document_id TEXT NOT NULL,
                    reading_status TEXT NOT NULL CHECK(reading_status IN ('unread','reading','finished')),
                    tags TEXT NOT NULL, note TEXT NOT NULL, favorite INTEGER NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision>0), updated_at TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(workspace_id,owner_id,document_id))""",
                """CREATE TABLE IF NOT EXISTS auth_collections (
                    collection_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES auth_workspaces(workspace_id),
                    owner_id TEXT NOT NULL REFERENCES auth_users(user_id), name TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision>0), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)""",
                """CREATE TABLE IF NOT EXISTS auth_collection_documents (
                    collection_id TEXT NOT NULL REFERENCES auth_collections(collection_id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL, position INTEGER NOT NULL,
                    PRIMARY KEY(collection_id,document_id))""",
                """CREATE INDEX IF NOT EXISTS auth_private_collections
                    ON auth_collections(workspace_id,owner_id,deleted,updated_at)""",
                """CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    job_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES auth_workspaces(workspace_id),
                    owner_id TEXT NOT NULL REFERENCES auth_users(user_id),
                    actor_session_id TEXT NOT NULL REFERENCES auth_sessions(session_id),
                    document_id TEXT NOT NULL UNIQUE, parse_id TEXT,
                    filename TEXT NOT NULL, source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                    idempotency_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','parsing','ready','failed','canceled')),
                    error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id,owner_id,idempotency_hash))""",
                """CREATE INDEX IF NOT EXISTS auth_ingestion_admission
                    ON ingestion_jobs(workspace_id,owner_id,status)""",
                """CREATE TABLE IF NOT EXISTS ingestion_job_events (
                    job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id),
                    seq INTEGER NOT NULL, status TEXT NOT NULL, timestamp TEXT NOT NULL,
                    PRIMARY KEY(job_id,seq))""",
            ):
                connection.execute(statement)
            version = connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM auth_schema_versions"
            ).fetchone()[0]
            if version > 4:
                raise IdentityConflict("Unsupported identity schema version")
            connection.execute(
                "INSERT OR IGNORE INTO auth_schema_versions VALUES(1,?)", (int(time.time()),)
            )
            connection.execute(
                "INSERT OR IGNORE INTO auth_schema_versions VALUES(2,?)", (int(time.time()),)
            )
            connection.execute(
                "INSERT OR IGNORE INTO auth_schema_versions VALUES(3,?)", (int(time.time()),)
            )
            connection.execute(
                "INSERT OR IGNORE INTO auth_schema_versions VALUES(4,?)", (int(time.time()),)
            )

    @contextmanager
    def authorized_transaction(
        self,
        context: AccessContext,
        action: str,
        *,
        write: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        """Revalidate authority and perform dependent data operations in the same transaction."""
        with self._transaction(write=write) as connection:
            self._authorize(connection, context, action)
            yield connection

    @staticmethod
    def active_document_ids(connection: sqlite3.Connection, context: AccessContext) -> list[str]:
        return [
            row[0]
            for row in connection.execute(
                """SELECT DISTINCT r.resource_id FROM auth_resources r
               JOIN documents d ON d.document_id=r.resource_id
               JOIN parses p ON p.document_id=d.document_id AND p.active=1
               WHERE r.workspace_id=? AND r.resource_type='document'
               AND (r.visibility='workspace' OR r.owner_id=?) ORDER BY r.resource_id""",
                (context.workspace_id, context.user_id),
            )
        ]

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _audit(connection, actor_id, workspace_id, action, target_id) -> None:
        connection.execute(
            "INSERT INTO auth_audit(timestamp,actor_id,workspace_id,action,target_id) VALUES(?,?,?,?,?)",
            (int(time.time()), actor_id, workspace_id, action, target_id),
        )

    def create_user(self, display_name: str, *, issuer: str, subject: str) -> str:
        if not all(value.strip() for value in (display_name, issuer, subject)):
            raise ValueError("Display name, issuer and subject are required")
        user_id = _id("usr")
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO auth_users(user_id,issuer,subject,display_name) VALUES(?,?,?,?)",
                    (user_id, issuer, subject, display_name.strip()),
                )
                self._audit(connection, "local-admin", None, "user.created", user_id)
        except sqlite3.IntegrityError as exc:
            raise IdentityConflict("Identity already registered") from exc
        return user_id

    def create_workspace(self, owner_id: str, name: str) -> str:
        if not name.strip():
            raise ValueError("Workspace name is required")
        workspace_id = _id("ws")
        with self._transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM auth_users WHERE user_id=? AND active=1", (owner_id,)
            ).fetchone():
                raise IdentityConflict("Owner unavailable")
            connection.execute(
                "INSERT INTO auth_workspaces(workspace_id,name) VALUES(?,?)",
                (workspace_id, name.strip()),
            )
            connection.execute(
                "INSERT INTO auth_memberships VALUES(?,?,?,1)",
                (workspace_id, owner_id, Role.OWNER),
            )
            self._audit(connection, "local-admin", workspace_id, "workspace.created", owner_id)
        return workspace_id

    def issue_session(self, user_id: str, *, ttl_seconds: int = 28800) -> tuple[str, str]:
        if not 60 <= ttl_seconds <= 30 * 86400:
            raise ValueError("Session lifetime must be between 60 seconds and 30 days")
        token = secrets.token_urlsafe(32)
        session_id = _id("sess")
        with self._transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM auth_users WHERE user_id=? AND active=1", (user_id,)
            ).fetchone():
                raise AccessDenied("User unavailable")
            connection.execute(
                "INSERT INTO auth_sessions VALUES(?,?,?,?,0)",
                (
                    session_id,
                    hashlib.sha256(token.encode()).hexdigest(),
                    user_id,
                    int(time.time()) + ttl_seconds,
                ),
            )
            self._audit(connection, "local-admin", None, "session.issued", session_id)
        return session_id, token

    def revoke_session(self, session_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked=1 WHERE session_id=?", (session_id,)
            )
            self._audit(connection, "local-admin", None, "session.revoked", session_id)

    def revoke_current(self, principal: Principal) -> None:
        with self._transaction() as connection:
            self._principal(connection, principal.session_id, principal.user_id)
            connection.execute(
                "UPDATE auth_sessions SET revoked=1 WHERE session_id=?", (principal.session_id,)
            )
            self._audit(
                connection, principal.user_id, None, "session.revoked", principal.session_id
            )

    @staticmethod
    def _principal(connection, session_id: str, user_id: str | None = None) -> Principal:
        row = connection.execute(
            """SELECT s.user_id,s.session_id FROM auth_sessions s
               JOIN auth_users u ON u.user_id=s.user_id
               WHERE s.session_id=? AND s.revoked=0 AND s.expires_at>? AND u.active=1""",
            (session_id, int(time.time())),
        ).fetchone()
        if row is None or (user_id is not None and row["user_id"] != user_id):
            raise AccessDenied("Authentication required")
        return Principal(user_id=row["user_id"], session_id=row["session_id"])

    def authenticate(self, token: str) -> Principal:
        if not token or len(token) > 512:
            raise AccessDenied("Authentication required")
        with self._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT session_id FROM auth_sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if row is None:
                raise AccessDenied("Authentication required")
            return self._principal(connection, row["session_id"])

    @classmethod
    def _context(cls, connection, principal: Principal, workspace_id: str) -> AccessContext:
        cls._principal(connection, principal.session_id, principal.user_id)
        row = connection.execute(
            """SELECT m.role FROM auth_memberships m
               JOIN auth_workspaces w ON w.workspace_id=m.workspace_id
               WHERE m.workspace_id=? AND m.user_id=? AND m.active=1 AND w.active=1""",
            (workspace_id, principal.user_id),
        ).fetchone()
        if row is None:
            raise AccessDenied("Workspace unavailable")
        return AccessContext(
            principal.user_id, principal.session_id, workspace_id, Role(row["role"])
        )

    @classmethod
    def _authorize(cls, connection, context: AccessContext, action: str) -> AccessContext:
        fresh = cls._context(
            connection, Principal(context.user_id, context.session_id), context.workspace_id
        )
        if fresh.role not in _PERMISSIONS.get(action, set()):
            raise AccessDenied("Operation not permitted")
        return fresh

    def context_for(self, principal: Principal, workspace_id: str) -> AccessContext:
        with self._transaction(write=False) as connection:
            return self._context(connection, principal, workspace_id)

    def authorize(self, context: AccessContext, action: str) -> AccessContext:
        with self._transaction(write=False) as connection:
            return self._authorize(connection, context, action)

    def profile(self, principal: Principal) -> dict:
        with self._transaction(write=False) as connection:
            self._principal(connection, principal.session_id, principal.user_id)
            row = connection.execute(
                "SELECT user_id,display_name FROM auth_users WHERE user_id=?", (principal.user_id,)
            ).fetchone()
            return dict(row)

    def list_workspaces(self, principal: Principal) -> list[dict]:
        with self._transaction(write=False) as connection:
            self._principal(connection, principal.session_id, principal.user_id)
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT w.workspace_id,w.name,m.role FROM auth_workspaces w
                   JOIN auth_memberships m ON m.workspace_id=w.workspace_id
                   WHERE m.user_id=? AND m.active=1 AND w.active=1 ORDER BY w.workspace_id""",
                    (principal.user_id,),
                )
            ]

    def list_members(self, context: AccessContext) -> list[dict]:
        with self._transaction(write=False) as connection:
            self._authorize(connection, context, "read")
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT m.user_id,u.display_name,m.role FROM auth_memberships m
                   JOIN auth_users u ON u.user_id=m.user_id
                   WHERE m.workspace_id=? AND m.active=1 AND u.active=1 ORDER BY m.user_id""",
                    (context.workspace_id,),
                )
            ]

    def set_member(self, context: AccessContext, user_id: str, role: Role) -> None:
        role = Role(role)
        with self._transaction() as connection:
            actor = self._authorize(connection, context, "manage_members")
            target = connection.execute(
                "SELECT role FROM auth_memberships WHERE workspace_id=? AND user_id=? AND active=1",
                (context.workspace_id, user_id),
            ).fetchone()
            if role == Role.OWNER or (target and target["role"] == Role.OWNER):
                raise IdentityConflict("Use ownership transfer to change the owner")
            if actor.role != Role.OWNER and (
                role == Role.ADMIN or (target and target["role"] == Role.ADMIN)
            ):
                raise AccessDenied("Only the owner can manage administrators")
            if not connection.execute(
                "SELECT 1 FROM auth_users WHERE user_id=? AND active=1", (user_id,)
            ).fetchone():
                raise IdentityConflict("User unavailable")
            connection.execute(
                """INSERT INTO auth_memberships VALUES(?,?,?,1)
                   ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=excluded.role,active=1""",
                (context.workspace_id, user_id, role),
            )
            self._audit(
                connection, actor.user_id, context.workspace_id, f"member.set.{role}", user_id
            )

    def remove_member(self, context: AccessContext, user_id: str) -> None:
        with self._transaction() as connection:
            actor = self._authorize(connection, context, "manage_members")
            target = connection.execute(
                "SELECT role FROM auth_memberships WHERE workspace_id=? AND user_id=? AND active=1",
                (context.workspace_id, user_id),
            ).fetchone()
            if target and target["role"] == Role.OWNER:
                raise IdentityConflict("Transfer ownership before removing the owner")
            if actor.role != Role.OWNER and target and target["role"] == Role.ADMIN:
                raise AccessDenied("Only the owner can manage administrators")
            connection.execute(
                "UPDATE auth_memberships SET active=0 WHERE workspace_id=? AND user_id=?",
                (context.workspace_id, user_id),
            )
            self._audit(connection, actor.user_id, context.workspace_id, "member.removed", user_id)

    def transfer_owner(self, context: AccessContext, user_id: str) -> None:
        with self._transaction() as connection:
            actor = self._authorize(connection, context, "manage_workspace")
            if actor.user_id == user_id:
                return
            target = connection.execute(
                """SELECT 1 FROM auth_memberships m JOIN auth_users u ON u.user_id=m.user_id
                   WHERE m.workspace_id=? AND m.user_id=? AND m.active=1 AND u.active=1""",
                (context.workspace_id, user_id),
            ).fetchone()
            if target is None:
                raise IdentityConflict("New owner must be an active member")
            connection.execute(
                "UPDATE auth_memberships SET role='admin' WHERE workspace_id=? AND user_id=?",
                (context.workspace_id, actor.user_id),
            )
            connection.execute(
                "UPDATE auth_memberships SET role='owner' WHERE workspace_id=? AND user_id=?",
                (context.workspace_id, user_id),
            )
            self._audit(
                connection, actor.user_id, context.workspace_id, "ownership.transferred", user_id
            )

    def assign_document(self, document_id: str, workspace_id: str, owner_id: str) -> None:
        """Trusted local migration operation, never exposed as an arbitrary HTTP claim."""
        with self._transaction() as connection:
            if not connection.execute(
                """SELECT 1 FROM auth_memberships m JOIN auth_users u ON u.user_id=m.user_id
                   JOIN auth_workspaces w ON w.workspace_id=m.workspace_id
                   WHERE m.workspace_id=? AND m.user_id=? AND m.active=1 AND u.active=1 AND w.active=1""",
                (workspace_id, owner_id),
            ).fetchone():
                raise IdentityConflict("Document owner must be an active workspace member")
            if not connection.execute(
                "SELECT 1 FROM documents WHERE document_id=?", (document_id,)
            ).fetchone():
                raise IdentityConflict("Document unavailable")
            existing = connection.execute(
                "SELECT workspace_id,owner_id FROM auth_resources WHERE resource_type='document' AND resource_id=?",
                (document_id,),
            ).fetchone()
            if existing:
                if (existing["workspace_id"], existing["owner_id"]) == (workspace_id, owner_id):
                    return
                raise IdentityConflict("Document already assigned; explicit migration required")
            connection.execute(
                "INSERT INTO auth_resources VALUES('document',?,?,?,'workspace')",
                (document_id, workspace_id, owner_id),
            )
            self._audit(connection, "local-admin", workspace_id, "document.assigned", document_id)

    def document_ids(self, context: AccessContext) -> list[str]:
        with self.authorized_transaction(context, "read") as connection:
            return self.active_document_ids(connection, context)

    def unassign_document(self, document_id: str, workspace_id: str) -> None:
        """Trusted local rollback of ownership mapping; original artifacts remain intact."""
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT workspace_id FROM auth_resources WHERE resource_type='document' AND resource_id=?",
                (document_id,),
            ).fetchone()
            if existing and existing["workspace_id"] != workspace_id:
                raise IdentityConflict("Document belongs to a different workspace")
            connection.execute(
                "DELETE FROM auth_resources WHERE resource_type='document' AND resource_id=? AND workspace_id=?",
                (document_id, workspace_id),
            )
            self._audit(connection, "local-admin", workspace_id, "document.unassigned", document_id)

    def create_conversation(self, context: AccessContext) -> str:
        conversation_id = _id("conv")
        with self._transaction() as connection:
            self._authorize(connection, context, "query")
            connection.execute(
                "INSERT INTO auth_conversations VALUES(?,?,?,?)",
                (conversation_id, context.workspace_id, context.user_id, int(time.time())),
            )
            self._audit(
                connection,
                context.user_id,
                context.workspace_id,
                "conversation.created",
                conversation_id,
            )
        return conversation_id

    @classmethod
    def _conversation(cls, connection, context: AccessContext, conversation_id: str) -> None:
        cls._authorize(connection, context, "read")
        if not connection.execute(
            "SELECT 1 FROM auth_conversations WHERE conversation_id=? AND workspace_id=? AND owner_id=?",
            (conversation_id, context.workspace_id, context.user_id),
        ).fetchone():
            raise AccessDenied("Conversation unavailable")

    def require_conversation(self, context: AccessContext, conversation_id: str) -> None:
        with self._transaction(write=False) as connection:
            self._conversation(connection, context, conversation_id)

    def register_query_run(self, context: AccessContext, run) -> None:
        with self._transaction() as connection:
            self._authorize(connection, context, "query")
            self._conversation(connection, context, run.conversation_id)
            if (run.workspace_id, run.owner_id, run.actor_session_id) != (
                context.workspace_id,
                context.user_id,
                context.session_id,
            ) or run.document_ids is None:
                raise AccessDenied("Invalid query ownership")
            allowed = set(self.active_document_ids(connection, context))
            if not set(run.document_ids).issubset(allowed):
                raise AccessDenied("Query documents unavailable")
            connection.execute(
                "INSERT INTO auth_query_runs VALUES(?,?,?,?,?,?,?)",
                (
                    run.run_id,
                    context.workspace_id,
                    context.user_id,
                    run.conversation_id,
                    context.session_id,
                    json.dumps(run.document_ids),
                    int(time.time()),
                ),
            )
            self._audit(
                connection, context.user_id, context.workspace_id, "query.created", run.run_id
            )

    def query_record(self, context: AccessContext, run_id: str) -> dict:
        with self._transaction(write=False) as connection:
            self._authorize(connection, context, "read")
            row = connection.execute(
                "SELECT * FROM auth_query_runs WHERE run_id=? AND workspace_id=? AND owner_id=?",
                (run_id, context.workspace_id, context.user_id),
            ).fetchone()
            if row is None:
                raise AccessDenied("Query run unavailable")
            return {**dict(row), "document_ids": json.loads(row["document_ids"])}

    def owned_query_ids(
        self, context: AccessContext, *, limit: int = 50, conversation_id: str | None = None
    ) -> list[str]:
        with self._transaction(write=False) as connection:
            self._authorize(connection, context, "read")
            if conversation_id is not None:
                self._conversation(connection, context, conversation_id)
            return [
                row[0]
                for row in connection.execute(
                    """SELECT run_id FROM auth_query_runs WHERE workspace_id=? AND owner_id=?
                   AND (? IS NULL OR conversation_id=?) ORDER BY created_at DESC,run_id DESC LIMIT ?""",
                    (
                        context.workspace_id,
                        context.user_id,
                        conversation_id,
                        conversation_id,
                        limit,
                    ),
                )
            ]

    def owned_conversations(self, context: AccessContext) -> list[dict]:
        with self._transaction(write=False) as connection:
            self._authorize(connection, context, "read")
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT c.conversation_id,c.created_at,COUNT(q.run_id) AS run_count
                   FROM auth_conversations c LEFT JOIN auth_query_runs q ON q.conversation_id=c.conversation_id
                   WHERE c.workspace_id=? AND c.owner_id=? GROUP BY c.conversation_id
                   ORDER BY c.created_at DESC,c.conversation_id DESC""",
                    (context.workspace_id, context.user_id),
                )
            ]

    def query_workspaces(self) -> list[str]:
        """Trusted startup recovery inventory; never exposed as a public enumeration API."""
        with self._transaction(write=False) as connection:
            return [
                row[0]
                for row in connection.execute("SELECT DISTINCT workspace_id FROM auth_query_runs")
            ]
