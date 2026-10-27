"""Private ingestion admission and lease-fenced execution for independent workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scholarrag.authorization import bind_authorization
from scholarrag.config import Settings
from scholarrag.domain.access import AccessContext, AccessDenied, IdentityConflict, Principal
from scholarrag.domain.models import CanonicalDocument, Chunk
from scholarrag.ingestion.checkpoints import IngestionJournal, IngestionUncertain
from scholarrag.ingestion.service import IngestionService
from scholarrag.knowledge.service import MetadataExtractor
from scholarrag.observability import Telemetry
from scholarrag.rag.runs import LeaseLost
from scholarrag.storage.artifacts import ArtifactStore
from scholarrag.storage.catalog import Catalog
from scholarrag.storage.identity import IdentityStore

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class IngestionGuard:
    identities: IdentityStore
    context: AccessContext
    job_id: str
    lease_token: str | None
    suspended: Callable[[], bool] = lambda: False
    # An upload grants no retrieval access, even while embedding new content.
    document_ids: tuple[str, ...] = ()

    def check(self) -> None:
        with self.identities.authorized_transaction(self.context, "upload") as connection:
            row = connection.execute(
                """SELECT * FROM ingestion_jobs
                   WHERE job_id=? AND workspace_id=? AND owner_id=?""",
                (self.job_id, self.context.workspace_id, self.context.user_id),
            ).fetchone()
            if row is not None:
                WorkspaceIngestions.check_lease(row, self.lease_token, self.suspended())
            if (
                row is None
                or row["status"] != "parsing"
                or row["actor_session_id"] != self.context.session_id
            ):
                raise AccessDenied("Ingestion no longer authorized")


class WorkspaceIngestions:
    def __init__(
        self,
        settings: Settings,
        identities: IdentityStore,
        telemetry: Telemetry | None = None,
        *,
        lease_token: str | None = None,
    ) -> None:
        self.settings = settings
        self.identities = identities
        self.telemetry = telemetry or Telemetry(settings)
        self.lease_token = lease_token
        self.suspended = False

    @staticmethod
    def _event(connection, job_id: str, status: str) -> None:
        connection.execute(
            """INSERT INTO ingestion_job_events(job_id,seq,status,timestamp)
               SELECT ?,COALESCE(MAX(seq),0)+1,?,? FROM ingestion_job_events WHERE job_id=?""",
            (job_id, status, _now(), job_id),
        )

    @staticmethod
    def _owned(connection, context: AccessContext, job_id: str):
        row = connection.execute(
            """SELECT * FROM ingestion_jobs
               WHERE job_id=? AND workspace_id=? AND owner_id=?""",
            (job_id, context.workspace_id, context.user_id),
        ).fetchone()
        if row is None:
            raise AccessDenied("Ingestion unavailable")
        return row

    @staticmethod
    def _public(connection, context: AccessContext, row) -> dict:
        result = {
            key: row[key]
            for key in ("job_id", "status", "filename", "error_code", "created_at", "updated_at")
        }
        allowed = IdentityStore.active_document_ids(connection, context)
        visible = row["status"] == "ready" and row["document_id"] in allowed
        result.update(
            document_id=row["document_id"] if visible else None,
            parse_id=row["parse_id"] if visible else None,
        )
        return result

    def enqueue(
        self,
        context: AccessContext,
        *,
        filename: str,
        source_path: Path,
        source_sha256: str,
        idempotency_key: str,
    ) -> tuple[dict, bool]:
        if not 1 <= len(idempotency_key.strip()) <= 128:
            raise ValueError("Idempotency-Key must contain 1–128 characters")
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        with self.identities.authorized_transaction(context, "upload", write=True) as connection:
            existing = connection.execute(
                """SELECT * FROM ingestion_jobs
                   WHERE workspace_id=? AND owner_id=? AND idempotency_hash=?""",
                (context.workspace_id, context.user_id, key_hash),
            ).fetchone()
            if existing is not None:
                if existing["source_sha256"] != source_sha256:
                    raise IdentityConflict("Idempotency key already used for a different PDF")
                return self._public(connection, context, existing), False
            counts = connection.execute(
                """SELECT COUNT(*),COALESCE(SUM(owner_id=?),0) FROM ingestion_jobs
                   WHERE workspace_id=? AND status IN ('queued','parsing')""",
                (context.user_id, context.workspace_id),
            ).fetchone()
            if (
                counts[0] >= self.settings.workspace_ingestion_max_active
                or counts[1] >= self.settings.workspace_ingestion_max_per_user
            ):
                raise IdentityConflict("Workspace ingestion queue capacity exceeded")
            job_id = f"job_{uuid.uuid4().hex}"
            timestamp = _now()
            connection.execute(
                """INSERT INTO ingestion_jobs(
                    job_id,workspace_id,owner_id,actor_session_id,document_id,filename,
                    source_path,source_sha256,idempotency_hash,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    job_id,
                    context.workspace_id,
                    context.user_id,
                    context.session_id,
                    f"doc_{uuid.uuid4().hex}",
                    filename,
                    str(source_path),
                    source_sha256,
                    key_hash,
                    timestamp,
                    timestamp,
                ),
            )
            self._event(connection, job_id, "queued")
            self.identities._audit(
                connection, context.user_id, context.workspace_id, "ingestion.queued", job_id
            )
            result = self._public(connection, context, self._owned(connection, context, job_id))
        return result, True

    def read(self, context: AccessContext, job_id: str) -> dict:
        with self.identities.authorized_transaction(context, "read") as connection:
            return self._public(connection, context, self._owned(connection, context, job_id))

    def list(self, context: AccessContext, *, limit: int = 50) -> list[dict]:
        with self.identities.authorized_transaction(context, "read") as connection:
            rows = connection.execute(
                """SELECT * FROM ingestion_jobs WHERE workspace_id=? AND owner_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (context.workspace_id, context.user_id, max(1, min(limit, 200))),
            ).fetchall()
            return [self._public(connection, context, row) for row in rows]

    def events(self, context: AccessContext, job_id: str) -> list[dict]:
        with self.identities.authorized_transaction(context, "read") as connection:
            self._owned(connection, context, job_id)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT seq,status,timestamp FROM ingestion_job_events WHERE job_id=? ORDER BY seq",
                    (job_id,),
                )
            ]

    def cancel(self, context: AccessContext, job_id: str) -> dict:
        # Downgraded Viewers may still stop their own prior uploads.
        with self.identities.authorized_transaction(context, "read", write=True) as connection:
            row = self._owned(connection, context, job_id)
            if row["status"] in {"queued", "parsing"}:
                self._finish(connection, job_id, "canceled", None)
                self.identities._audit(
                    connection, context.user_id, context.workspace_id, "ingestion.canceled", job_id
                )
            return self._public(connection, context, self._owned(connection, context, job_id))

    @staticmethod
    def check_lease(row, token: str | None, suspended: bool = False) -> None:
        if (
            suspended
            or row is None
            or not token
            or row["lease_token"] != token
            or (row["lease_until"] or 0) <= time.time()
            or row["status"] != "parsing"
        ):
            raise LeaseLost("Ingestion lease unavailable")

    def claim_next(self, worker_id: str = "manual") -> dict | None:
        with self.identities._transaction() as connection:
            now = time.time()
            row = connection.execute(
                """SELECT * FROM ingestion_jobs WHERE status='queued'
                   OR (status='parsing' AND COALESCE(lease_until,0)<=?)
                   ORDER BY created_at,job_id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE ingestion_jobs SET status='parsing',updated_at=?,lease_token=?,
                   lease_until=?,worker_id=? WHERE job_id=?""",
                (
                    _now(),
                    uuid.uuid4().hex,
                    now + self.settings.ingestion_worker_lease_seconds,
                    worker_id,
                    row["job_id"],
                ),
            )
            if row["status"] == "queued":
                self._event(connection, row["job_id"], "parsing")
            return dict(
                connection.execute(
                    "SELECT * FROM ingestion_jobs WHERE job_id=?",
                    (row["job_id"],),
                ).fetchone()
            )

    def heartbeat(self, job_id: str) -> None:
        with self.identities._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            self.check_lease(row, self.lease_token, self.suspended)
            connection.execute(
                "UPDATE ingestion_jobs SET lease_until=? WHERE job_id=?",
                (time.time() + self.settings.ingestion_worker_lease_seconds, job_id),
            )

    def load_checkpoint(self, job_id: str) -> dict:
        with self.identities._transaction(write=False) as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            self.check_lease(row, self.lease_token, self.suspended)
            return json.loads(row["checkpoint"])

    def save_checkpoint(self, job_id: str, key: str, value: dict) -> None:
        with self.identities._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            self.check_lease(row, self.lease_token, self.suspended)
            checkpoint = json.loads(row["checkpoint"])
            checkpoint[key] = value
            connection.execute(
                "UPDATE ingestion_jobs SET checkpoint=?,updated_at=? WHERE job_id=?",
                (json.dumps(checkpoint), _now(), job_id),
            )

    def _finish(self, connection, job_id: str, status: str, error_code: str | None) -> None:
        changed = connection.execute(
            """UPDATE ingestion_jobs SET status=?,error_code=?,updated_at=?,lease_token=NULL,lease_until=NULL
               WHERE job_id=? AND status IN ('queued','parsing')""",
            (status, error_code, _now(), job_id),
        ).rowcount
        if changed:
            self._event(connection, job_id, status)

    def fail(self, job_id: str, error_code: str) -> None:
        with self.identities._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ingestion_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            self.check_lease(row, self.lease_token, self.suspended)
            self._finish(connection, job_id, "failed", error_code)

    def publish(
        self,
        context: AccessContext,
        job_id: str,
        document_id: str,
        parse_id: str,
        *,
        lease_token: str | None = None,
        publication: tuple[CanonicalDocument, list[Chunk], Path, Path] | None = None,
    ) -> None:
        # Catalog activation + grant + ready state + event + audit commit together.
        catalog = Catalog(self.settings)
        with self.identities.authorized_transaction(context, "upload", write=True) as connection:
            row = self._owned(connection, context, job_id)
            self.check_lease(row, lease_token or self.lease_token, self.suspended)
            if (
                row["status"] != "parsing"
                or row["actor_session_id"] != context.session_id
                or row["document_id"] != document_id
            ):
                raise AccessDenied("Ingestion no longer authorized")
            if publication is not None:
                document, chunks, source, parse_dir = publication
                if document.document_id != document_id or document.parse_id != parse_id:
                    raise IdentityConflict("Publication identity mismatch")
                catalog.register(
                    document,
                    chunks,
                    source_path=source,
                    artifact_dir=parse_dir,
                    external_connection=connection,
                )
                catalog.register_metadata(
                    document_id,
                    parse_id,
                    MetadataExtractor.extract(document).model_dump(mode="json"),
                    external_connection=connection,
                )
            active = connection.execute(
                """SELECT 1 FROM parses p JOIN documents d ON d.document_id=p.document_id
                   WHERE p.document_id=? AND p.parse_id=? AND p.active=1 AND d.source_sha256=?""",
                (document_id, parse_id, row["source_sha256"]),
            ).fetchone()
            if active is None:
                raise IdentityConflict("Ingestion result is not active")
            connection.execute(
                """INSERT INTO auth_resources VALUES('document',?,?,?,'private')""",
                (document_id, context.workspace_id, context.user_id),
            )
            connection.execute(
                "UPDATE ingestion_jobs SET parse_id=? WHERE job_id=?", (parse_id, job_id)
            )
            self._finish(connection, job_id, "ready", None)
            self.identities._audit(
                connection,
                context.user_id,
                context.workspace_id,
                "ingestion.published",
                document_id,
            )

    async def _monitor(self, guard: IngestionGuard, task: asyncio.Task) -> None:
        while not task.done():
            await asyncio.sleep(
                min(
                    self.settings.authorization_poll_seconds,
                    self.settings.ingestion_worker_lease_seconds / 3,
                )
            )
            try:
                guard.check()
                self.heartbeat(guard.job_id)
            except (AccessDenied, LeaseLost):
                task.cancel()
                return
            except Exception:
                logger.exception("Ingestion heartbeat failed: %s", guard.job_id)
                self.suspended = True
                task.cancel()
                return

    async def execute(self, job: dict) -> None:
        if self.lease_token is None:
            executor = WorkspaceIngestions(
                self.settings, self.identities, self.telemetry, lease_token=job["lease_token"]
            )
            await executor.execute(job)
            return
        try:
            await self._execute(job)
        except LeaseLost:
            # Cancellation, expiry or shutdown fenced this executor out.
            return

    async def _execute(self, job: dict) -> None:
        task = asyncio.current_task()
        monitor = None
        try:
            context = self.identities.context_for(
                Principal(job["owner_id"], job["actor_session_id"]), job["workspace_id"]
            )
            guard = IngestionGuard(
                self.identities,
                context,
                job["job_id"],
                self.lease_token,
                suspended=lambda: self.suspended,
            )
            with (
                bind_authorization(guard),
                self.telemetry.trace(
                    "scholarrag.ingestion",
                    trace_id=job["job_id"].removeprefix("job_"),
                    user_id=context.user_id,
                    metadata={"workspace_id": context.workspace_id, "job_id": job["job_id"]},
                ),
            ):
                monitor = asyncio.create_task(self._monitor(guard, task))
                if (
                    await asyncio.to_thread(ArtifactStore.sha256, Path(job["source_path"]))
                    != job["source_sha256"]
                ):
                    raise IdentityConflict("Staged PDF has changed")
                guard.check()
                preserved, _ = await asyncio.to_thread(
                    ArtifactStore(self.settings).preserve_source,
                    Path(job["source_path"]),
                    job["document_id"],
                )
                service = IngestionService(
                    self.settings,
                    document_id=job["document_id"],
                    journal=IngestionJournal(self, job["job_id"]),
                )
                document, chunks, parse_dir = await service.ingest(Path(job["source_path"]))
                self.publish(
                    context,
                    job["job_id"],
                    document.document_id,
                    document.parse_id,
                    publication=(document, chunks, preserved, parse_dir),
                )
                Path(job["source_path"]).unlink(missing_ok=True)
        except IngestionUncertain:
            self.fail(job["job_id"], "external_outcome_unknown")
        except AccessDenied:
            self.fail(job["job_id"], "authorization_revoked")
        except asyncio.CancelledError:
            self.fail(job["job_id"], "execution_stopped")
            raise
        except Exception:  # noqa: BLE001 - persist failure without leaking raw provider errors
            self.fail(job["job_id"], "ingestion_failed")
        finally:
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
