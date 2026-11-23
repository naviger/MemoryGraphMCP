#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio

from app.config.settings import get_settings
from app.repositories.factory import build_repository
from app.services.embeddings import EmbeddingService
from app.services.observability import MemoryObservability


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Re-embed the MemoryGraphMCP corpus")
    parser.add_argument("--documents", action="store_true", help="Update memory_documents embedding fields")
    parser.add_argument("--vectors", action="store_true", help="Update memory_vectors records")
    parser.add_argument("--strict", action="store_true", help="Fail on embedding provider errors instead of using fallback")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of documents to re-embed")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    settings.embedding_strict_mode = args.strict or settings.embedding_strict_mode
    repository = build_repository(settings)
    embedding_service = EmbeddingService.from_settings(settings)
    observability = MemoryObservability()

    documents = repository.list_documents()
    if args.limit > 0:
        documents = documents[: args.limit]

    if not args.documents and not args.vectors:
        args.documents = True
        args.vectors = True

    async def _run() -> None:
        for document in documents:
            embedded = embedding_service.embed_document(document, allow_fallback=not args.strict)
            updated = document.model_copy(
                update={
                    "embedding": embedded.vector,
                    "embedding_provider": embedded.provider,
                    "embedding_model": embedded.model,
                    "embedding_dimension": embedded.dimension,
                    "embedding_version": embedded.version,
                    "metadata": {
                        **document.metadata,
                        "embedding_provider": embedded.provider,
                        "embedding_model": embedded.model,
                        "embedding_dimension": embedded.dimension,
                        "embedding_version": embedded.version,
                        "embedding_fallback_used": embedded.fallback_used,
                    },
                }
            )
            if args.documents:
                updated = repository.update_document_embedding(updated)
            if args.vectors:
                repository.upsert_vector_document(updated)
        print(f"Re-embedded {len(documents)} documents")
        print(observability.snapshot())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
