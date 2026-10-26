"""Server-created identity and workspace authorization context."""

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    VIEWER = "viewer"
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


@dataclass(frozen=True)
class Principal:
    user_id: str
    session_id: str


@dataclass(frozen=True)
class AccessContext:
    user_id: str
    session_id: str
    workspace_id: str
    role: Role


class AccessDenied(PermissionError):
    pass


class IdentityConflict(ValueError):
    pass
