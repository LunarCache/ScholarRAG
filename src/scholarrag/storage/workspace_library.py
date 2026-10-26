"""Private library operations with transactional authorization and revision checks."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from scholarrag.domain.access import AccessContext, AccessDenied, IdentityConflict
from scholarrag.domain.library import (
    WorkspaceCollection,
    WorkspaceCollectionRequest,
    WorkspaceCollectionUpdate,
    WorkspacePaperState,
    WorkspaceStateRequest,
)
from scholarrag.storage.identity import IdentityStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkspaceLibrary:
    def __init__(self, identities: IdentityStore) -> None:
        self.identities = identities

    @staticmethod
    def _require_documents(connection, context: AccessContext, document_ids: list[str]) -> None:
        if not set(document_ids).issubset(IdentityStore.active_document_ids(connection, context)):
            raise AccessDenied("Document unavailable")

    @staticmethod
    def _state(context: AccessContext, document_id: str, row) -> WorkspacePaperState:
        if row is None or row["deleted"]:
            return WorkspacePaperState(
                workspace_id=context.workspace_id,
                owner_id=context.user_id,
                document_id=document_id,
                revision=row["revision"] if row else 0,
                updated_at=row["updated_at"] if row else None,
            )
        return WorkspacePaperState(
            **{
                key: row[key]
                for key in (
                    "workspace_id",
                    "owner_id",
                    "document_id",
                    "reading_status",
                    "note",
                    "revision",
                    "updated_at",
                )
            },
            tags=json.loads(row["tags"]),
            favorite=bool(row["favorite"]),
        )

    @staticmethod
    def _state_row(connection, context, document_id):
        return connection.execute(
            "SELECT * FROM auth_personal_paper_state WHERE workspace_id=? AND owner_id=? AND document_id=?",
            (context.workspace_id, context.user_id, document_id),
        ).fetchone()

    def get_state(self, context: AccessContext, document_id: str) -> WorkspacePaperState:
        with self.identities.authorized_transaction(context, "read") as connection:
            self._require_documents(connection, context, [document_id])
            return self._state(
                context, document_id, self._state_row(connection, context, document_id)
            )

    def save_state(
        self, context: AccessContext, document_id: str, body: WorkspaceStateRequest
    ) -> WorkspacePaperState:
        with self.identities.authorized_transaction(
            context, "write_personal", write=True
        ) as connection:
            self._require_documents(connection, context, [document_id])
            row = self._state_row(connection, context, document_id)
            revision = row["revision"] if row else 0
            if body.expected_revision != revision:
                raise IdentityConflict("Reading state changed; reload before saving")
            connection.execute(
                """INSERT INTO auth_personal_paper_state
                   (workspace_id,owner_id,document_id,reading_status,tags,note,favorite,revision,updated_at,deleted)
                   VALUES(?,?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(workspace_id,owner_id,document_id) DO UPDATE SET
                   reading_status=excluded.reading_status,tags=excluded.tags,note=excluded.note,
                   favorite=excluded.favorite,revision=excluded.revision,updated_at=excluded.updated_at,deleted=0""",
                (
                    context.workspace_id,
                    context.user_id,
                    document_id,
                    body.reading_status,
                    json.dumps(list(dict.fromkeys(body.tags)), ensure_ascii=False),
                    body.note,
                    int(body.favorite),
                    revision + 1,
                    _now(),
                ),
            )
            self.identities._audit(
                connection,
                context.user_id,
                context.workspace_id,
                "personal_state.saved",
                document_id,
            )
            return self._state(
                context, document_id, self._state_row(connection, context, document_id)
            )

    def delete_state(self, context: AccessContext, document_id: str) -> None:
        with self.identities.authorized_transaction(context, "read", write=True) as connection:
            # A user may erase their own note even after document access has been revoked.
            row = self._state_row(connection, context, document_id)
            if row is None or row["deleted"]:
                return
            connection.execute(
                """UPDATE auth_personal_paper_state SET note='',tags='[]',favorite=0,
                   reading_status='unread',deleted=1,revision=revision+1,updated_at=?
                   WHERE workspace_id=? AND owner_id=? AND document_id=?""",
                (_now(), context.workspace_id, context.user_id, document_id),
            )
            self.identities._audit(
                connection,
                context.user_id,
                context.workspace_id,
                "personal_state.deleted",
                document_id,
            )

    @staticmethod
    def _owned_collection(
        connection, context: AccessContext, collection_id: str, *, include_deleted=False
    ):
        row = connection.execute(
            "SELECT * FROM auth_collections WHERE collection_id=? AND workspace_id=? AND owner_id=?",
            (collection_id, context.workspace_id, context.user_id),
        ).fetchone()
        if row is None or (row["deleted"] and not include_deleted):
            raise AccessDenied("Collection unavailable")
        return row

    @staticmethod
    def _collection(connection, context, row, allowed: set[str]) -> WorkspaceCollection:
        ids = [
            value[0]
            for value in connection.execute(
                "SELECT document_id FROM auth_collection_documents WHERE collection_id=? ORDER BY position",
                (row["collection_id"],),
            )
        ]
        return WorkspaceCollection(
            **{
                key: row[key]
                for key in (
                    "collection_id",
                    "workspace_id",
                    "owner_id",
                    "name",
                    "revision",
                    "created_at",
                    "updated_at",
                )
            },
            document_ids=[document_id for document_id in ids if document_id in allowed],
            unavailable_document_count=sum(document_id not in allowed for document_id in ids),
        )

    def get_collection(self, context: AccessContext, collection_id: str) -> WorkspaceCollection:
        with self.identities.authorized_transaction(context, "read") as connection:
            row = self._owned_collection(connection, context, collection_id)
            return self._collection(
                connection,
                context,
                row,
                set(self.identities.active_document_ids(connection, context)),
            )

    def list_collections(
        self, context: AccessContext, *, limit: int = 50
    ) -> list[WorkspaceCollection]:
        with self.identities.authorized_transaction(context, "read") as connection:
            rows = connection.execute(
                """SELECT * FROM auth_collections WHERE workspace_id=? AND owner_id=? AND deleted=0
                   ORDER BY updated_at DESC,collection_id DESC LIMIT ?""",
                (context.workspace_id, context.user_id, limit),
            ).fetchall()
            allowed = set(self.identities.active_document_ids(connection, context))
            return [self._collection(connection, context, row, allowed) for row in rows]

    def save_collection(
        self,
        context: AccessContext,
        body: WorkspaceCollectionRequest,
        *,
        collection_id: str | None = None,
    ) -> WorkspaceCollection:
        with self.identities.authorized_transaction(
            context, "write_personal", write=True
        ) as connection:
            now = _now()
            if collection_id is not None:
                row = self._owned_collection(connection, context, collection_id)
                if (
                    not isinstance(body, WorkspaceCollectionUpdate)
                    or body.expected_revision != row["revision"]
                ):
                    raise IdentityConflict("Collection changed; reload before saving")
                revision, created_at = row["revision"] + 1, row["created_at"]
            else:
                collection_id = f"col_{uuid.uuid4().hex}"
                revision, created_at = 1, now
            document_ids = list(dict.fromkeys(body.document_ids))
            self._require_documents(connection, context, document_ids)
            connection.execute(
                """INSERT INTO auth_collections
                   (collection_id,workspace_id,owner_id,name,revision,created_at,updated_at,deleted)
                   VALUES(?,?,?,?,?,?,?,0) ON CONFLICT(collection_id) DO UPDATE SET
                   name=excluded.name,revision=excluded.revision,updated_at=excluded.updated_at""",
                (
                    collection_id,
                    context.workspace_id,
                    context.user_id,
                    body.name,
                    revision,
                    created_at,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM auth_collection_documents WHERE collection_id=?", (collection_id,)
            )
            connection.executemany(
                "INSERT INTO auth_collection_documents VALUES(?,?,?)",
                [
                    (collection_id, document_id, index)
                    for index, document_id in enumerate(document_ids)
                ],
            )
            self.identities._audit(
                connection, context.user_id, context.workspace_id, "collection.saved", collection_id
            )
            return self._collection(
                connection,
                context,
                self._owned_collection(connection, context, collection_id),
                set(document_ids),
            )

    def delete_collection(self, context: AccessContext, collection_id: str) -> None:
        with self.identities.authorized_transaction(context, "read", write=True) as connection:
            row = self._owned_collection(connection, context, collection_id, include_deleted=True)
            if row["deleted"]:
                return
            connection.execute(
                "DELETE FROM auth_collection_documents WHERE collection_id=?", (collection_id,)
            )
            connection.execute(
                "UPDATE auth_collections SET name='',deleted=1,revision=revision+1,updated_at=? WHERE collection_id=?",
                (_now(), collection_id),
            )
            self.identities._audit(
                connection,
                context.user_id,
                context.workspace_id,
                "collection.deleted",
                collection_id,
            )
