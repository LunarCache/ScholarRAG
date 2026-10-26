from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from scholarrag.domain.models import Answer
from scholarrag.evaluation.jobs import EvaluationJob
from scholarrag.evaluation.service import EvaluationCase, EvaluationRun
from scholarrag.ingestion.review import ParseReviewResult, ParseVersion
from scholarrag.rag.runs import QueryRun
from scholarrag.storage.library import Collection, PersonalPaperState


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    document_ids: list[str] | None = None
    top_k: int = Field(default=8, ge=1, le=20)
    agentic: bool = True
    # True: tool-calling loop; False: hard-coded multi-step planner;
    # None (default): follow the server-side agent_tool_calling setting.
    tool_mode: bool | None = None
    conversation_id: str | None = None
    parent_run_id: str | None = None


class ParseReviewRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    force: bool = False


class ParseVersionResponse(ParseVersion):
    pass


class ParseReviewResponse(ParseReviewResult):
    pass


class QueryResponse(Answer):
    pass


class QueryRunResponse(QueryRun):
    pass


class EvaluationRequest(EvaluationCase):
    query_run_id: str | None = None


class EvaluationResponse(EvaluationRun):
    pass


class EvaluationJobResponse(EvaluationJob):
    pass


class PersonalPaperStateRequest(BaseModel):
    reading_status: Literal["unread", "reading", "finished"] = "unread"
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    favorite: bool = False


class PersonalPaperStateResponse(PersonalPaperState):
    pass


class CollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    document_ids: list[str] = Field(default_factory=list)


class CollectionResponse(Collection):
    pass


class ParserAnnotationRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=120)
    elements: list[dict] = Field(default_factory=list)
    qa_cases: list[dict] = Field(default_factory=list)
    notes: str = ""


class HumanActorRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=120)


class ParserReopenRequest(HumanActorRequest):
    reason: str = Field(min_length=3, max_length=1000)


class QAReviewRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=120)
    decision: Literal["candidate", "reviewed", "rejected"]
    question: str = Field(min_length=1, max_length=8000)
    reference: str = Field(min_length=1, max_length=20_000)
    tags: list[str] = Field(default_factory=list)
    source_chunk_ids: list[str] = Field(default_factory=list)
    source_block_ids: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=4000)


class JudgeRatingRequest(BaseModel):
    rater: str = Field(min_length=2, max_length=120)
    faithfulness: float = Field(ge=0, le=1)
    answer_relevancy: float = Field(ge=0, le=1)
    citation_correctness: float = Field(ge=0, le=1)
    notes: str = Field(default="", max_length=4000)
