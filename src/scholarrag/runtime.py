"""Service wiring shared by API and workers; importing it does not create an app."""

from __future__ import annotations

import asyncio
from typing import Any

from scholarrag.config import Settings
from scholarrag.evaluation.jobs import EvaluationJobStore
from scholarrag.evaluation.workspaces import WorkspaceEvaluations
from scholarrag.ingestion.review import ParseReviewService
from scholarrag.ingestion.workspaces import WorkspaceIngestions
from scholarrag.observability import Telemetry
from scholarrag.rag.runs import QueryRunStore
from scholarrag.rag.workspaces import WorkspaceQueries
from scholarrag.storage.catalog import Catalog
from scholarrag.storage.identity import IdentityStore
from scholarrag.storage.library import PersonalLibraryStore


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.telemetry = Telemetry(settings)
        self.catalog = Catalog(settings)
        self.identities = IdentityStore(settings)
        self.workspace_ingestions = WorkspaceIngestions(settings, self.identities, self.telemetry)
        self.reviews = ParseReviewService(settings)
        self.query_runs = QueryRunStore(settings)
        self.query_tasks: dict[str, asyncio.Task[Any]] = {}
        self.evaluation_jobs = EvaluationJobStore(settings)
        self.evaluation_tasks: dict[str, asyncio.Task[Any]] = {}
        self.library = PersonalLibraryStore(settings)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.workspace_queries = WorkspaceQueries(settings, self.identities)
        self.workspace_evaluations = WorkspaceEvaluations(
            settings, self.identities, self.workspace_queries
        )
