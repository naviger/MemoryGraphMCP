from __future__ import annotations

from typing import Protocol

from app.domain.models import GraphTraversalRequest, MemorySearchHit, MemorySearchRequest, RelatedEntity


class MemoryRepository(Protocol):
    def search_documents(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def semantic_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def hybrid_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def find_related_entities(self, request: GraphTraversalRequest) -> list[RelatedEntity]:
        ...
