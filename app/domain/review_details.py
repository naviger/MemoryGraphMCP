from __future__ import annotations

from datetime import date as CalendarDate
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.domain.architecture import ArchitectureEntity, EntityType, RelationshipType
from app.domain.models import MemoryKey, MemoryKind, MemoryValidation, MemoryValidity


class ReviewRecord(MemoryValidity):
    key: str
    title: str
    kind: MemoryKind
    status: str | None = None
    content: str
    source: str | None = None
    source_id: str | None = None
    date: CalendarDate | None = None
    decision: str | None = None
    rationale: str | None = None
    consequences: str | None = None
    next_review_due: CalendarDate | None = None
    validation_history: list[MemoryValidation] = Field(default_factory=list)


class ReviewEntityLink(BaseModel):
    key: str
    destination: Literal["records", "entities"]
    entity_type: EntityType
    name: str
    source_path: str | None = None
    index_state: Literal["current", "stale"] | None = None
    relationships: list[RelationshipType]


class ReviewSearchQuery(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    mode: Literal["semantic", "hybrid"] = "hybrid"
    kinds: list[MemoryKind] = Field(default_factory=list, max_length=5)
    project_key: MemoryKey | None = None
    as_of: CalendarDate | None = None
    limit: int = Field(default=20, ge=1, le=50)


class ReviewSearchMatch(BaseModel):
    key: str
    title: str
    excerpt: str
    kind: MemoryKind
    source: str | None = None
    source_id: str | None = None
    effective_date: CalendarDate | None = None
    expiration_date: CalendarDate | None = None


class SearchEmbeddingDiagnostics(BaseModel):
    provider: str | None = None
    model: str | None = None
    dimension: int | None = None
    version: str | None = None


class RecordEmbeddingDiagnostics(SearchEmbeddingDiagnostics):
    state: Literal["matches", "mismatch", "missing", "unknown"]


class ReviewEntityDetail(BaseModel):
    record: ReviewRecord | None = None
    entity: ArchitectureEntity | None = None
    links: list[ReviewEntityLink] = Field(default_factory=list)
    embedding: RecordEmbeddingDiagnostics | None = None


class ReviewSearchResults(BaseModel):
    matches: list[ReviewSearchMatch]
    fallback_used: bool
    embedding: SearchEmbeddingDiagnostics | None = None


class ProjectDecisionLink(BaseModel):
    key: str
    name: str
    relationship: Literal["implemented_by", "validated_by"]


class ProjectEntity(BaseModel):
    key: str
    name: str
    entity_type: EntityType
    source_path: str | None = None
    memory_key: str | None = None
    repository: str | None = None
    role: Literal["source", "persistence"] | None = None
    qualified_name: str | None = None
    parent_key: str | None = None
    status: str | None = None
    reason: str
    decision_links: list[ProjectDecisionLink] = Field(default_factory=list)


class ProjectScope(BaseModel):
    entities: list[ProjectEntity]
    dependencies: list[ProjectEntity]
    repository_names: list[str]
    earliest_record: CalendarDate | None = None
    latest_record: CalendarDate | None = None
    last_index_date: CalendarDate | None = None


class ProjectDocument(BaseModel):
    key: str
    title: str
    kind: MemoryKind
    reason: str
    source: str | None = None
    status: str | None = None
    effective_date: CalendarDate | None = None
    expiration_date: CalendarDate | None = None
    recorded_date: CalendarDate | None = None
    last_recorded_update: CalendarDate | None = None


class ProjectSummary(BaseModel):
    open_tasks: int
    proposed_adrs: int
    earliest_record: CalendarDate | None = None
    latest_record: CalendarDate | None = None
    last_index_date: CalendarDate | None = None


class ProjectOverview(BaseModel):
    key: str
    name: str
    description: str
    entities: list[ProjectEntity]
    dependencies: list[ProjectEntity]
    documents: list[ProjectDocument]


class SimilarRecord(BaseModel):
    key: str
    title: str
    kind: MemoryKind
    similarity: float


class SimilarRecords(BaseModel):
    matches: list[SimilarRecord] = Field(default_factory=list)
    available: bool = True
