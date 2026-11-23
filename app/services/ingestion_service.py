from __future__ import annotations

from collections.abc import Callable

import anyio

from app.domain.models import ADRCreate, ADRWriteResult, ArchitecturalDecisionRecord, MemoryDocument
from app.repositories.protocol import MemoryRepository
from app.services.embeddings import EmbeddingService
from app.services.observability import MemoryObservability


class MemoryIngestionService:
    def __init__(
        self,
        repository: MemoryRepository,
        embedding_service: EmbeddingService | None = None,
        observability: MemoryObservability | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_service = embedding_service or EmbeddingService.default()
        self.observability = observability

    async def ingest_memory(self, document: MemoryDocument) -> MemoryDocument:
        return await self._ingest(document, self.repository.write_document)

    async def ingest_adr(self, draft: ADRCreate) -> ADRWriteResult:
        content = (
            f"Decision: {draft.decision}\n\n"
            f"Rationale: {draft.rationale}\n\n"
            f"Consequences: {draft.consequences}"
        )
        record = ArchitecturalDecisionRecord(**draft.model_dump(), content=content)
        return await self._ingest(record, self.repository.create_adr)

    async def _ingest[DocumentT: MemoryDocument, StoredT: MemoryDocument](
        self, document: DocumentT, store: Callable[[DocumentT], StoredT],
    ) -> StoredT:
        normalized = self._normalize_document(document)
        embedded = await anyio.to_thread.run_sync(self.embedding_service.embed_document, normalized)
        enriched = normalized.model_copy(
            update={
                "embedding": embedded.vector,
                "embedding_provider": embedded.provider,
                "embedding_model": embedded.model,
                "embedding_dimension": embedded.dimension,
                "embedding_version": embedded.version,
                "metadata": {
                    **normalized.metadata,
                    "embedding_provider": embedded.provider,
                    "embedding_model": embedded.model,
                    "embedding_dimension": embedded.dimension,
                    "embedding_version": embedded.version,
                    "embedding_fallback_used": embedded.fallback_used,
                },
            }
        )
        if self.observability is not None:
            self.observability.record_request("embedding_ingest")
        stored = await anyio.to_thread.run_sync(store, enriched)
        if self.observability is not None:
            if embedded.fallback_used:
                self.observability.record_fallback("embedding_ingest")
            self.observability.record_success("embedding_ingest", embedded.latency_ms / 1000.0)
        return stored

    def _normalize_document[DocumentT: MemoryDocument](self, document: DocumentT) -> DocumentT:
        provenance = dict(document.provenance)
        if document.source is not None:
            provenance.setdefault("source", document.source)
        if document.source_id is not None:
            provenance.setdefault("source_id", document.source_id)

        return document.model_copy(
            update={
                "namespace": document.namespace or "default",
                "version": max(1, document.version),
                "status": document.status or "active",
                "provenance": provenance,
            }
        )
