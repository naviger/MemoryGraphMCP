from __future__ import annotations

import pytest

from app.domain.models import MemoryDocument
from app.services.ingestion_service import MemoryIngestionService


class FakeRepository:
    def __init__(self) -> None:
        self.documents: list[MemoryDocument] = []

    def write_document(self, document: MemoryDocument) -> MemoryDocument:
        self.documents.append(document)
        return document


@pytest.mark.asyncio
async def test_ingest_memory_adds_provenance_and_lifecycle_defaults() -> None:
    repository = FakeRepository()
    service = MemoryIngestionService(repository)

    document = MemoryDocument(
        key="memory-1",
        title="Service contract",
        content="The API must be versioned.",
        source="github",
        source_id="issue-42",
    )

    stored = await service.ingest_memory(document)

    assert stored.version == 1
    assert stored.status == "active"
    assert stored.namespace == "default"
    assert stored.provenance["source"] == "github"
    assert stored.provenance["source_id"] == "issue-42"
    assert repository.documents[0] == stored


@pytest.mark.asyncio
async def test_ingest_memory_preserves_explicit_values() -> None:
    repository = FakeRepository()
    service = MemoryIngestionService(repository)

    document = MemoryDocument(
        key="memory-2",
        title="Security note",
        content="JWT must be validated.",
        source="wiki",
        source_id="doc-17",
        namespace="platform-security",
        version=7,
        status="archived",
        provenance={"reviewed_by": "ops"},
    )

    stored = await service.ingest_memory(document)

    assert stored.namespace == "platform-security"
    assert stored.version == 7
    assert stored.status == "archived"
    assert stored.provenance == {
        "reviewed_by": "ops",
        "source": "wiki",
        "source_id": "doc-17",
    }
    assert repository.documents[0] == stored
