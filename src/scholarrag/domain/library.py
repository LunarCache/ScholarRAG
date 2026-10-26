"""Bounded payloads for private workspace reading state and collections."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
DocumentId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
CollectionName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


class WorkspaceStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reading_status: Literal["unread", "reading", "finished"] = "unread"
    tags: list[Tag] = Field(default_factory=list, max_length=64)
    note: str = Field(default="", max_length=20_000)
    favorite: bool = False
    expected_revision: int = Field(default=0, ge=0)


class WorkspacePaperState(BaseModel):
    workspace_id: str
    owner_id: str
    document_id: str
    reading_status: Literal["unread", "reading", "finished"] = "unread"
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    favorite: bool = False
    revision: int = 0
    updated_at: str | None = None


class WorkspaceCollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: CollectionName
    document_ids: list[DocumentId] = Field(default_factory=list, max_length=1000)


class WorkspaceCollectionUpdate(WorkspaceCollectionRequest):
    expected_revision: int = Field(ge=1)


class WorkspaceCollection(BaseModel):
    collection_id: str
    workspace_id: str
    owner_id: str
    name: str
    document_ids: list[str]
    unavailable_document_count: int = 0
    revision: int
    created_at: str
    updated_at: str
