from datetime import date

from pydantic import BaseModel

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


class ProjectGuidance(BaseModel):
    project_key: MemoryKey
    project_name: str
    as_of: date
    decisions: list[GuidanceDecision]
    constraints: list[GuidanceConstraint]


class GuidanceScope(BaseModel):
    entity_keys: list[MemoryKey]
    adr_keys: list[MemoryKey]
