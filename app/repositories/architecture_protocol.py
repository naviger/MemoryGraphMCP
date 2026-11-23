from typing import Protocol

from app.domain.architecture import (
    ArchitectureEntity,
    ArchitectureIndex,
    ArchitectureLink,
    ArchitecturePath,
    ArchitectureRelationship,
    ArchitectureSearch,
    ArchitectureTrace,
    Constraint,
    ConstraintInput,
    ConversationSummary,
    EntityType,
    Goal,
    Observation,
)
from app.domain.project_guidance import GuidanceScope


class ArchitectureRepository(Protocol):
    def ensure_storage_ready(self) -> None: ...

    def guidance_scope(self, project_key: str) -> GuidanceScope: ...

    def guidance_constraints(self, scoped_keys: list[str]) -> list[Constraint]: ...

    def put_entity(self, entity: ArchitectureEntity) -> ArchitectureEntity: ...

    def add_observation(self, observation: Observation) -> Observation: ...

    def save_architectural_constraint(self, draft: ConstraintInput) -> Constraint: ...

    def save_project_goal(self, goal: Goal) -> Goal: ...

    def add_conversation_summary(
        self, conversation: ConversationSummary
    ) -> ConversationSummary: ...

    def put_link(self, link: ArchitectureLink) -> ArchitectureRelationship: ...

    def replace_index(self, index: ArchitectureIndex) -> int: ...

    def read_entity(self, key: str) -> ArchitectureEntity | None: ...

    def find_memory_entities(self, key: str) -> list[ArchitectureEntity]: ...

    def search(self, request: ArchitectureSearch) -> list[ArchitectureEntity]: ...

    def count_by_entity_type(
        self, *, source_repositories_only: bool = False
    ) -> dict[EntityType, int]: ...

    def trace(self, request: ArchitectureTrace) -> list[ArchitecturePath]: ...
