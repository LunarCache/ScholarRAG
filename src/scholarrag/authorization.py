"""Request/task-local authorization; no ambient global tenant or client identity."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from scholarrag.config import Settings
from scholarrag.domain.access import AccessContext, AccessDenied
from scholarrag.rag.scope import DocumentScope
from scholarrag.storage.identity import IdentityStore


@dataclass(frozen=True)
class AuthorizationGuard:
    identities: IdentityStore
    context: AccessContext
    document_ids: tuple[str, ...]

    def check(self) -> None:
        self.identities.authorize(self.context, "query")
        allowed = set(self.identities.document_ids(self.context))
        if not set(self.document_ids).issubset(allowed):
            raise AccessDenied("Query document authorization changed")


class ExecutionGuard(Protocol):
    identities: IdentityStore
    context: AccessContext
    document_ids: tuple[str, ...]

    def check(self) -> None: ...


_guard: ContextVar[ExecutionGuard | None] = ContextVar("scholarrag_authorization", default=None)


def current_guard(settings: Settings) -> ExecutionGuard | None:
    guard = _guard.get()
    if settings.auth_mode == "session" and guard is None:
        raise AccessDenied("Workspace execution requires authorization")
    if guard is not None:
        guard.check()
    return guard


def check_active_authorization() -> None:
    guard = _guard.get()
    if guard is not None:
        guard.check()


def authorized_documents(settings: Settings, requested: list[str] | None) -> list[str] | None:
    guard = current_guard(settings)
    if guard is None:
        return requested
    return DocumentScope.intersect(list(guard.document_ids)).resolve(requested)


@contextmanager
def bind_authorization(guard: ExecutionGuard | None) -> Iterator[None]:
    token = _guard.set(guard)
    try:
        if guard is not None:
            guard.check()
        yield
    finally:
        _guard.reset(token)
