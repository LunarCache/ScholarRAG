"""Shared supervision for lease-fenced query and evaluation workers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from scholarrag.domain.access import AccessDenied
from scholarrag.rag.runs import LeaseLost

logger = logging.getLogger(__name__)


class LeasedWorker:
    def __init__(
        self, runtime, *, kind: str, poll_seconds: float, lease_seconds: float, concurrency: int
    ):
        self.runtime = runtime
        self.settings = runtime.settings
        self.worker_id = f"{kind}_worker_{uuid.uuid4().hex}"
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.concurrency = concurrency
        self.active: dict[str, asyncio.Task] = {}
        self.stores: dict[str, Any] = {}

    def claim(self):
        raise NotImplementedError

    def identifier(self, item):
        raise NotImplementedError

    def validate(self, item):
        pass

    async def _execute(self, item, store):
        raise NotImplementedError

    async def _heartbeat(self, store, run_id: str, task: asyncio.Task, item) -> None:
        while not task.done():
            await asyncio.sleep(
                min(
                    self.settings.authorization_poll_seconds,
                    self.lease_seconds / 3,
                )
            )
            try:
                current = store.get(run_id)
                if current is None or current.status in {"completed", "failed", "cancelled"}:
                    return
                self.validate(item)
                cancel = store.heartbeat(run_id)
            except AccessDenied:
                task.cancel()
                return
            except LeaseLost:
                store.suspended = True
                task.cancel()
                return
            except Exception:
                logger.exception("Worker heartbeat failed: %s", run_id)
                store.suspended = True
                task.cancel()
                return
            if cancel:
                task.cancel()
                return

    async def _process(self, item, store) -> None:
        task = asyncio.create_task(self._execute(item, store))
        heartbeat = asyncio.create_task(self._heartbeat(store, self.identifier(item), task, item))
        try:
            await asyncio.shield(task)
        except LeaseLost:
            # Another worker or a queued cancellation owns the authoritative state.
            pass
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                store.suspended = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def tick(self) -> None:
        for run_id, task in list(self.active.items()):
            if task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Leased worker task failed: %s", run_id)
                self.active.pop(run_id)
                self.stores.pop(run_id)
        while len(self.active) < self.concurrency:
            claimed = self.claim()
            if claimed is None:
                break
            item, store = claimed
            key = self.identifier(item)
            if key in self.active:
                # A long event-loop stall may expire our own lease. Stop that
                # fenced executor before replacing its tracked task.
                self.stores[key].suspended = True
                previous = self.active.pop(key)
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            self.stores[key] = store
            self.active[key] = asyncio.create_task(self._process(item, store))

    async def run(self) -> None:
        try:
            while True:
                await self.tick()
                await asyncio.sleep(self.poll_seconds)
        finally:
            # Preserve queued/running snapshots on worker shutdown. Expired leases
            # recover through checkpoints, never through a blind model retry.
            for store in self.stores.values():
                store.suspended = True
            for task in self.active.values():
                task.cancel()
            await asyncio.gather(*self.active.values(), return_exceptions=True)
