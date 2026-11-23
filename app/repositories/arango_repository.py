from __future__ import annotations

import re
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, timedelta
from functools import cached_property
from hashlib import sha256

from arango import ArangoClient
from arango.database import StandardDatabase, TransactionDatabase
from arango.exceptions import ArangoServerError, DocumentInsertError

from app.domain.exceptions import ADRError, RevalidationError
from app.domain.models import (
    ADRAcceptanceConflictCheck,
    ADRConflict,
    ADRConflictListQuery,
    ADRConflictQuery,
    ADRConflictResolution,
    ADRConflictScope,
    ADRResolutionDecision,
    ADRStatus,
    ADRSupersession,
    ADRWriteResult,
    ArchitecturalDecisionRecord,
    ConfidenceScore,
    GraphTraversalRequest,
    MemoryDocument,
    MemoryRevalidationResult,
    MemorySearchHit,
    MemorySearchRequest,
    MemoryValidation,
    OverdueValidation,
    RelatedEntity,
    ResolvedADRConflict,
)
from app.domain.project_guidance import GuidanceDecision
from app.domain.review_details import ProjectDocument, ReviewEntityLink
from app.repositories.arango_architecture import ArangoArchitectureRepository

_VALIDITY_FILTER = """
    FILTER @as_of == null OR (
        (doc.effective_date == null OR doc.effective_date <= @as_of)
        AND (doc.expiration_date == null OR @as_of < doc.expiration_date)
        AND (
            doc.kind != "adr" OR !HAS(doc, "decision")
            OR doc.status == "accepted"
            OR (doc.status == "superseded" AND doc.expiration_date != null)
        )
    )
"""


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
    architecture_entities_collection: str = "architecture_entities"
    architecture_edges_collection: str = "architecture_edges"


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

    @cached_property
    def architecture(self) -> ArangoArchitectureRepository:
        return ArangoArchitectureRepository(
            lambda: self.database,
            self._config.architecture_entities_collection,
            self._config.architecture_edges_collection,
        )

    def ensure_connected(self) -> None:
        self.database.properties()

    def ensure_storage_ready(self) -> None:
        self.ensure_connected()
        self._ensure_collection(self._config.documents_collection)
        self._ensure_collection(self._config.edges_collection, edge=True)
        self._ensure_collection(self._config.vectors_collection)
        self.architecture.ensure_storage_ready()

        self._ensure_search_view(self._config.search_view, self._config.documents_collection)
        self._ensure_search_view(self._config.vector_view, self._config.vectors_collection)

    def _ensure_collection(self, collection_name: str, edge: bool = False) -> None:
        if not self.database.has_collection(collection_name):
            self.database.create_collection(collection_name, edge=edge)

    def _ensure_search_view(self, view_name: str, collection_name: str) -> None:
        try:
            self.database.view_info(view_name)
            return
        except Exception:
            pass

        self.database.create_arangosearch_view(
            view_name,
            {
                "links": {
                    collection_name: {
                        "includeAllFields": True,
                    }
                }
            },
        )

    @contextmanager
    def _memory_transaction(self, *, with_edges: bool = False):
        collections = [self._config.documents_collection, self._config.vectors_collection]
        if with_edges:
            collections.append(self._config.edges_collection)
        transaction = self.database.begin_transaction(
            exclusive=collections, read=None if with_edges else self._config.edges_collection,
        )
        try:
            yield transaction
            transaction.commit_transaction()
        except BaseException:
            with suppress(ArangoServerError):
                transaction.abort_transaction()
            raise

    def create_adr(self, record: ArchitecturalDecisionRecord) -> ADRWriteResult:
        if record.status == "superseded":
            raise ADRError("Set superseded status by linking an accepted replacement")
        payload = self._document_payload(record)
        payload["next_review_due"] = self._next_review_due(
            record, record.last_validated or record.date,
        ).isoformat()
        with self._memory_transaction() as transaction:
            documents = transaction.collection(self._config.documents_collection)
            try:
                result = documents.insert(payload, return_new=True)
            except DocumentInsertError as exc:
                if exc.error_code == 1210:
                    raise ADRError(f"Document key '{record.key}' already exists") from exc
                raise
            if record.embedding:
                vectors = transaction.collection(self._config.vectors_collection)
                vectors.insert(payload, overwrite_mode="replace")
            stored = ArchitecturalDecisionRecord.model_validate(result["new"])
            return self._adr_write_result(transaction, stored)

    def read_adr(self, key: str) -> ArchitecturalDecisionRecord:
        return self._require_adr(self.read_document(key), key)

    @staticmethod
    def _require_adr(document: MemoryDocument | None, key: str) -> ArchitecturalDecisionRecord:
        if not isinstance(document, ArchitecturalDecisionRecord):
            raise ADRError(f"Structured ADR '{key}' was not found")
        return document

    def set_adr_status(self, key: str, status: ADRStatus) -> ADRWriteResult:
        with self._memory_transaction() as transaction:
            documents = transaction.collection(self._config.documents_collection)
            record = self._require_adr(self._get_document(documents, key), key)
            if record.status != status:
                if record.status != "proposed" or status not in {"accepted", "rejected"}:
                    raise ADRError("Only proposed ADRs can be accepted or rejected")
                record = self._update_adr_status(transaction, record.key, status)
            return self._adr_write_result(transaction, record)

    def _adr_write_result(
        self, transaction: TransactionDatabase, record: ArchitecturalDecisionRecord,
    ) -> ADRWriteResult:
        check = None
        if record.status == "accepted":
            if record.conflict_scope is None:
                check = ADRAcceptanceConflictCheck(status="missing_scope")
            else:
                limit = 20
                possible = self._find_adr_conflicts(transaction, record, as_of=None, limit=limit + 1)
                check = ADRAcceptanceConflictCheck(
                    status="checked", conflicts=possible[:limit], has_more=len(possible) > limit,
                )
        return ADRWriteResult(**record.model_dump(), conflict_check=check)

    def set_adr_conflict_scope(
        self, key: str, scope: ADRConflictScope | None,
    ) -> ArchitecturalDecisionRecord:
        with self._memory_transaction() as transaction:
            documents = transaction.collection(self._config.documents_collection)
            record = self._require_adr(self._get_document(documents, key), key)
            updated = self._update_document_and_vector(
                transaction,
                {
                    "_key": record.key,
                    "conflict_scope": scope.model_dump(mode="json") if scope is not None else None,
                },
            )
            return ArchitecturalDecisionRecord.model_validate(updated)

    def find_adr_conflicts(self, request: ADRConflictQuery) -> list[ADRConflict]:
        return self._find_adr_conflicts(
            self.database, self.read_adr(request.key), as_of=request.as_of, limit=request.limit,
        )

    def list_adr_conflicts(
        self, request: ADRConflictListQuery, *, document_keys: list[str] | None = None
    ) -> list[ADRConflict]:
        return self._query_adr_conflicts(self.database, request, document_keys=document_keys)

    def _find_adr_conflicts(
        self, database: StandardDatabase | TransactionDatabase,
        original: ArchitecturalDecisionRecord, *, as_of: date | None, limit: int,
        competing_key: str | None = None,
    ) -> list[ADRConflict]:
        if original.conflict_scope is None:
            raise ADRError("Set a topic and system or environment before checking this ADR")
        if original.status != "accepted" and (
            original.status != "superseded" or original.expiration_date is None
        ):
            return []
        if as_of and (
            (original.effective_date and as_of < original.effective_date)
            or (original.expiration_date and as_of >= original.expiration_date)
        ):
            return []

        return self._query_adr_conflicts(
            database, ADRConflictListQuery(as_of=as_of, limit=limit),
            key=original.key, competing_key=competing_key,
        )

    def _query_adr_conflicts(
        self, database: StandardDatabase | TransactionDatabase, request: ADRConflictListQuery,
        *, key: str | None = None, competing_key: str | None = None,
        document_keys: list[str] | None = None,
    ) -> list[ADRConflict]:
        query = """
        FOR original IN @@documents
            FILTER @key == null OR original._key == @key
            FILTER original.kind == "adr" AND IS_STRING(original.conflict_scope.topic)
            FILTER original.status == "accepted"
                OR (original.status == "superseded" AND original.expiration_date != null)
            FILTER @as_of == null OR (
                (original.effective_date == null OR original.effective_date <= @as_of)
                AND (original.expiration_date == null OR @as_of < original.expiration_date)
            )
            FILTER IS_STRING(original.decision)
            LET normalized_decision = REGEX_REPLACE(LOWER(TRIM(original.decision)), "[[:space:]]+", " ")

            FOR doc IN @@documents
                FILTER @key == null ? original._key < doc._key : original._key != doc._key
                FILTER @document_keys == null OR original._key IN @document_keys
                    OR doc._key IN @document_keys
                FILTER @competing_key == null OR doc._key == @competing_key
                FILTER doc.kind == "adr" AND doc.conflict_scope.topic == original.conflict_scope.topic
                FILTER NOT_NULL(doc.namespace, "default") == NOT_NULL(original.namespace, "default")
                LET original_system = original.conflict_scope.system
                LET original_environment = original.conflict_scope.environment
                LET system = doc.conflict_scope.system
                LET environment = doc.conflict_scope.environment
                FILTER (original_system != null AND system == original_system)
                    OR (original_environment != null AND environment == original_environment)
                FILTER original_system == null OR system == null OR system == original_system
                FILTER original_environment == null OR environment == null
                    OR environment == original_environment
                FILTER @system == null OR NOT_NULL(original_system, system) == @system
                FILTER @environment == null OR NOT_NULL(original_environment, environment) == @environment
                FILTER doc.status == "accepted"
                    OR (doc.status == "superseded" AND doc.expiration_date != null)
                FILTER original.expiration_date == null OR doc.effective_date == null
                    OR doc.effective_date < original.expiration_date
                FILTER original.effective_date == null OR doc.expiration_date == null
                    OR original.effective_date < doc.expiration_date
                FILTER @as_of == null OR (
                    (doc.effective_date == null OR doc.effective_date <= @as_of)
                    AND (doc.expiration_date == null OR @as_of < doc.expiration_date)
                )
                FILTER IS_STRING(doc.decision)
                FILTER REGEX_REPLACE(LOWER(TRIM(doc.decision)), "[[:space:]]+", " ") != normalized_decision
                LET resolution = FIRST(
                    FOR edge IN @@edges
                        FILTER (edge._from == original._id AND edge._to == doc._id)
                            OR (edge._from == doc._id AND edge._to == original._id)
                        FILTER edge.relation == "adr_conflict_resolution"
                        LIMIT 1
                        RETURN true
                )
                FILTER resolution == null
                LET overlap_start = original.effective_date == null OR doc.effective_date == null
                    ? NOT_NULL(original.effective_date, doc.effective_date)
                    : MAX([original.effective_date, doc.effective_date])
                LET overlap_end = original.expiration_date == null OR doc.expiration_date == null
                    ? NOT_NULL(original.expiration_date, doc.expiration_date)
                    : MIN([original.expiration_date, doc.expiration_date])
                SORT overlap_start DESC, original._key, doc._key
                LIMIT @offset, @limit
                RETURN {
                    original: UNSET(original, "embedding", "content"),
                    competing: UNSET(doc, "embedding", "content"),
                    overlap_start,
                    overlap_end
                }
        """
        cursor = database.aql.execute(
            query,
            bind_vars={
                "@documents": self._config.documents_collection,
                "@edges": self._config.edges_collection,
                "key": key,
                "competing_key": competing_key,
                "document_keys": document_keys,
                "system": request.system,
                "environment": request.environment,
                "as_of": request.as_of.isoformat() if request.as_of else None,
                "limit": request.limit,
                "offset": request.offset,
            },
        )
        return [ADRConflict.model_validate(row) for row in cursor]

    def list_overdue_validations(
        self, as_of: date, limit: int, offset: int, *, document_keys: list[str] | None = None,
    ) -> list[OverdueValidation]:
        query = """
        FOR doc IN @@documents
            FILTER @document_keys == null OR doc._key IN @document_keys
            FILTER IS_STRING(doc.next_review_due) AND doc.next_review_due < @as_of
        """ + _VALIDITY_FILTER + """
            FILTER doc.status NOT IN ["proposed", "rejected", "superseded", "archived", "inactive"]
            SORT doc.next_review_due, doc._key
            LIMIT @offset, @limit
            RETURN {
                key: doc._key,
                title: doc.title,
                kind: NOT_NULL(doc.kind, "memory"),
                status: doc.status,
                next_review_due: doc.next_review_due,
                effective_date: doc.effective_date,
                expiration_date: doc.expiration_date,
                last_validated: doc.last_validated,
                confidence: doc.confidence,
                last_validation: LAST(NOT_NULL(doc.validation_history, []))
            }
        """
        rows = self.database.aql.execute(
            query,
            bind_vars={
                "@documents": self._config.documents_collection,
                "as_of": as_of.isoformat(),
                "limit": limit,
                "offset": offset,
                "document_keys": document_keys,
            },
        )
        return [OverdueValidation.model_validate(row) for row in rows]

    def list_resolved_adr_conflicts(self, request: ADRConflictListQuery) -> list[ResolvedADRConflict]:
        query = """
        FOR edge IN @@edges
            FILTER edge.relation == "adr_conflict_resolution"
            FILTER STARTS_WITH(edge._from, @document_prefix) AND STARTS_WITH(edge._to, @document_prefix)
            FILTER @as_of == null OR (
                (edge.overlap_start == null OR edge.overlap_start <= @as_of)
                AND (edge.overlap_end == null OR @as_of < edge.overlap_end)
            )
            LET original = DOCUMENT(edge._from)
            LET competing = DOCUMENT(edge._to)
            FILTER original.kind == "adr" AND competing.kind == "adr"
            LET system = HAS(edge, "system") ? edge.system
                : NOT_NULL(original.conflict_scope.system, competing.conflict_scope.system)
            LET environment = HAS(edge, "environment") ? edge.environment
                : NOT_NULL(original.conflict_scope.environment, competing.conflict_scope.environment)
            FILTER @system == null OR system == @system
            FILTER @environment == null OR environment == @environment
            SORT edge.overlap_start DESC, MIN([original._key, competing._key]),
                MAX([original._key, competing._key])
            LIMIT @offset, @limit
            RETURN {
                original: UNSET(original, "embedding", "content"),
                competing: UNSET(competing, "embedding", "content"),
                overlap_start: edge.overlap_start,
                overlap_end: edge.overlap_end,
                resolution: MERGE(edge, {system, environment})
            }
        """
        cursor = self.database.aql.execute(
            query,
            bind_vars={
                "@edges": self._config.edges_collection,
                "document_prefix": f"{self._config.documents_collection}/",
                "system": request.system,
                "environment": request.environment,
                "as_of": request.as_of.isoformat() if request.as_of else None,
                "limit": request.limit,
                "offset": request.offset,
            },
        )
        return [ResolvedADRConflict.model_validate(row) for row in cursor]

    def _document_id(self, key: str) -> str:
        return f"{self._config.documents_collection}/{key}"

    @staticmethod
    def _resolution_edge_key(first_id: str, second_id: str) -> str:
        pair = "|".join(sorted((first_id, second_id)))
        return f"adr-conflict-resolution-{sha256(pair.encode()).hexdigest()}"

    def get_adr_conflict_resolution(
        self, source_key: str, target_key: str,
    ) -> ADRConflictResolution:
        key = self._resolution_edge_key(self._document_id(source_key), self._document_id(target_key))
        row = self.database.collection(self._config.edges_collection).get(key)
        if row is None:
            raise ADRError("No resolution recorded for this ADR pair")
        return ADRConflictResolution.model_validate(row)

    def resolve_adr_conflict(
        self, source_key: str, target_key: str, decision: ADRResolutionDecision,
    ) -> ADRConflictResolution:
        with self._memory_transaction(with_edges=True) as transaction:
            documents = transaction.collection(self._config.documents_collection)
            source = self._require_adr(self._get_document(documents, source_key), source_key)
            target = self._require_adr(self._get_document(documents, target_key), target_key)
            if source.key == target.key:
                raise ADRError("A conflict resolution requires two different ADRs")

            source_id = self._document_id(source.key)
            target_id = self._document_id(target.key)
            edges = transaction.collection(self._config.edges_collection)
            key = self._resolution_edge_key(source_id, target_id)
            stored = edges.get(key)
            if stored is not None:
                existing = ADRConflictResolution.model_validate(stored)
                same_direction = decision.action == "coexist" or existing.source_key == source.key
                if existing.action == decision.action and existing.rationale == decision.rationale and same_direction:
                    return existing
                raise ADRError("A different resolution is already recorded for this ADR pair")

            conflicts = self._find_adr_conflicts(
                transaction, source, as_of=None, limit=1, competing_key=target.key,
            )
            if not conflicts:
                raise ADRError("There is no unresolved conflict between these ADRs")
            conflict = conflicts[0]
            source_scope = source.conflict_scope
            target_scope = target.conflict_scope
            assert source_scope is not None and target_scope is not None
            resolution = ADRConflictResolution(
                **decision.model_dump(),
                source_key=source.key,
                target_key=target.key,
                overlap_start=conflict.overlap_start,
                overlap_end=conflict.overlap_end,
                system=source_scope.system or target_scope.system,
                environment=source_scope.environment or target_scope.environment,
            )
            if decision.action == "supersede":
                self._supersede_adr(transaction, source, target)
            edges.insert({
                **resolution.model_dump(mode="json"),
                "_key": key,
                "_from": source_id,
                "_to": target_id,
                "relation": "adr_conflict_resolution",
            })
            return resolution

    def supersede_adr(self, source_key: str, target_key: str) -> ADRSupersession:
        with self._memory_transaction(with_edges=True) as transaction:
            documents = transaction.collection(self._config.documents_collection)
            source = self._require_adr(self._get_document(documents, source_key), source_key)
            target = self._require_adr(self._get_document(documents, target_key), target_key)
            return self._supersede_adr(transaction, source, target)

    def _supersede_adr(
        self, transaction: TransactionDatabase,
        source: ArchitecturalDecisionRecord, target: ArchitecturalDecisionRecord,
    ) -> ADRSupersession:
        if source.key == target.key:
            raise ADRError("An ADR cannot supersede itself")

        relationship = ADRSupersession(
            source_id=self._document_id(source.key), target_id=self._document_id(target.key),
        )
        edges = transaction.collection(self._config.edges_collection)
        edge_key = f"adr-superseded-by-{sha256(relationship.source_id.encode()).hexdigest()}"
        existing = edges.get(edge_key)
        if existing:
            if existing["_to"] != relationship.target_id:
                raise ADRError("The original ADR already has a different replacement")
            return relationship
        if source.status != "accepted" or target.status != "accepted":
            raise ADRError("Both ADRs must be accepted to record supersession")

        expiration_date = source.expiration_date
        if target.effective_date and (
            expiration_date is None or target.effective_date < expiration_date
        ):
            expiration_date = target.effective_date
            if source.effective_date and expiration_date <= source.effective_date:
                raise ADRError("The replacement must take effect after the original ADR")

        edges.insert({
            "_key": edge_key,
            "_from": relationship.source_id,
            "_to": relationship.target_id,
            "relation": relationship.relation,
        })
        self._update_adr_status(
            transaction, source.key, "superseded", expiration_date=expiration_date,
        )
        return relationship

    def _update_adr_status(
        self, transaction, key: str, status: ADRStatus, *, expiration_date: date | None = None,
    ) -> ArchitecturalDecisionRecord:
        payload = {"_key": key, "status": status}
        if expiration_date is not None:
            payload["expiration_date"] = expiration_date.isoformat()
        return ArchitecturalDecisionRecord.model_validate(
            self._update_document_and_vector(transaction, payload)
        )

    @staticmethod
    def _next_review_due(document: MemoryDocument, checked_on: date) -> date:
        security_adr = document.kind == "adr" and any(
            tag.strip().casefold() == "security" for tag in document.tags
        )
        return checked_on + timedelta(days=30 if security_adr else 90)

    def revalidate_memory(
        self, key: str, validation: MemoryValidation, confidence: ConfidenceScore | None = None,
    ) -> MemoryRevalidationResult:
        with self._memory_transaction() as transaction:
            documents = transaction.collection(self._config.documents_collection)
            document = self._get_document(documents, key)
            if document is None:
                raise RevalidationError(f"Memory record '{key}' was not found")
            checked_at = validation.checked_at.astimezone(UTC)
            checked_on = checked_at.date()
            if document.last_validated and checked_on < document.last_validated:
                raise RevalidationError("A validation cannot precede the last validation date")
            latest = max((entry.checked_at for entry in document.validation_history), default=checked_at)
            if checked_at < latest:
                raise RevalidationError("A validation cannot precede the last recorded check")
            validation = MemoryValidation(
                reviewer=validation.reviewer,
                evidence=validation.evidence,
                checked_at=checked_at,
                previous_confidence=document.confidence,
                new_confidence=document.confidence if confidence is None else confidence,
            )
            due = self._next_review_due(document, checked_on)
            history = [*document.validation_history, validation]
            payload = {
                "_key": key,
                "last_validated": checked_on.isoformat(),
                "next_review_due": due.isoformat(),
                "validation_history": [entry.model_dump(mode="json") for entry in history],
            }
            if confidence is not None:
                payload["confidence"] = validation.new_confidence
            self._update_document_and_vector(transaction, payload)
            return MemoryRevalidationResult(
                key=key, validation=validation, last_validated=checked_on, next_review_due=due,
            )

    def _update_document_and_vector(self, transaction, payload: dict):
        documents = transaction.collection(self._config.documents_collection)
        result = documents.update(payload, return_new=True)
        vectors = transaction.collection(self._config.vectors_collection)
        if vectors.has(payload["_key"]):
            vectors.update(payload)
        return result["new"]

    @classmethod
    def _get_document(cls, collection, key: str) -> MemoryDocument | None:
        row = collection.get(key)
        return cls._parse_document(row) if row is not None else None

    @staticmethod
    def _parse_document(row: dict) -> MemoryDocument:
        if row.get("kind") == "adr" and {"decision", "rationale", "consequences", "date"} <= row.keys():
            return ArchitecturalDecisionRecord.model_validate(row)
        return MemoryDocument.model_validate(row)

    def read_guidance_decisions(self, keys: list[str], as_of: date) -> list[GuidanceDecision]:
        if not keys:
            return []
        rows = self.database.aql.execute(
            """
            FOR doc IN @@documents
                FILTER doc._key IN @keys AND doc.kind == "adr" AND doc.status == "accepted"
                FILTER IS_STRING(doc.decision) AND IS_STRING(doc.rationale)
                FILTER doc.effective_date == null OR doc.effective_date <= @as_of
                FILTER doc.expiration_date == null OR @as_of < doc.expiration_date
                LET validation = FIRST(
                    FOR item IN NOT_NULL(doc.validation_history, [])
                        SORT DATE_TIMESTAMP(item.checked_at) DESC
                        LIMIT 1
                        RETURN item
                )
                SORT doc._key
                RETURN MERGE(KEEP(doc, ["title", "decision", "rationale", "effective_date",
                    "expiration_date", "last_validated", "source", "source_id"]), {
                    record_key: doc._key, evidence: NOT_NULL(validation.evidence, [])
                })
            """,
            bind_vars={
                "@documents": self._config.documents_collection, "keys": keys,
                "as_of": as_of.isoformat(),
            },
        )
        return [GuidanceDecision.model_validate(row) for row in rows]

    def write_document(self, document: MemoryDocument) -> MemoryDocument:
        collection = self.database.collection(self._config.documents_collection)
        payload = self._document_payload(document)
        result = collection.insert(payload, return_new=True, overwrite=True, overwrite_mode="replace")
        created = result["new"] if isinstance(result, dict) and "new" in result else payload
        stored = self._parse_document(created)
        if stored.embedding:
            self.upsert_vector_document(stored)
        return stored

    def upsert_vector_document(self, document: MemoryDocument) -> MemoryDocument:
        collection = self.database.collection(self._config.vectors_collection)
        payload = self._document_payload(document)
        result = collection.insert(payload, return_new=True, overwrite=True, overwrite_mode="replace")
        created = result["new"] if isinstance(result, dict) and "new" in result else payload
        return self._parse_document(created)

    @staticmethod
    def _document_payload(document: MemoryDocument) -> dict[str, object]:
        payload = document.model_dump(mode="json", exclude_none=True)
        if document.key is not None:
            payload["_key"] = document.key
        return payload

    def update_document_embedding(self, document: MemoryDocument) -> MemoryDocument:
        if document.key is None:
            raise ValueError("Updating an embedding requires an existing document key")
        payload = document.model_dump(
            mode="json",
            include={
                "embedding",
                "embedding_provider",
                "embedding_model",
                "embedding_dimension",
                "embedding_version",
                "metadata",
            },
        )
        payload["_key"] = document.key
        collection = self.database.collection(self._config.documents_collection)
        result = collection.update(payload, return_new=True, merge=True)
        return self._parse_document(result["new"])

    def project_documents(
        self, project_key: str, repository_names: list[str], linked_keys: list[str],
        query: str, as_of: date | None,
    ) -> list[ProjectDocument]:
        aql = """
        FOR doc IN @@documents
            FILTER doc._key IN @linked_keys OR doc.metadata.project_key == @project_key
                OR doc.metadata.repo IN @repository_names
        """ + _VALIDITY_FILTER + """
            FILTER @query == "" OR CONTAINS(LOWER(NOT_NULL(doc.title, "")), @query)
                OR CONTAINS(LOWER(NOT_NULL(doc.content, "")), @query)
            SORT doc.kind, LOWER(doc.title), doc._key
            LET recorded = NOT_NULL(doc.created_at, doc.date, doc.effective_date)
            LET updated = MAX([doc.updated_at, doc.last_validated])
            RETURN MERGE(KEEP(doc, [
                "title", "kind", "source", "status", "effective_date", "expiration_date"
            ]), {
                key: doc._key,
                recorded_date: recorded == null ? null : LEFT(recorded, 10),
                last_recorded_update: updated == null ? null : LEFT(updated, 10),
                header: doc.kind == "adr" AND doc.status == null ? LEFT(doc.content, 1024) : "",
                reason: doc._key IN @linked_keys ? "Linked graph record"
                    : doc.metadata.project_key == @project_key ? "Project source metadata"
                    : CONCAT("Source repository: ", doc.metadata.repo)
            })
        """
        rows = self.database.aql.execute(aql, bind_vars={
            "@documents": self._config.documents_collection,
            "project_key": project_key, "repository_names": repository_names,
            "linked_keys": linked_keys, "query": query.casefold(),
            "as_of": as_of.isoformat() if as_of else None,
        })
        documents = []
        for row in rows:
            header = row.pop("header", "")
            status = re.search(
                r"^\s*-\s*Status:\s*(proposed|accepted|superseded|rejected)\b",
                header or "",
                re.IGNORECASE | re.MULTILINE,
            )
            if status:
                row["status"] = status[1].lower()
            documents.append(ProjectDocument.model_validate(row))
        return documents

    def list_documents(self) -> list[MemoryDocument]:
        query = "FOR doc IN @@documents SORT doc._key RETURN doc"
        cursor = self.database.aql.execute(query, bind_vars={"@documents": self._config.documents_collection})
        return [self._parse_document(item) for item in cursor]

    def read_document(self, document_key: str) -> MemoryDocument | None:
        collection = self.database.collection(self._config.documents_collection)
        return self._get_document(collection, document_key)

    def list_adr_supersession_links(self, key: str) -> list[ReviewEntityLink]:
        rows = self.database.aql.execute(
            """
            WITH @@documents
            FOR vertex, edge IN 1..1 ANY @seed @@edges
                FILTER edge.relation == "superseded_by" AND vertex.kind == "adr"
                SORT vertex.title, vertex._key
                RETURN {
                    key: vertex._key,
                    destination: "records",
                    entity_type: "ADR",
                    name: vertex.title,
                    relationships: [edge._from == @seed ? "superseded_by" : "supersedes"]
                }
            """,
            bind_vars={
                "@documents": self._config.documents_collection,
                "@edges": self._config.edges_collection,
                "seed": self._document_id(key),
            },
        )
        return [ReviewEntityLink.model_validate(row) for row in rows]

    def search_question_documents(
        self, terms: str, as_of: date, limit: int, *, document_keys: list[str] | None = None,
    ) -> list[MemoryDocument]:
        query = """
        LET terms = UNIQUE(TOKENS(@terms, "text_en"))
        FOR doc IN @@documents
            FILTER @document_keys == null OR doc._key IN @document_keys
        """ + _VALIDITY_FILTER + """
            FILTER doc.status NOT IN ["proposed", "rejected", "archived", "inactive"]
            LET title = CONCAT_SEPARATOR(" ", doc.title, doc._key, doc.conflict_scope.topic)
            LET body = CONCAT_SEPARATOR(" ", doc.decision, doc.content, doc.conflict_scope.system,
                doc.conflict_scope.environment)
            LET title_matches = INTERSECTION(terms, TOKENS(title, "text_en"))
            LET body_matches = INTERSECTION(terms, TOKENS(body, "text_en"))
            LET score = 2 * LENGTH(title_matches) + LENGTH(body_matches)
            FILTER score > 0
            SORT score DESC, doc.effective_date != null DESC, doc.kind == "adr" DESC, doc._key
            LIMIT @limit
            RETURN UNSET(doc, "embedding")
        """
        rows = self.database.aql.execute(query, bind_vars={
            "@documents": self._config.documents_collection,
            "document_keys": document_keys,
            "terms": terms, "as_of": as_of.isoformat(), "limit": limit,
        })
        return [self._parse_document(row) for row in rows]

    def search_documents(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        query = """
        FOR doc IN @@documents
        """ + _VALIDITY_FILTER + """
            FILTER @document_keys == null OR doc._key IN @document_keys
            FILTER LENGTH(@kinds) == 0 OR doc.kind IN @kinds
            FILTER LENGTH(@tags) == 0 OR LENGTH(INTERSECTION(NOT_NULL(doc.tags, []), @tags)) > 0
            FILTER @query == ""
                OR CONTAINS(LOWER(NOT_NULL(doc.title, "")), @query_lower)
                OR CONTAINS(LOWER(NOT_NULL(doc.content, "")), @query_lower)
            SORT TO_NUMBER(NOT_NULL(doc.rank, 0)) DESC
            LIMIT @limit
            RETURN MERGE(doc, {
                score: 1.0,
                retrieval_mode: "lexical",
                fallback_used: false,
                embedding_provider_used: null,
                score_breakdown: {lexical: 1.0}
            })
        """
        return self._run_document_query(
            query,
            request,
            {"query": request.query, "query_lower": request.query.lower(), "tags": request.tags},
        )

    def semantic_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        if not request.embedding:
            return self.search_documents(request)

        query = """
        FOR doc IN @@documents
        """ + _VALIDITY_FILTER + """
            FILTER @exclude_key == null OR doc._key != @exclude_key
            FILTER @document_keys == null OR doc._key IN @document_keys
            FILTER LENGTH(@kinds) == 0 OR doc.kind IN @kinds
            FILTER LENGTH(@tags) == 0 OR LENGTH(INTERSECTION(NOT_NULL(doc.tags, []), @tags)) > 0
            FILTER @embedding_provider == null OR (
                doc.embedding_provider == @embedding_provider
                AND doc.embedding_model == @embedding_model
                AND doc.embedding_version == @embedding_version
            )
            FILTER HAS(doc, "embedding")
            FILTER LENGTH(NOT_NULL(doc.embedding, [])) == @embedding_dimension
            LET score = COSINE_SIMILARITY(doc.embedding, @query_vector)
            FILTER score != null
            SORT score DESC
            LIMIT @limit
            RETURN MERGE(doc, {
                score: score,
                retrieval_mode: "semantic",
                fallback_used: false,
                embedding_provider_used: doc.embedding_provider,
                score_breakdown: {semantic: score}
            })
        """
        return self._run_document_query(
            query,
            request,
            {
                "query_vector": request.embedding,
                "exclude_key": request.exclude_key,
                "embedding_dimension": len(request.embedding),
                "embedding_provider": request.embedding_provider,
                "embedding_model": request.embedding_model,
                "embedding_version": request.embedding_version,
                "tags": request.tags,
            },
        )

    def hybrid_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        if not request.embedding:
            return self.search_documents(request)

        lexical = self.search_documents(request)
        semantic = self.semantic_search(request)
        if not semantic and request.embedding_provider is not None:
            return [hit.model_copy(update={"fallback_used": True}) for hit in lexical]
        ranked: dict[str, dict[str, object]] = {}
        for hit in lexical + semantic:
            key = hit.document.id or hit.document.key or hit.document.title
            current = ranked.get(key)
            if current is None:
                ranked[key] = {
                    "document": hit.document,
                    "lexical_score": hit.score if hit.retrieval_mode == "lexical" else 0.0,
                    "semantic_score": hit.score if hit.retrieval_mode == "semantic" else 0.0,
                    "fallback_used": hit.fallback_used,
                    "embedding_provider_used": hit.embedding_provider_used,
                    "metadata": dict(hit.metadata),
                }
                continue

            current["lexical_score"] = max(float(current["lexical_score"]), hit.score if hit.retrieval_mode == "lexical" else 0.0)
            current["semantic_score"] = max(float(current["semantic_score"]), hit.score if hit.retrieval_mode == "semantic" else 0.0)
            current["fallback_used"] = bool(current["fallback_used"]) or hit.fallback_used
            if hit.embedding_provider_used:
                current["embedding_provider_used"] = hit.embedding_provider_used

        fused: list[MemorySearchHit] = []
        for item in ranked.values():
            lexical_score = float(item["lexical_score"])
            semantic_score = float(item["semantic_score"])
            blended_score = round((0.45 * lexical_score) + (0.55 * semantic_score), 6)
            metadata = dict(item["metadata"])
            metadata.update({
                "lexical_score": lexical_score,
                "semantic_score": semantic_score,
                "blended_score": blended_score,
            })
            fused.append(
                MemorySearchHit(
                    document=item["document"],
                    score=blended_score,
                    retrieval_mode="hybrid",
                    embedding_provider_used=item.get("embedding_provider_used"),
                    fallback_used=bool(item["fallback_used"]),
                    score_breakdown={
                        "lexical": lexical_score,
                        "semantic": semantic_score,
                        "blended": blended_score,
                    },
                    metadata=metadata,
                )
            )
        return sorted(fused, key=lambda hit: hit.score, reverse=True)[: request.limit]

    def find_related_entities(self, request: GraphTraversalRequest) -> list[RelatedEntity]:
        query = """
        FOR vertex, edge, path IN 1..@depth OUTBOUND @seed @@edges
            PRUNE edge.relation == "adr_conflict_resolution"
            FILTER edge.relation != "adr_conflict_resolution"
            LIMIT @limit
            RETURN {
                entity_id: vertex._id,
                label: vertex.title,
                relation: edge.relation,
                depth: LENGTH(path.edges),
                score: 1.0,
                metadata: KEEP(vertex, [
                    "kind", "tags", "source", "status", "date", "effective_date",
                    "expiration_date", "last_validated", "confidence", "next_review_due"
                ])
            }
        """
        cursor = self.database.aql.execute(
            query,
            bind_vars={
                "seed": request.seed_entity_id,
                "depth": request.depth,
                "limit": request.limit,
                "@edges": self._config.edges_collection,
            },
        )
        return [RelatedEntity.model_validate(item) for item in cursor]

    def _run_document_query(
        self,
        query: str,
        request: MemorySearchRequest,
        search_bind_vars: dict[str, object],
    ) -> list[MemorySearchHit]:
        bind_vars = {
            "limit": request.limit,
            "as_of": request.as_of.isoformat() if request.as_of else None,
            "kinds": request.kinds,
            "document_keys": request.document_keys,
            "@documents": self._config.documents_collection,
            **search_bind_vars,
        }
        cursor = self.database.aql.execute(query, bind_vars=bind_vars)
        return [self._row_to_hit(item) for item in cursor]

    @staticmethod
    def _row_to_hit(row: dict[str, object]) -> MemorySearchHit:
        score = float(row.pop("score", 0.0))
        retrieval_mode = row.pop("retrieval_mode", None)
        embedding_provider_used = row.pop("embedding_provider_used", None)
        fallback_used = bool(row.pop("fallback_used", False))
        score_breakdown = row.pop("score_breakdown", {})
        metadata = row.get("metadata", {})
        document = ArangoMemoryRepository._parse_document(row)
        return MemorySearchHit(
            document=document,
            score=score,
            retrieval_mode=retrieval_mode,
            embedding_provider_used=embedding_provider_used,
            fallback_used=fallback_used,
            score_breakdown=dict(score_breakdown) if isinstance(score_breakdown, dict) else {},
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )
