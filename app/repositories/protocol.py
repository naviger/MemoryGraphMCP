from __future__ import annotations

from datetime import date
from typing import Protocol

from app.domain.models import GraphTraversalRequest, MemoryDocument, MemorySearchHit, MemorySearchRequest, RelatedEntity

from app.domain.project_guidance import GuidanceDecision


class MemoryRepository(Protocol):
    def read_document(self, document_key: str) -> MemoryDocument | None: ...

    def read_guidance_decisions(self, keys: list[str], as_of: date) -> list[GuidanceDecision]: ...

    def search_documents(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def semantic_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def hybrid_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def find_related_entities(self, request: GraphTraversalRequest) -> list[RelatedEntity]:
        ...
