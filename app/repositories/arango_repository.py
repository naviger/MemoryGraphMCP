from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from arango import ArangoClient
from arango.database import StandardDatabase

from app.domain.models import GraphTraversalRequest, MemoryDocument, MemorySearchHit, MemorySearchRequest, RelatedEntity


@dataclass(frozen=True)
class ArangoRepositoryConfig:
    url: str
    database: str
    username: str
    password: str
    documents_collection: str
    edges_collection: str
    vectors_collection: str
    search_view: str
    vector_view: str


class ArangoMemoryRepository:
    def __init__(self, config: ArangoRepositoryConfig) -> None:
        self._config = config
        self._client = ArangoClient(hosts=config.url)

    @cached_property
    def database(self) -> StandardDatabase:
        return self._client.db(
            name=self._config.database,
            username=self._config.username,
            password=self._config.password,
        )

    def ensure_connected(self) -> None:
        self.database.ping()

    def ensure_storage_ready(self) -> None:
        self.ensure_connected()
        for collection_name in (
            self._config.documents_collection,
            self._config.edges_collection,
            self._config.vectors_collection,
        ):
            if not self.database.has_collection(collection_name):
                raise RuntimeError(f"ArangoDB collection is missing: {collection_name}")

        for view_name in (self._config.search_view, self._config.vector_view):
            if not self.database.has_view(view_name):
                raise RuntimeError(f"ArangoDB view is missing: {view_name}")

    def write_document(self, document: MemoryDocument) -> MemoryDocument:
        collection = self.database.collection(self._config.documents_collection)
        payload = document.model_dump(mode="json", exclude_none=True)
        result = collection.insert(payload, return_new=True)
        created = result["new"] if isinstance(result, dict) and "new" in result else payload
        return MemoryDocument.model_validate(created)

    def read_document(self, document_key: str) -> MemoryDocument | None:
        collection = self.database.collection(self._config.documents_collection)
        document = collection.get(document_key)
        if document is None:
            return None
        return MemoryDocument.model_validate(document)

    def search_documents(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        query = """
        FOR doc IN @@documents
            FILTER LENGTH(@kinds) == 0 OR doc.kind IN @kinds
            FILTER LENGTH(@tags) == 0 OR LENGTH(INTERSECTION(COALESCE(doc.tags, []), @tags)) > 0
            FILTER @query == ""
                OR CONTAINS(LOWER(COALESCE(doc.title, "")), @query_lower)
                OR CONTAINS(LOWER(COALESCE(doc.content, "")), @query_lower)
            SORT TO_NUMBER(COALESCE(doc.rank, 0)) DESC
            LIMIT @limit
            RETURN MERGE(doc, {score: 1.0})
        """
        return self._run_document_query(query, request)

    def semantic_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        if not request.embedding:
            return self.search_documents(request)

        query = """
        FOR doc IN @@documents
            FILTER LENGTH(@kinds) == 0 OR doc.kind IN @kinds
            FILTER HAS(doc, "embedding")
            FILTER LENGTH(doc.embedding) == LENGTH(@query_vector)
            LET score = COSINE_SIMILARITY(doc.embedding, @query_vector)
            SORT score DESC
            LIMIT @limit
            RETURN MERGE(doc, {score: score})
        """
        return self._run_document_query(query, request, extra_bind_vars={"query_vector": request.embedding})

    def hybrid_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        lexical = self.search_documents(request)
        semantic = self.semantic_search(request)
        ranked: dict[str, MemorySearchHit] = {}
        for hit in lexical + semantic:
            key = hit.document.id or hit.document.key or hit.document.title
            current = ranked.get(key)
            if current is None or hit.score > current.score:
                ranked[key] = hit
        return sorted(ranked.values(), key=lambda hit: hit.score, reverse=True)[: request.limit]

    def find_related_entities(self, request: GraphTraversalRequest) -> list[RelatedEntity]:
        query = """
        FOR vertex, edge, path IN 1..@depth OUTBOUND @seed @@edges
            LIMIT @limit
            RETURN {
                entity_id: vertex._id,
                label: vertex.title,
                relation: edge.relation,
                depth: LENGTH(path.edges),
                score: 1.0,
                metadata: KEEP(vertex, ["kind", "tags", "source"])
            }
        """
        cursor = self.database.aql.execute(
            query,
            bind_vars={
                "seed": request.seed_entity_id,
                "depth": request.depth,
                "limit": request.limit,
                "edges": self._config.edges_collection,
            },
        )
        return [RelatedEntity.model_validate(item) for item in cursor]

    def _run_document_query(
        self,
        query: str,
        request: MemorySearchRequest,
        extra_bind_vars: dict[str, object] | None = None,
    ) -> list[MemorySearchHit]:
        bind_vars = {
            "query": request.query,
            "query_lower": request.query.lower(),
            "limit": request.limit,
            "kinds": request.kinds,
            "tags": request.tags,
            "documents": self._config.documents_collection,
        }
        if extra_bind_vars:
            bind_vars.update(extra_bind_vars)
        cursor = self.database.aql.execute(query, bind_vars=bind_vars)
        return [self._row_to_hit(item) for item in cursor]

    @staticmethod
    def _row_to_hit(row: dict[str, object]) -> MemorySearchHit:
        score = float(row.pop("score", 0.0))
        document = MemoryDocument.model_validate(row)
        return MemorySearchHit(document=document, score=score)
