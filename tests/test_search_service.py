from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("anyio")

from app.domain.models import (
    GraphTraversalRequest,
    MemoryDocument,
    MemorySearchHit,
    MemorySearchRequest,
    RelatedEntity,
)
from app.services.search_service import MemorySearchService


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def search_documents(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        self.calls.append(("search_documents", request))
        document = MemoryDocument(kind=request.kinds[0] if request.kinds else "memory", title="Example", content="Body")
        return [MemorySearchHit(document=document, score=0.8)]

    def semantic_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        self.calls.append(("semantic_search", request))
        document = MemoryDocument(kind="memory", title="Semantic", content="Result")
        return [MemorySearchHit(document=document, score=0.95)]

    def hybrid_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        self.calls.append(("hybrid_search", request))
        document = MemoryDocument(kind="memory", title="Hybrid", content="Result")
        return [MemorySearchHit(document=document, score=0.9)]

    def find_related_entities(self, request: GraphTraversalRequest) -> list[RelatedEntity]:
        self.calls.append(("find_related_entities", request))
        return [RelatedEntity(entity_id="memory-1", label="Related", relation="references", depth=request.depth)]


@pytest.mark.asyncio
async def test_search_architecture_scopes_to_kind() -> None:
    repository = FakeRepository()
    service = MemorySearchService(repository)

    results = await service.search_architecture("FastAPI", limit=5)

    assert results[0].document.kind == "architecture"
    name, request = repository.calls[0]
    assert name == "search_documents"
    assert request.kinds == ["architecture"]
    assert request.limit == 5


@pytest.mark.asyncio
async def test_related_entities_delegate_to_repository() -> None:
    repository = FakeRepository()
    service = MemorySearchService(repository)

    results = await service.find_related_entities("memory-1", depth=3, limit=7)

    assert results[0].entity_id == "memory-1"
    name, request = repository.calls[0]
    assert name == "find_related_entities"
    assert request.seed_entity_id == "memory-1"
    assert request.depth == 3
    assert request.limit == 7


@pytest.mark.asyncio
async def test_semantic_search_falls_back_when_no_vectors_match_the_query_model():
    class Repository(FakeRepository):
        def semantic_search(self, request):
            assert request.embedding_provider == "deterministic"
            assert request.embedding_model == "deterministic-hash"
            assert request.embedding_version == "1"
            assert request.as_of == date(2026, 11, 21)
            return []

    repository = Repository()
    hits = await MemorySearchService(repository).semantic_search("example", as_of=date(2026, 11, 21))

    assert repository.calls[0][0] == "search_documents"
    assert repository.calls[0][1].as_of == date(2026, 11, 21)
    assert hits[0].retrieval_mode == "lexical"
    assert hits[0].fallback_used is True
    assert hits[0].embedding_provider_used is None
