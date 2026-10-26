"""Document selection boundary; authenticated workspace grants can narrow it later."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentScope:
    document_ids: frozenset[str] | None

    @classmethod
    def intersect(
        cls, selected: list[str] | None, authorized: list[str] | None = None
    ) -> DocumentScope:
        selection = frozenset(selected) if selected is not None else None
        grants = frozenset(authorized) if authorized is not None else None
        if selection is None:
            return cls(grants)
        return cls(selection if grants is None else selection & grants)

    def allows(self, document_id: str) -> bool:
        return self.document_ids is None or document_id in self.document_ids

    def resolve(self, requested: list[str] | None = None) -> list[str] | None:
        if requested is None:
            return sorted(self.document_ids) if self.document_ids is not None else None
        if not isinstance(requested, list) or any(not isinstance(x, str) for x in requested):
            raise ValueError("document_ids must be a list of strings")
        if any(not self.allows(document_id) for document_id in requested):
            raise PermissionError("Requested documents are outside the query scope")
        return sorted(set(requested))
