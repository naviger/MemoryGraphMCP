from __future__ import annotations

from app.config.settings import Settings
from app.repositories.arango_repository import ArangoMemoryRepository, ArangoRepositoryConfig


def build_repository(settings: Settings) -> ArangoMemoryRepository:
    password = settings.arango_password or settings.arango_root_password
    return ArangoMemoryRepository(
        ArangoRepositoryConfig(
            architecture_entities_collection=settings.arango_architecture_entities_collection,
            architecture_edges_collection=settings.arango_architecture_edges_collection,
            url=settings.arango_url,
            database=settings.arango_database,
            username=settings.arango_username,
            password=password,
            documents_collection=settings.arango_documents_collection,
            edges_collection=settings.arango_edges_collection,
            vectors_collection=settings.arango_vectors_collection,
            search_view=settings.arango_search_view,
            vector_view=settings.arango_vector_view,
        )
    )
