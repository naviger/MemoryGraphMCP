from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.domain.models import MemoryKey

RunStatus = Literal["passed", "failed", "cancelled"]
TestLabel = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
TestRunID = Annotated[str, StringConstraints(pattern=r"^[0-9]+:[0-9]+$", max_length=64)]


class TestFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: TestLabel
    source_path: TestLabel | None = None
    kind: Literal["failure", "error"]
    exception_type: Annotated[str, StringConstraints(
        max_length=100, pattern=r"^[A-Za-z_][A-Za-z_0-9]*$"
    )] | None = None


class TestWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: TestLabel
    source_path: TestLabel | None = None
    message: TestLabel
    stage: Literal["build", "test"]


class TestRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: TestRunID
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    finished_at: AwareDatetime
    status: RunStatus
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failures: list[TestFailure] = Field(max_length=500)
    build_status: Literal["passed", "failed", "skipped"] | None = None
    warning_count: int | None = Field(default=None, ge=0)
    warnings: list[TestWarning] = Field(default_factory=list, max_length=100)

    @field_validator("finished_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_counts(self) -> "TestRunInput":
        if len(self.failures) > self.failed or (self.status == "passed" and self.failed):
            raise ValueError("Failure details and status must agree with the failed count")
        if self.warnings and (self.warning_count is None or len(self.warnings) > self.warning_count):
            raise ValueError("Warning details must agree with the warning count")
        return self


class TestRun(TestRunInput):
    project_key: MemoryKey
    repository_key: MemoryKey
    source: Literal["github-actions"] = "github-actions"


class TestValidationLink(BaseModel):
    key: MemoryKey
    name: str
    entity_type: Literal["ADR", "Constraint"]
    memory_key: MemoryKey | None = None


class TestFailureDetail(TestFailure):
    validations: list[TestValidationLink] = Field(default_factory=list)


class TestRunDetail(TestRun):
    failures: list[TestFailureDetail]


class LastPassingRun(BaseModel):
    run_id: TestRunID
    finished_at: AwareDatetime
    run_url: str | None = None


class ProjectTestRuns(BaseModel):
    latest_run: TestRunDetail | None = None
    run_url: str | None = None
    can_suggest_fix: bool = False
    can_review_fix: bool = False
    can_draft_fix: bool = False
    can_apply_draft: bool = False
    can_complete_fix: bool = False
    can_publish_fix: bool = False
    last_passing_run: LastPassingRun | None = None
    health: Literal["red", "yellow", "green", "unknown"] = "unknown"


class ProjectCIHealth(BaseModel):
    health: Literal["red", "yellow", "green", "unknown"]
    freshness: Literal["current", "stale", "unverified", "unknown"]
    head_commit: str | None = None
    run_commit: str | None = None


def project_ci_health(run: TestRun | None, head_commit: str | None) -> ProjectCIHealth:
    health = ci_run_health(run)
    freshness = "unknown"
    if head_commit:
        if run is None:
            freshness = "unverified"
        elif run.commit != head_commit:
            freshness = "stale"
            health = "red" if health == "red" else "yellow"
        elif run.build_status == "failed" or run.status == "cancelled" or not (
            run.passed or run.failed
        ):
            freshness = "unverified"
        else:
            freshness = "current"
    return ProjectCIHealth(
        health=health, freshness=freshness, head_commit=head_commit,
        run_commit=run.commit if run else None,
    )


def ci_run_health(run: TestRun | None) -> Literal["red", "yellow", "green", "unknown"]:
    if run is None:
        return "unknown"
    if run.status == "failed" or run.build_status == "failed" or run.failed:
        return "red"
    if run.build_status != "passed" or run.status != "passed" or run.warning_count is None:
        return "unknown"
    if run.warning_count:
        return "yellow"
    return "green" if run.passed else "unknown"


class TestRunSummary(BaseModel):
    run_id: TestRunID
    commit: str
    finished_at: AwareDatetime
    status: RunStatus
    passed: int
    failed: int
    skipped: int
    warning_count: int | None = None
    build_status: Literal["passed", "failed", "skipped"] | None = None


class TestRunHistory(BaseModel):
    runs: list[TestRunSummary]
    next_cursor: str | None = None


class FailingAttempt(BaseModel):
    run_id: TestRunID
    finished_at: AwareDatetime


class RecurringTestFailure(BaseModel):
    name: TestLabel
    source_path: TestLabel | None = None
    attempts: list[FailingAttempt]


class RecurringTestFailures(BaseModel):
    window_size: int
    attempts_examined: int
    failures: list[RecurringTestFailure]


class TestFixRequest(BaseModel):
    run_id: TestRunID
    failure_index: int = Field(ge=0, lt=500)
    issue_type: Literal["failure", "warning"] = "failure"
