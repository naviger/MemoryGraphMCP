from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from app.domain.keys import MemoryKey

CaptureSource = Literal[
    "commit", "pull_request", "adr", "ci", "task", "index", "conversation",
    "constraint", "agent_reflection",
]
Evidence = Annotated[str, StringConstraints(min_length=1, max_length=1200)]


class CaptureSuggestion(BaseModel):
    source_key: MemoryKey
    target_key: MemoryKey
    relationship_type: Literal["implements", "validates", "persists_to", "supports_goal", "references"]
    confidence: float = Field(ge=0, le=1)
    evidence: Evidence


class ChangeReflectionInput(BaseModel):
    project_key: MemoryKey
    diff: str = Field(max_length=250_000)
    rationale: str = Field(default="", max_length=4000)
    test_results: str = Field(default="", max_length=4000)
    linked_entities: list[MemoryKey] = Field(default_factory=list, max_length=50)


class CommitCapture(BaseModel):
    sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
    timestamp: AwareDatetime
    subject: str = Field(max_length=500)
    changed_files: list[str] = Field(max_length=500)
    diff: str = Field(max_length=250_000)
    truncated: bool = False


class CaptureEvent(BaseModel):
    source: CaptureSource
    event_id: str = Field(min_length=1, max_length=500)
    project_key: MemoryKey
    timestamp: AwareDatetime
    summary: str = Field(min_length=1, max_length=4000)
    related_keys: list[MemoryKey] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list, max_length=50)
    suggestions: list[CaptureSuggestion] = Field(default_factory=list, max_length=50)


class CaptureReviewItem(BaseModel):
    key: MemoryKey
    project_key: MemoryKey
    summary: str
    timestamp: AwareDatetime
    suggestions: list[CaptureSuggestion]


class CommitEnvelope(BaseModel):
    commit: CommitCapture
    python_snapshot: dict[str, str] | None = None
    queued_at: AwareDatetime | None = None
