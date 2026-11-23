from __future__ import annotations

import pytest

from app.domain.exceptions import EmbeddingError
from app.domain.models import MemoryDocument
from app.services.embeddings import DeterministicEmbeddingProvider, EmbeddingService


class FailingProvider:
    name = "failing"
    model = "broken"
    dimension = 4
    version = "1"

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("provider unavailable")


class ConstantProvider:
    name = "constant"
    model = "constant-model"
    dimension = 4
    version = "1"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.25, 0.5, 0.75, 1.0]


def test_deterministic_embedding_provider_returns_expected_dimension() -> None:
    provider = DeterministicEmbeddingProvider(dimension=8)

    vector = provider.embed("MemoryGraph MCP")

    assert len(vector) == 8
    assert any(value != 0.0 for value in vector)


def test_embedding_service_falls_back_when_primary_provider_fails() -> None:
    fallback = ConstantProvider()
    service = EmbeddingService(primary_provider=FailingProvider(), fallback_provider=fallback, strict_mode=False)

    artifact = service.embed_query("semantic retrieval")

    assert artifact.provider == "constant"
    assert artifact.fallback_used is True
    assert fallback.calls == ["semantic retrieval"]
    assert artifact.vector == [0.25, 0.5, 0.75, 1.0]


def test_embedding_service_strict_mode_raises_on_primary_failure() -> None:
    service = EmbeddingService(primary_provider=FailingProvider(), fallback_provider=ConstantProvider(), strict_mode=True)

    with pytest.raises(EmbeddingError):
        service.embed_document(MemoryDocument(key="m1", title="Note", content="Body"))


def test_embedding_service_rejects_wrong_client_vector_dimension() -> None:
    service = EmbeddingService.default(dimension=4)

    with pytest.raises(EmbeddingError):
        service.embed_client_vector([0.1, 0.2])
