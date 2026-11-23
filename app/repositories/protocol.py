from __future__ import annotations

from datetime import date
from typing import Protocol

from app.domain.models import (
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


class MemoryRepository(Protocol):
    def write_document(self, document: MemoryDocument) -> MemoryDocument:
        ...

    def read_document(self, document_key: str) -> MemoryDocument | None:
        ...

    def list_adr_supersession_links(self, key: str) -> list[ReviewEntityLink]:
        ...

    def create_adr(self, record: ArchitecturalDecisionRecord) -> ADRWriteResult:
        ...

    def read_adr(self, key: str) -> ArchitecturalDecisionRecord:
        ...

    def set_adr_status(self, key: str, status: ADRStatus) -> ADRWriteResult:
        ...

    def set_adr_conflict_scope(
        self, key: str, scope: ADRConflictScope | None,
    ) -> ArchitecturalDecisionRecord:
        ...

    def find_adr_conflicts(self, request: ADRConflictQuery) -> list[ADRConflict]:
        ...

    def list_adr_conflicts(
        self, request: ADRConflictListQuery, *, document_keys: list[str] | None = None
    ) -> list[ADRConflict]:
        ...

    def list_overdue_validations(
        self, as_of: date, limit: int, offset: int, *, document_keys: list[str] | None = None,
    ) -> list[OverdueValidation]:
        ...

    def list_resolved_adr_conflicts(self, request: ADRConflictListQuery) -> list[ResolvedADRConflict]:
        ...

    def resolve_adr_conflict(
        self, source_key: str, target_key: str, decision: ADRResolutionDecision,
    ) -> ADRConflictResolution:
        ...

    def get_adr_conflict_resolution(
        self, source_key: str, target_key: str,
    ) -> ADRConflictResolution:
        ...

    def supersede_adr(self, source_key: str, target_key: str) -> ADRSupersession:
        ...

    def revalidate_memory(
        self, key: str, validation: MemoryValidation, confidence: ConfidenceScore | None = None,
    ) -> MemoryRevalidationResult:
        ...

    def read_guidance_decisions(self, keys: list[str], as_of: date) -> list[GuidanceDecision]: ...

    def project_documents(
        self, project_key: str, repository_names: list[str], linked_keys: list[str],
        query: str, as_of: date | None,
    ) -> list[ProjectDocument]:
        ...

    def list_documents(self) -> list[MemoryDocument]:
        ...

    def search_question_documents(
        self, terms: str, as_of: date, limit: int, *, document_keys: list[str] | None = None,
    ) -> list[MemoryDocument]:
        ...

    def search_documents(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def semantic_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def hybrid_search(self, request: MemorySearchRequest) -> list[MemorySearchHit]:
        ...

    def upsert_vector_document(self, document: MemoryDocument) -> MemoryDocument:
        ...

    def find_related_entities(self, request: GraphTraversalRequest) -> list[RelatedEntity]:
        ...
