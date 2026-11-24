from __future__ import annotations

from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.domain.execution_usage import ExecutionAttempt
from app.domain.knowledge_capture import CaptureSuggestion, Evidence
from app.domain.models import MemoryKey
from app.domain.repair_contract import RepairContract

EntityName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
EntityType = Literal[
    "ADR",
    "DomainConcept",
    "Service",
    "Repository",
    "Module",
    "Class",
    "Function",
    "SourceFile",
    "ApiEndpoint",
    "DatabaseCollection",
    "TestSuite",
    "Constraint",
    "Goal",
    "Risk",
    "Observation",
    "Project",
    "Task",
    "ConversationSummary",
    "Execution",
    "Artifact",
    "Agent",
]
RelationshipType = Literal[
    "implements",
    "implemented_by",
    "defines",
    "defined_in",
    "uses",
    "used_by",
    "persists_to",
    "persisted_by",
    "constrained_by",
    "constrains",
    "validated_by",
    "validates",
    "references",
    "referenced_by",
    "supersedes",
    "superseded_by",
    "supports_goal",
    "supported_by",
    "related_to",
    "produced",
    "produced_by",
    "identified",
    "identified_by",
    "created",
    "created_by",
]

FORWARD_RELATIONSHIPS = {
    "implements": "implemented_by",
    "defines": "defined_in",
    "uses": "used_by",
    "persists_to": "persisted_by",
    "constrained_by": "constrains",
    "validated_by": "validates",
    "references": "referenced_by",
    "supersedes": "superseded_by",
    "supports_goal": "supported_by",
    "related_to": "related_to",
    "produced": "produced_by",
    "identified": "identified_by",
    "created": "created_by",
}
INVERSE_RELATIONSHIPS = FORWARD_RELATIONSHIPS | {
    inverse: forward for forward, inverse in FORWARD_RELATIONSHIPS.items()
}


def architecture_key(*parts: str) -> str:
    return sha256(dumps(parts, ensure_ascii=False).encode()).hexdigest()


class ArchitectureNode(BaseModel):
    key: MemoryKey
    name: EntityName
    description: str = ""
    source_path: str | None = None
    repository: str | None = None
    index_state: Literal["current", "stale"] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArchitectureADR(ArchitectureNode):
    entity_type: Literal["ADR"] = "ADR"
    memory_key: MemoryKey | None = None


class DomainConcept(ArchitectureNode):
    entity_type: Literal["DomainConcept"] = "DomainConcept"
    memory_key: MemoryKey | None = None


class Service(ArchitectureNode):
    entity_type: Literal["Service"] = "Service"


class Repository(ArchitectureNode):
    entity_type: Literal["Repository"] = "Repository"
    role: Literal["source", "persistence"] = "persistence"
    last_indexed_at: AwareDatetime | None = None


class Module(ArchitectureNode):
    entity_type: Literal["Module"] = "Module"
    qualified_name: str
    imports: list[str] = Field(default_factory=list)


class Class(ArchitectureNode):
    entity_type: Literal["Class"] = "Class"
    qualified_name: str


class Function(ArchitectureNode):
    entity_type: Literal["Function"] = "Function"
    qualified_name: str
    is_async: bool = False


class SourceFile(ArchitectureNode):
    entity_type: Literal["SourceFile"] = "SourceFile"
    source_path: str


class ApiEndpoint(ArchitectureNode):
    entity_type: Literal["ApiEndpoint"] = "ApiEndpoint"
    method: EntityName
    path: Annotated[str, StringConstraints(pattern=r"^/")]


class DatabaseCollection(ArchitectureNode):
    entity_type: Literal["DatabaseCollection"] = "DatabaseCollection"


class TestSuite(ArchitectureNode):
    entity_type: Literal["TestSuite"] = "TestSuite"


ConstraintRule = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)
]
EnforcementStatus = Literal["proposed", "unenforced", "partially_enforced", "enforced", "retired"]
EntityKeys = Annotated[list[MemoryKey], Field(max_length=100)]


class ConstraintInput(BaseModel):
    key: MemoryKey
    name: EntityName
    rule: ConstraintRule
    scope: EntityName
    authority: EntityName
    enforcement_status: EnforcementStatus = "proposed"
    constrained_entity_keys: EntityKeys = Field(default_factory=list)
    test_suite_keys: EntityKeys = Field(default_factory=list)


class Constraint(ArchitectureNode):
    entity_type: Literal["Constraint"] = "Constraint"
    rule: ConstraintRule | None = None
    scope: EntityName | None = None
    authority: EntityName | None = None
    enforcement_status: EnforcementStatus | None = None
    constrained_entity_keys: EntityKeys = Field(default_factory=list)
    test_suite_keys: EntityKeys = Field(default_factory=list)


class Project(ArchitectureNode):
    entity_type: Literal["Project"] = "Project"
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


TaskStatus = Literal["open", "in_progress", "done", "cancelled"]


class Task(ArchitectureNode):
    entity_type: Literal["Task"] = "Task"
    status: TaskStatus = "open"


GoalStatus = Literal["proposed", "active", "achieved", "archived"]


class GoalDetails(BaseModel):
    status: GoalStatus = "active"
    success_criteria: list[EntityName] = Field(default_factory=list, max_length=50)
    adr_keys: EntityKeys = Field(default_factory=list)
    service_keys: EntityKeys = Field(default_factory=list)
    task_keys: EntityKeys = Field(default_factory=list)


class ProjectGoalInput(GoalDetails):
    key: MemoryKey
    name: EntityName
    description: str = ""
    project_key: MemoryKey


class Goal(ArchitectureNode, GoalDetails):
    entity_type: Literal["Goal"] = "Goal"
    project_key: MemoryKey | None = None


class ConversationDetails(BaseModel):
    source: EntityName | None = None
    summary: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000)
    ]
    decision_keys: EntityKeys = Field(default_factory=list)
    risk_keys: EntityKeys = Field(default_factory=list)
    task_keys: EntityKeys = Field(default_factory=list)


class ConversationSummaryInput(ConversationDetails):
    model_config = ConfigDict(extra="forbid")

    key: MemoryKey | None = None


class ConversationSummary(ArchitectureNode, ConversationDetails):
    entity_type: Literal["ConversationSummary"] = "ConversationSummary"
    timestamp: AwareDatetime
    recorded_by: str | None = None


class Risk(ArchitectureNode):
    entity_type: Literal["Risk"] = "Risk"


ObservationSummary = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16_000)
]


class ObservationInput(BaseModel):
    key: MemoryKey | None = None
    source: EntityName
    summary: ObservationSummary
    confidence: float = Field(ge=0, le=1)
    related_entity_keys: list[MemoryKey] = Field(min_length=1, max_length=100)


class Observation(ArchitectureNode):
    entity_type: Literal["Observation"] = "Observation"
    source: EntityName
    summary: ObservationSummary
    confidence: float = Field(ge=0, le=1)
    timestamp: AwareDatetime
    related_entity_keys: list[MemoryKey] = Field(min_length=1, max_length=100)
    recorded_by: str | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=50)
    suggestions: list[CaptureSuggestion] = Field(default_factory=list, max_length=50)


class Execution(ArchitectureNode):
    entity_type: Literal["Execution"] = "Execution"
    project_key: MemoryKey
    task_key: MemoryKey
    patch_execution_key: MemoryKey | None = None
    approved_review_key: MemoryKey | None = None
    approved_patch: dict[str, str] | None = None
    guided_repair: bool = False
    repair_review_key: MemoryKey | None = None
    repair_patch_sha256: str | None = None
    repair_prompt: str | None = None
    repair_direction: str | None = None
    repair_contract: RepairContract | None = None
    repair_token_ceiling: int | None = None
    approve_review_attempt: bool = False
    additional_review_tokens: int | None = None
    agent: Literal["local", "codex", "claude-code", "openhands"]
    mode: Literal["PLAN_ONLY", "IMPLEMENT", "REVIEW", "FULL_LIFECYCLE", "APPLY_APPROVED"]
    state: Literal[
        "BACKLOG", "PLANNED", "READY", "RUNNING", "BLOCKED", "REVIEW", "COMPLETED", "FAILED",
        "CANCEL_REQUESTED", "CANCELLED",
    ] = "READY"
    phase: int = 0
    revision: int = 0
    requested_by: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    lease_until: AwareDatetime | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    context_keys: list[MemoryKey] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[ExecutionAttempt] = Field(default_factory=list)
    failure: str | None = None


class Artifact(ArchitectureNode):
    entity_type: Literal["Artifact"] = "Artifact"
    project_key: MemoryKey
    reference: str


class Agent(ArchitectureNode):
    entity_type: Literal["Agent"] = "Agent"
    project_key: MemoryKey


ArchitectureEntity = Annotated[
    ArchitectureADR
    | DomainConcept
    | Service
    | Repository
    | Module
    | Class
    | Function
    | SourceFile
    | ApiEndpoint
    | DatabaseCollection
    | TestSuite
    | Constraint
    | Goal
    | Risk
    | Observation
    | Project
    | Task
    | ConversationSummary
    | Execution
    | Artifact
    | Agent,
    Field(discriminator="entity_type"),
]


class ArchitectureLink(BaseModel):
    source_key: MemoryKey
    target_key: MemoryKey
    relationship_type: RelationshipType
    confidence: float = Field(default=1, ge=0, le=1)
    source: EntityName


class ArchitectureRelationship(ArchitectureLink):
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ArchitecturePath(BaseModel):
    entity: ArchitectureEntity
    nodes: list[ArchitectureEntity]
    relationships: list[ArchitectureRelationship]


class ArchitectureSearch(BaseModel):
    query: str = ""
    entity_type: EntityType | None = None
    limit: int = Field(default=20, ge=1, le=100)
    include_stale: bool = False


MAX_GRAPH_HOPS = 10
DEFAULT_GRAPH_HOPS = 4


class ArchitectureTrace(BaseModel):
    key: MemoryKey
    depth: int = Field(default=1, ge=1, le=MAX_GRAPH_HOPS)
    min_depth: int = Field(default=1, ge=1, le=MAX_GRAPH_HOPS)
    prioritize: bool = False
    shortest_paths: bool = False
    limit: int = Field(default=20, ge=1, le=100)
    relationship_types: list[RelationshipType] = Field(default_factory=list)
    entity_types: list[EntityType] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_depth_range(self) -> ArchitectureTrace:
        if self.min_depth > self.depth:
            raise ValueError("Minimum depth must not exceed maximum depth")
        return self


class ArchitectureMetamodel(BaseModel):
    entity_types: list[EntityType]
    relationships: dict[RelationshipType, RelationshipType]


class ArchitectureNeighborhood(BaseModel):
    center: ArchitectureEntity
    paths: list[ArchitecturePath]
    truncated: bool


class ArchitectureIndex(BaseModel):
    repository: EntityName
    entities: list[ArchitectureEntity]
    links: list[ArchitectureLink]


class ArchitectureIndexResult(BaseModel):
    repository: str
    repository_key: MemoryKey
    files_indexed: int
    entities_indexed: int
    relationships_indexed: int
