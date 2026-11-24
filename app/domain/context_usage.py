from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from app.domain.architecture import CodeLocation
from app.domain.models import MemoryKey

RetrievalMethod = Literal["Lexical", "Semantic", "Hybrid", "Graph", "ContextPackage"]
UsageScope = Literal["session", "project", "lifetime"]
RequestID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class ContextQuery(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]
    project_key: MemoryKey | None = None
    entity: MemoryKey | None = None
    adr: MemoryKey | None = None
    goal: MemoryKey | None = None
    service: MemoryKey | None = None
    as_of: date | None = None
    max_context_tokens: int = Field(default=900, ge=256, le=8000)
    include_full_adrs: bool = False


class ContextItem(BaseModel):
    key: MemoryKey
    title: str
    text: str = ""
    destination: Literal["records", "entities"] = "entities"


class ContextEvidence(BaseModel):
    key: MemoryKey
    citation: str
    code_location: CodeLocation | None = None


class ContextConflict(BaseModel):
    original_key: MemoryKey
    competing_key: MemoryKey
    overlap_start: date | None = None
    overlap_end: date | None = None
    original_decision_excerpt: str | None = None
    competing_decision_excerpt: str | None = None


class ContextPackage(BaseModel):
    summary: str
    as_of: date
    relevant_adrs: list[ContextItem] = Field(default_factory=list)
    relevant_constraints: list[ContextItem] = Field(default_factory=list)
    relevant_services: list[ContextItem] = Field(default_factory=list)
    relevant_tests: list[ContextItem] = Field(default_factory=list)
    evidence: list[ContextEvidence] = Field(default_factory=list)
    conflicts: list[ContextConflict] = Field(
        default_factory=list, description="Unresolved conflicts with both cited ADRs in the package.",
    )
    budget_excluded: dict[str, int] = Field(
        default_factory=dict, description="Counts of applicable candidates omitted by package field.",
    )


class UsageOutcome(BaseModel):
    completed: bool
    follow_up_retries: int = Field(default=0, ge=0, le=1000)
    response_tokens: int | None = Field(default=None, ge=0, le=10_000_000)


class UsageReceipt(BaseModel):
    request_id: RequestID
    timestamp: AwareDatetime
    project_key: MemoryKey | None = None
    method: RetrievalMethod
    query_tokens: int
    context_tokens: int
    response_tokens: int | None = None
    total_tokens: int
    naive_rag_tokens: int | None = None
    savings_tokens: int | None = None
    entities_supplied: int
    retrieval_path: str
    outcome: UsageOutcome | None = None


class UsageSummary(BaseModel):
    scope: UsageScope
    requests: int
    measured_savings_requests: int
    total_query_tokens: int
    total_context_tokens: int
    total_reported_response_tokens: int
    total_tokens: int
    total_savings: int | None = None
    average_context_tokens: float
    average_savings: float | None = None
    methods: dict[str, int]
    reported_outcomes: int
    successful_completions: int
    average_follow_up_retries: float | None = None
    retry_reduction_estimate: float | None = None
    recent: list[UsageReceipt]
    trend: list[dict[str, str | int]]


class MeasuredContextPackage(ContextPackage):
    usage: UsageReceipt
