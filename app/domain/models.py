from __future__ import annotations

from datetime import date as CalendarDate
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    StringConstraints,
    model_validator,
)

from app.domain.keys import MemoryKey as MemoryKey
from app.domain.knowledge_capture import CaptureReviewItem

MemoryKind = Literal["memory", "architecture", "adr", "design-language", "domain-concept"]
SearchMode = Literal["lexical", "semantic", "hybrid"]
ConfidenceScore = Annotated[float, Field(ge=0, le=1)]


class MemoryValidity(BaseModel):
    effective_date: CalendarDate | None = Field(
        default=None, description="First date on which the memory applies, inclusive."
    )
    expiration_date: CalendarDate | None = Field(
        default=None, description="First date on which the memory no longer applies, exclusive."
    )
    last_validated: CalendarDate | None = Field(
        default=None, description="Most recent date on which the memory was checked against evidence."
    )
    confidence: ConfidenceScore | None = Field(
        default=None, description="Confidence in the memory on a 0–1 scale."
    )

    @model_validator(mode="after")
    def validate_validity_window(self) -> MemoryValidity:
        if self.effective_date and self.expiration_date and self.effective_date >= self.expiration_date:
            raise ValueError("expiration_date must be after effective_date")
        return self


EvidenceCitation = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class MemoryValidation(BaseModel):
    reviewer: str = Field(min_length=1)
    checked_at: AwareDatetime
    evidence: list[EvidenceCitation] = Field(min_length=1)
    previous_confidence: ConfidenceScore | None = None
    new_confidence: ConfidenceScore | None = None


class MemoryRevalidationResult(BaseModel):
    key: str
    validation: MemoryValidation
    last_validated: CalendarDate
    next_review_due: CalendarDate


class MemoryDocument(MemoryValidity):
    id: str | None = Field(default=None, validation_alias=AliasChoices("_id", "id"))
    key: str | None = Field(default=None, validation_alias=AliasChoices("_key", "key"))
    kind: MemoryKind = "memory"
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    source_id: str | None = None
    namespace: str | None = None
    version: int = 1
    status: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    embedding_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    next_review_due: CalendarDate | None = None
    validation_history: list[MemoryValidation] = Field(default_factory=list)


ADRStatus = Literal["proposed", "accepted", "superseded", "rejected"]


ScopeLabel = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=120),
]


class ADRConflictScope(BaseModel):
    topic: ScopeLabel
    system: ScopeLabel | None = None
    environment: ScopeLabel | None = None

    @model_validator(mode="after")
    def require_system_or_environment(self) -> ADRConflictScope:
        if self.system is None and self.environment is None:
            raise ValueError("A conflict scope must name a system or environment")
        return self


class ADRCreate(MemoryValidity):
    model_config = ConfigDict(str_strip_whitespace=True)

    key: MemoryKey
    title: str = Field(min_length=1)
    status: ADRStatus = "proposed"
    tags: list[str] = Field(default_factory=list)
    decision: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    consequences: str = Field(min_length=1)
    date: CalendarDate
    conflict_scope: ADRConflictScope | None = None


class ArchitecturalDecisionRecord(ADRCreate, MemoryDocument):
    key: MemoryKey = Field(validation_alias=AliasChoices("_key", "key"))
    kind: Literal["adr"] = "adr"


class ADRConflictRecord(ADRCreate):
    key: MemoryKey = Field(validation_alias=AliasChoices("_key", "key"))
    id: str | None = Field(default=None, validation_alias=AliasChoices("_id", "id"))
    validation_history: list[MemoryValidation] = Field(default_factory=list)
    source: str | None = None
    source_id: str | None = None


class ADRConflict(BaseModel):
    original: ADRConflictRecord
    competing: ADRConflictRecord
    overlap_start: CalendarDate | None
    overlap_end: CalendarDate | None


ADRResolutionAction = Literal["supersede", "coexist"]
ADRResolutionRationale = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ADRResolutionDecision(BaseModel):
    action: ADRResolutionAction
    rationale: ADRResolutionRationale
    resolved_by: str = Field(min_length=1)
    resolved_at: AwareDatetime


class ADRConflictResolution(ADRResolutionDecision):
    source_key: MemoryKey
    target_key: MemoryKey
    overlap_start: CalendarDate | None
    overlap_end: CalendarDate | None
    system: ScopeLabel | None = None
    environment: ScopeLabel | None = None


class ResolvedADRConflict(ADRConflict):
    resolution: ADRConflictResolution


class ADRAcceptanceConflictCheck(BaseModel):
    status: Literal["checked", "missing_scope"]
    conflicts: list[ADRConflict] = Field(default_factory=list)
    has_more: bool = False


class ADRWriteResult(ArchitecturalDecisionRecord):
    conflict_check: ADRAcceptanceConflictCheck | None = None


class ADRConflictQuery(BaseModel):
    key: MemoryKey
    as_of: CalendarDate | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ADRConflictListQuery(BaseModel):
    system: ScopeLabel | None = None
    environment: ScopeLabel | None = None
    as_of: CalendarDate | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ReviewQueueQuery(BaseModel):
    project_key: MemoryKey | None = None
    as_of: CalendarDate | None = None
    limit: int = Field(default=20, ge=1, le=100)
    validation_offset: int = Field(default=0, ge=0)
    conflict_offset: int = Field(default=0, ge=0)


class OverdueValidation(MemoryValidity):
    key: MemoryKey
    title: str
    kind: MemoryKind
    status: str | None = None
    next_review_due: CalendarDate
    last_validation: MemoryValidation | None = None


class ReviewQueue(BaseModel):
    review_date: CalendarDate
    overdue_validations: list[OverdueValidation]
    unresolved_adr_conflicts: list[ADRConflict]
    inferred_relationships: list[CaptureReviewItem] = Field(default_factory=list)


class ADRSupersession(BaseModel):
    source_id: str
    target_id: str
    relation: Literal["superseded_by"] = "superseded_by"


class MemorySearchRequest(BaseModel):
    query: str
    as_of: CalendarDate | None = None
    limit: int = Field(default=10, ge=1, le=100)
    kinds: list[MemoryKind] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mode: SearchMode = "lexical"
    exclude_key: MemoryKey | None = None
    document_keys: list[str] | None = None
    embedding: list[float] | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_version: str | None = None


class MemorySearchHit(BaseModel):
    document: SerializeAsAny[MemoryDocument]
    score: float = 0.0
    highlights: list[str] = Field(default_factory=list)
    retrieval_mode: SearchMode | None = None
    embedding_provider_used: str | None = None
    fallback_used: bool = False
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelatedEntity(BaseModel):
    entity_id: str
    label: str
    relation: str
    depth: int = 1
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphTraversalRequest(BaseModel):
    seed_entity_id: str
    depth: int = Field(default=2, ge=1, le=6)
    limit: int = Field(default=20, ge=1, le=100)
