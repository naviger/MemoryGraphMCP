"""Project workspace commands and adapter results."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.domain.architecture import EntityName, TaskStatus
from app.domain.execution_usage import ExecutionUsage
from app.domain.models import MemoryKey

WorkType = Literal["Task", "Goal", "Risk", "Constraint"]
AgentName = Literal["local", "codex", "claude-code", "openhands"]
ExecutionMode = Literal["PLAN_ONLY", "IMPLEMENT", "REVIEW", "FULL_LIFECYCLE"]
WorkText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class WorkDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: WorkType
    name: EntityName
    description: WorkText
    scope: EntityName | None = None
    authority: EntityName | None = None

    @model_validator(mode="after")
    def check_constraint(self):
        if self.kind == "Constraint" and (
            len(self.description) > 4000 or not self.scope or not self.authority
        ):
            raise ValueError(
                "Constraints require scope, authority, and a rule of at most 4,000 characters"
            )
        return self


class AssistDraft(BaseModel):
    kind: WorkType
    brief: WorkText


class StartExecution(BaseModel):
    request_id: MemoryKey
    agent: AgentName
    mode: ExecutionMode


class TaskUpdate(BaseModel):
    status: TaskStatus
    outcome: WorkText | None = None


class AgentArtifact(BaseModel):
    name: EntityName
    reference: WorkText


class AgentResult(BaseModel):
    summary: WorkText
    plan: WorkText | None = None
    files_modified: list[WorkText] = Field(default_factory=list, max_length=50)
    pull_request: WorkText | None = None
    review_comments: list[WorkText] = Field(default_factory=list, max_length=50)
    logs: list[WorkText] = Field(default_factory=list, max_length=50)
    artifacts: list[AgentArtifact] = Field(default_factory=list, max_length=50)
    tests: list[WorkText] = Field(default_factory=list, max_length=50)
    lesson: WorkText | None = None
    related_keys: list[MemoryKey] = Field(default_factory=list, max_length=50)
    completed: bool = False
    usage: ExecutionUsage | None = None
