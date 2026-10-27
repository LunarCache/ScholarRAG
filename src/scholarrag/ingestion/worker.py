"""Independent leased ingestion execution; API processes only admit and read jobs."""

from __future__ import annotations

import asyncio
import logging
import uuid

from scholarrag.ingestion.workspaces import WorkspaceIngestions
from scholarrag.runtime import Runtime

logger = logging.getLogger(__name__)


class IngestionWorker:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        self.worker_id = f"ingestion_worker_{uuid.uuid4().hex}"
        self.active: dict[str, asyncio.Task] = {}
        self.executors: dict[str, WorkspaceIngestions] = {}

    async def tick(self) -> None:
        for job_id, task in list(self.active.items()):
            if task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Ingestion task failed: %s", job_id)
                self.active.pop(job_id)
                self.executors.pop(job_id)
        while len(self.active) < self.settings.ingestion_worker_concurrency:
            job = self.runtime.workspace_ingestions.claim_next(self.worker_id)
            if job is None:
                break
            job_id = job["job_id"]
            if job_id in self.active:
                self.executors[job_id].suspended = True
                previous = self.active.pop(job_id)
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            executor = WorkspaceIngestions(
                self.settings,
                self.runtime.identities,
                self.runtime.telemetry,
                lease_token=job["lease_token"],
            )
            self.executors[job_id] = executor
            self.active[job_id] = asyncio.create_task(executor.execute(job))

    async def run(self) -> None:
        try:
            while True:
                await self.tick()
                await asyncio.sleep(self.settings.ingestion_worker_poll_seconds)
        finally:
            for executor in self.executors.values():
                executor.suspended = True
            for task in self.active.values():
                task.cancel()
            await asyncio.gather(*self.active.values(), return_exceptions=True)
