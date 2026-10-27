"""Lease-fenced ingestion checkpoints, stored privately with the queue entry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scholarrag.ingestion.workspaces import WorkspaceIngestions


class IngestionUncertain(RuntimeError):
    """A paid side effect may have happened; never automatically submit again."""


class IngestionJournal:
    def __init__(self, queue: WorkspaceIngestions, job_id: str) -> None:
        self.queue = queue
        self.job_id = job_id

    def load(self, key: str) -> dict[str, Any] | None:
        return self.queue.load_checkpoint(self.job_id).get(key)

    def save(self, key: str, value: dict[str, Any]) -> None:
        self.queue.save_checkpoint(self.job_id, key, value)
