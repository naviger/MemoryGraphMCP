from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from app.domain.architecture import EnforcementStatus
from app.domain.models import EvidenceCitation, MemoryKey


class GuidanceDecision(BaseModel):
    record_key: MemoryKey
    title: str
    decision: str
    rationale: str
    effective_date: date | None = None
    expiration_date: date | None = None
    last_validated: date | None = None
    source: str | None = None
    source_id: str | None = None
    evidence: list[EvidenceCitation]


class GuidanceConstraint(BaseModel):
    entity_key: MemoryKey
    name: str
    rule: str
    scope: str | None = None
    authority: str | None = None
    enforcement_status: EnforcementStatus
    constrained_entity_keys: list[MemoryKey]
    validating_test_keys: list[MemoryKey]


GuidanceText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]


class GuidanceDraftInput(BaseModel):
    text: GuidanceText
    revision: int = Field(ge=0)


class GuidanceApproval(BaseModel):
    revision: int = Field(ge=1)
    approve: Literal[True]


class GuidanceDraft(BaseModel):
    text: GuidanceText
    edited_by: str
    edited_at: AwareDatetime


class PublishedGuidance(GuidanceDraft):
    version: int = Field(ge=1)
    approved_by: str
    published_at: AwareDatetime


class GuidanceState(BaseModel):
    revision: int = 0
    draft: GuidanceDraft | None = None
    published: PublishedGuidance | None = None


class GuidanceEditor(GuidanceState):
    can_edit: bool
    can_publish: bool
    history: list[PublishedGuidance]
    next_before_version: int | None = None


class ProjectGuidance(BaseModel):
    project_key: MemoryKey
    project_name: str
    as_of: date
    decisions: list[GuidanceDecision]
    constraints: list[GuidanceConstraint]
    global_guidance: PublishedGuidance | None = None
    published_guidance: PublishedGuidance | None = Field(default=None, description="Published project guidance")


class GuidanceScope(BaseModel):
    entity_keys: list[MemoryKey]
    adr_keys: list[MemoryKey]
