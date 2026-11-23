from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.domain.models import MemoryKey

Evidence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
ActorKind = Literal["agent", "human"]
AvoidanceKind = Literal["rework", "review", "onboarding"]


class HoursEvidence(BaseModel):
    category: AvoidanceKind
    hours: float = Field(gt=0, le=1000)
    confidence: float = Field(ge=0, le=1)
    evidence: Evidence


class DevelopmentOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_key: MemoryKey
    repository_key: MemoryKey
    change_id: Evidence
    actor: ActorKind
    occurred_at: AwareDatetime
    evidence: Evidence
    first_pass_success: bool | None = None
    retries: int | None = Field(default=None, ge=0, le=1000)
    task_key: MemoryKey | None = None
    task_outcome: Literal["completed", "abandoned"] | None = None
    task_started_at: AwareDatetime | None = None
    violated_constraints: list[MemoryKey] = Field(default_factory=list, max_length=50)
    avoided_effort: list[HoursEvidence] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_evidence(self):
        if self.occurred_at > datetime.now(UTC):
            raise ValueError("Outcome time cannot be in the future")
        if bool(self.task_outcome) != bool(self.task_key):
            raise ValueError("Task outcomes require a linked task and outcome")
        if self.task_started_at and (not self.task_outcome or self.task_started_at > self.occurred_at):
            raise ValueError("Task start must precede its reported outcome")
        if self.first_pass_success and self.retries:
            raise ValueError("A successful first pass cannot have follow-up retries")
        if len({item.category for item in self.avoided_effort}) != len(self.avoided_effort):
            raise ValueError("Report each effort category once per change")
        self.occurred_at = self.occurred_at.astimezone(UTC)
        if self.task_started_at:
            self.task_started_at = self.task_started_at.astimezone(UTC)
        self.violated_constraints = sorted(set(self.violated_constraints))
        self.avoided_effort.sort(key=lambda item: item.category)
        return self


class OutcomeReceipt(BaseModel):
    key: MemoryKey
    outcome: DevelopmentOutcome
    recorded_at: AwareDatetime
    recorded_by: str


class EffectivenessQuery(BaseModel):
    project_key: MemoryKey
    repository_key: MemoryKey | None = None
    start: date | None = None
    end: date | None = None
    input_price_per_million: float | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def ordered_period(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start date must not be after end date")
        return self


class Rate(BaseModel):
    numerator: int
    denominator: int
    percent: float | None


class Estimate(BaseModel):
    value: float | None
    confidence: float | None
    samples: int
    methodology: str


class EffectivenessReport(BaseModel):
    project_key: MemoryKey
    project_name: str
    repository_key: MemoryKey | None
    repository_name: str | None
    repositories: dict[str, str]
    start: date | None
    end: date | None
    inventory: dict[str, int]
    delivery: dict[str, int | float | None]
    architecture: dict[str, int | Rate]
    capture: dict[str, int]
    health: dict[str, Rate]
    economics: dict[str, Estimate]
    methodology: dict[str, str]
