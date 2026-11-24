from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

from app.domain.keys import MemoryKey

CaptureSource = Literal[
    "commit", "pull_request", "adr", "ci", "task", "index", "conversation",
    "constraint", "agent_reflection",
]
Evidence = Annotated[str, StringConstraints(min_length=1, max_length=1200)]


SuggestionType = Literal["implements", "validates", "persists_to", "supports_goal", "references"]


class SuggestedRelationship(BaseModel):
    source_key: MemoryKey
    target_key: MemoryKey
    relationship_type: SuggestionType


class CaptureSuggestion(SuggestedRelationship):
    confidence: float = Field(ge=0, le=1)
    evidence: Evidence


class SuggestionDecision(SuggestedRelationship):
    action: Literal["accept", "reject", "replace"]
    rationale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    replacement: SuggestedRelationship | None = None

    @model_validator(mode="after")
    def validate_replacement(self):
        if (self.action == "replace") != (self.replacement is not None):
            raise ValueError("Provide a replacement only when replacing a suggestion")
        if self.replacement and self.replacement == SuggestedRelationship.model_validate({
            "source_key": self.source_key, "target_key": self.target_key,
            "relationship_type": self.relationship_type,
        }):
            raise ValueError("The replacement must differ from the original relationship")
        return self


class SuggestionResolution(BaseModel):
    project_key: MemoryKey
    observation_key: MemoryKey
    suggestion: CaptureSuggestion
    decision: SuggestionDecision
    reviewer: str
    resolved_at: AwareDatetime
    relationship_key: MemoryKey | None = None


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
