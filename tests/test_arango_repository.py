from __future__ import annotations

import pytest

from app.domain.models import MemoryDocument
from app.repositories.arango_repository import ArangoMemoryRepository, ArangoRepositoryConfig


class FakeCollection:
    def __init__(self) -> None:
        self.inserted: list[dict[str, object]] = []
        self.documents_by_key: dict[str, dict[str, object]] = {}

    def insert(self, document: dict[str, object], return_new: bool = False):
        self.inserted.append(document)
        stored = {"_key": document.get("key", "memory-1"), **document}
        self.documents_by_key[str(stored["_key"])] = stored
        return {"new": stored} if return_new else stored

    def get(self, document_key: str):
        return self.documents_by_key.get(document_key)


class FakeDatabase:
    def __init__(self) -> None:
        self.collections = {
            "memory_documents": FakeCollection(),
            "memory_edges": FakeCollection(),
            "memory_vectors": FakeCollection(),
        }
        self.views = {"memory_search": True, "memory_vectors_view": True}
        self.ping_count = 0
        self.created_collections: list[tuple[str, bool]] = []

    def ping(self) -> None:
        self.ping_count += 1

    def has_collection(self, name: str) -> bool:
        return name in self.collections

    def create_collection(self, name: str, edge: bool = False):
        self.created_collections.append((name, edge))
        self.collections[name] = FakeCollection()

    def has_view(self, name: str) -> bool:
        return name in self.views

    def collection(self, name: str) -> FakeCollection:
        return self.collections[name]


@pytest.fixture()
def repository() -> ArangoMemoryRepository:
    config = ArangoRepositoryConfig(
        url="http://localhost:8529",
        database="architecture",
        username="architect",
        password="@rchitect",
        documents_collection="memory_documents",
        edges_collection="memory_edges",
        vectors_collection="memory_vectors",
        search_view="memory_search",
        vector_view="memory_vectors_view",
    )
    repository = object.__new__(ArangoMemoryRepository)
    repository._config = config
    repository.__dict__["database"] = FakeDatabase()
    return repository


def test_ensure_storage_ready_checks_collections_and_views(repository: ArangoMemoryRepository) -> None:
    repository.ensure_storage_ready()

    assert repository.database.ping_count == 1
    assert repository.database.created_collections == [
        ("architecture_entities", False), ("architecture_edges", True),
    ]


def test_write_then_read_document(repository: ArangoMemoryRepository) -> None:
    document = MemoryDocument(key="memory-1", title="Boot Log", content="Storage is ready")

    written = repository.write_document(document)
    loaded = repository.read_document("memory-1")

    assert written.key == "memory-1"
    assert loaded is not None
    assert loaded.title == "Boot Log"
    assert loaded.content == "Storage is ready"
    assert repository.database.collections["memory_documents"].inserted[0]["title"] == "Boot Log"


def test_ensure_storage_ready_raises_when_collection_missing(repository: ArangoMemoryRepository) -> None:
    del repository.database.collections["memory_vectors"]

    with pytest.raises(RuntimeError, match="memory_vectors"):
        repository.ensure_storage_ready()
