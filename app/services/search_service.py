from __future__ import annotations

from functools import partial

import anyio

from app.domain.models import GraphTraversalRequest, MemorySearchHit, MemorySearchRequest, MemoryKind, RelatedEntity
from app.repositories.protocol import MemoryRepository


class MemorySearchService:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def search_memory(
        self,
        query: str,
        limit: int = 10,
        tags: list[str] | None = None,
    ) -> list[MemorySearchHit]:
        request = MemorySearchRequest(query=query, limit=limit, tags=tags or [], mode="lexical")
        return await anyio.to_thread.run_sync(partial(self.repository.search_documents, request))

    async def search_architecture(self, query: str, limit: int = 10) -> list[MemorySearchHit]:
        return await self._search_by_kind(query=query, kind="architecture", limit=limit)

    async def search_adrs(self, query: str, limit: int = 10) -> list[MemorySearchHit]:
        return await self._search_by_kind(query=query, kind="adr", limit=limit)

    async def search_design_language(self, query: str, limit: int = 10) -> list[MemorySearchHit]:
        return await self._search_by_kind(query=query, kind="design-language", limit=limit)

    async def search_domain_concepts(self, query: str, limit: int = 10) -> list[MemorySearchHit]:
        return await self._search_by_kind(query=query, kind="domain-concept", limit=limit)

    async def semantic_search(
        self,
        query: str,
        limit: int = 10,
        embedding: list[float] | None = None,
    ) -> list[MemorySearchHit]:
        request = MemorySearchRequest(query=query, limit=limit, mode="semantic", embedding=embedding)
        return await anyio.to_thread.run_sync(partial(self.repository.semantic_search, request))

    async def hybrid_search(
        self,
        query: str,
        limit: int = 10,
        embedding: list[float] | None = None,
    ) -> list[MemorySearchHit]:
        request = MemorySearchRequest(query=query, limit=limit, mode="hybrid", embedding=embedding)
        return await anyio.to_thread.run_sync(partial(self.repository.hybrid_search, request))

    async def find_related_entities(
        self,
        entity_id: str,
        depth: int = 2,
        limit: int = 20,
    ) -> list[RelatedEntity]:
        request = GraphTraversalRequest(seed_entity_id=entity_id, depth=depth, limit=limit)
        return await anyio.to_thread.run_sync(partial(self.repository.find_related_entities, request))

    async def _search_by_kind(self, query: str, kind: MemoryKind, limit: int) -> list[MemorySearchHit]:
        request = MemorySearchRequest(query=query, limit=limit, kinds=[kind], mode="lexical")
        return await anyio.to_thread.run_sync(partial(self.repository.search_documents, request))
