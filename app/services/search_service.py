from __future__ import annotations

import time
from datetime import date
from functools import partial
from typing import Literal

import anyio
import structlog

from app.domain.models import (
    GraphTraversalRequest,
    MemoryKind,
    MemorySearchHit,
    MemorySearchRequest,
    RelatedEntity,
)
from app.repositories.protocol import MemoryRepository
from app.services.embeddings import EmbeddingArtifact, EmbeddingService
from app.services.observability import MemoryObservability

logger = structlog.get_logger(__name__)


class MemorySearchService:
    def __init__(
        self,
        repository: MemoryRepository,
        embedding_service: EmbeddingService | None = None,
        observability: MemoryObservability | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_service = embedding_service or EmbeddingService.default()
        self.observability = observability

    async def search_memory(
        self,
        query: str,
        limit: int = 10,
        tags: list[str] | None = None,
        as_of: date | None = None,
    ) -> list[MemorySearchHit]:
        self._record_request("search_memory")
        request = MemorySearchRequest(query=query, limit=limit, tags=tags or [], mode="lexical", as_of=as_of)
        started = time.perf_counter()
        hits = await anyio.to_thread.run_sync(partial(self.repository.search_documents, request))
        self._record_success("search_memory", started)
        return self._decorate_hits(hits, retrieval_mode="lexical", embedding_artifact=None)

    async def search_architecture(
        self, query: str, limit: int = 10, as_of: date | None = None,
    ) -> list[MemorySearchHit]:
        return await self._search_by_kind(query=query, kind="architecture", limit=limit, as_of=as_of)

    async def search_adrs(
        self, query: str, limit: int = 10, as_of: date | None = None,
    ) -> list[MemorySearchHit]:
        return await self._search_by_kind(query=query, kind="adr", limit=limit, as_of=as_of)

    async def search_design_language(
        self, query: str, limit: int = 10, as_of: date | None = None,
    ) -> list[MemorySearchHit]:
        return await self._search_by_kind(query=query, kind="design-language", limit=limit, as_of=as_of)

    async def search_domain_concepts(
        self, query: str, limit: int = 10, as_of: date | None = None,
    ) -> list[MemorySearchHit]:
        return await self._search_by_kind(query=query, kind="domain-concept", limit=limit, as_of=as_of)

    async def semantic_search(
        self,
        query: str,
        limit: int = 10,
        embedding: list[float] | None = None,
        as_of: date | None = None,
        *,
        kinds: list[MemoryKind] | None = None,
        document_keys: list[str] | None = None,
    ) -> list[MemorySearchHit]:
        return await self._search_with_embedding(
            "semantic", query, limit, embedding, as_of, kinds or [], document_keys
        )

    async def hybrid_search(
        self,
        query: str,
        limit: int = 10,
        embedding: list[float] | None = None,
        as_of: date | None = None,
        *,
        kinds: list[MemoryKind] | None = None,
        document_keys: list[str] | None = None,
    ) -> list[MemorySearchHit]:
        return await self._search_with_embedding(
            "hybrid", query, limit, embedding, as_of, kinds or [], document_keys
        )

    async def _search_with_embedding(
        self,
        mode: Literal["semantic", "hybrid"],
        query: str,
        limit: int,
        embedding: list[float] | None,
        as_of: date | None,
        kinds: list[MemoryKind],
        document_keys: list[str] | None,
    ) -> list[MemorySearchHit]:
        operation = f"{mode}_search"
        self._record_request(operation)
        started = time.perf_counter()
        artifact = None
        if query.strip() or embedding is not None:
            artifact = await anyio.to_thread.run_sync(self._resolve_embedding, query, embedding, operation)

        if artifact is None:
            hits = await self._run_lexical_search(query, limit, as_of, kinds, document_keys)
        else:
            generated = embedding is None
            request = MemorySearchRequest(
                query=query,
                limit=limit,
                mode=mode,
                as_of=as_of,
                kinds=kinds,
                document_keys=document_keys,
                embedding=artifact.vector,
                embedding_provider=artifact.provider if generated else None,
                embedding_model=artifact.model if generated else None,
                embedding_version=artifact.version if generated else None,
            )
            search = self.repository.semantic_search if mode == "semantic" else self.repository.hybrid_search
            hits = await anyio.to_thread.run_sync(search, request)
            if not hits and generated and mode == "semantic":
                hits = await self._run_lexical_search(query, limit, as_of, kinds, document_keys)
                artifact = None

        self._record_success(operation, started)
        if artifact is None:
            self._record_fallback(operation)
            return self._decorate_hits(hits, "lexical", None, fallback_used=True)
        if artifact.fallback_used or any(hit.fallback_used for hit in hits):
            self._record_fallback(operation)
        return self._decorate_hits(hits, mode, artifact)

    async def find_related_entities(
        self,
        entity_id: str,
        depth: int = 2,
        limit: int = 20,
    ) -> list[RelatedEntity]:
        request = GraphTraversalRequest(seed_entity_id=entity_id, depth=depth, limit=limit)
        return await anyio.to_thread.run_sync(partial(self.repository.find_related_entities, request))

    async def _search_by_kind(
        self, query: str, kind: MemoryKind, limit: int, as_of: date | None,
    ) -> list[MemorySearchHit]:
        self._record_request(f"search_{kind}")
        request = MemorySearchRequest(query=query, limit=limit, kinds=[kind], mode="lexical", as_of=as_of)
        started = time.perf_counter()
        hits = await anyio.to_thread.run_sync(partial(self.repository.search_documents, request))
        self._record_success(f"search_{kind}", started)
        return self._decorate_hits(hits, retrieval_mode="lexical", embedding_artifact=None)

    def _resolve_embedding(
        self,
        query: str,
        embedding: list[float] | None,
        purpose: str,
    ) -> EmbeddingArtifact:
        if embedding is not None:
            artifact = self.embedding_service.embed_client_vector(embedding)
            logger.info(
                "embedding_client_vector_used",
                purpose=purpose,
                provider=artifact.provider,
                dimension=artifact.dimension,
            )
            if self.observability is not None:
                self.observability.record_success(f"{purpose}_embedding", 0.0)
            return artifact

        artifact = self.embedding_service.embed_query(query, allow_fallback=True)
        if self.observability is not None:
            self.observability.record_success(f"{purpose}_embedding", artifact.latency_ms / 1000.0)
            if artifact.fallback_used:
                self.observability.record_fallback(f"{purpose}_embedding")
        return artifact

    def _decorate_hits(
        self,
        hits: list[MemorySearchHit],
        retrieval_mode: str,
        embedding_artifact: EmbeddingArtifact | None,
        fallback_used: bool = False,
    ) -> list[MemorySearchHit]:
        decorated: list[MemorySearchHit] = []
        for hit in hits:
            metadata = dict(hit.metadata)
            if embedding_artifact is not None:
                metadata.update(
                    {
                        "embedding_provider": embedding_artifact.provider,
                        "embedding_model": embedding_artifact.model,
                        "embedding_dimension": embedding_artifact.dimension,
                        "embedding_version": embedding_artifact.version,
                        "embedding_latency_ms": embedding_artifact.latency_ms,
                    }
                )
            decorated.append(
                hit.model_copy(
                    update={
                        "retrieval_mode": hit.retrieval_mode or retrieval_mode,
                        "embedding_provider_used": (
                            hit.embedding_provider_used
                            or (embedding_artifact.provider if embedding_artifact and not hit.fallback_used else None)
                        ),
                        "fallback_used": fallback_used or hit.fallback_used or bool(
                            embedding_artifact and embedding_artifact.fallback_used
                        ),
                        "metadata": metadata,
                    }
                )
            )
        return decorated

    async def _run_lexical_search(
        self, query: str, limit: int, as_of: date | None, kinds: list[MemoryKind],
        document_keys: list[str] | None,
    ) -> list[MemorySearchHit]:
        request = MemorySearchRequest(
            query=query, limit=limit, mode="lexical", as_of=as_of, kinds=kinds,
            document_keys=document_keys
        )
        started = time.perf_counter()
        hits = await anyio.to_thread.run_sync(partial(self.repository.search_documents, request))
        self._record_success("search_memory", started)
        return hits

    def _record_request(self, operation: str) -> None:
        if self.observability is not None:
            self.observability.record_request(operation)

    def _record_success(self, operation: str, started: float) -> None:
        if self.observability is not None:
            self.observability.record_success(operation, time.perf_counter() - started)

    def _record_fallback(self, operation: str) -> None:
        if self.observability is not None:
            self.observability.record_fallback(operation)
