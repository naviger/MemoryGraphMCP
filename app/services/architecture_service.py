from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import anyio

from app.domain.architecture import (
    ArchitectureADR,
    ArchitectureEntity,
    ArchitectureIndexResult,
    ArchitectureSearch,
    Constraint,
    ConstraintInput,
    ConversationSummary,
    ConversationSummaryInput,
    DomainConcept,
    Goal,
    Observation,
    ObservationInput,
    Project,
    ProjectGoalInput,
    Repository,
    architecture_key,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.project_guidance import GuidanceConstraint, ProjectGuidance
from app.repositories.architecture_protocol import ArchitectureRepository
from app.repositories.protocol import MemoryRepository
from app.services.python_architecture import PythonArchitectureIndexer


class ArchitectureService:
    def __init__(self, repository: ArchitectureRepository, memory: MemoryRepository) -> None:
        self.repository = repository
        self.memory = memory
        self.indexer = PythonArchitectureIndexer()

    async def get_project_guidance(self, project_key: str) -> ProjectGuidance:
        return await anyio.to_thread.run_sync(self._project_guidance, project_key)

    def _project_guidance(self, project_key: str) -> ProjectGuidance:
        project = self.repository.read_entity(project_key)
        if not isinstance(project, Project):
            raise ArchitectureGraphError("Project not found")
        as_of = datetime.now(UTC).date()
        scope = self.repository.guidance_scope(project_key)
        decisions = self.memory.read_guidance_decisions(scope.adr_keys, as_of)
        constraints = []
        for constraint in self.repository.guidance_constraints(scope.entity_keys):
            if not constraint.rule or constraint.enforcement_status not in {
                "unenforced", "partially_enforced", "enforced",
            }:
                continue
            constraints.append(GuidanceConstraint(
                entity_key=constraint.key, name=constraint.name, rule=constraint.rule,
                scope=constraint.scope, authority=constraint.authority,
                enforcement_status=constraint.enforcement_status,
                constrained_entity_keys=constraint.constrained_entity_keys,
                validating_test_keys=constraint.test_suite_keys,
            ))
        return ProjectGuidance(
            project_key=project_key, project_name=project.name, as_of=as_of,
            decisions=decisions, constraints=constraints,
            published_guidance=self.repository.read_guidance_state(project_key).published,
        )

    async def add_observation(
        self,
        draft: ObservationInput,
        recorded_by: str | None,
    ) -> Observation:
        observation = Observation(
            key=draft.key or uuid4().hex,
            name=draft.summary[:120],
            source=draft.source,
            summary=draft.summary,
            confidence=draft.confidence,
            timestamp=datetime.now(UTC),
            related_entity_keys=sorted(set(draft.related_entity_keys)),
            recorded_by=recorded_by,
        )
        return await anyio.to_thread.run_sync(self.repository.add_observation, observation)

    async def save_architectural_constraint(self, draft: ConstraintInput) -> Constraint:
        normalized = draft.model_copy(
            update={
                "constrained_entity_keys": sorted(set(draft.constrained_entity_keys)),
                "test_suite_keys": sorted(set(draft.test_suite_keys)),
            }
        )
        return await anyio.to_thread.run_sync(
            self.repository.save_architectural_constraint,
            normalized,
        )

    async def save_project_goal(self, draft: ProjectGoalInput) -> Goal:
        goal = Goal(
            **draft.model_dump(exclude={"adr_keys", "service_keys", "task_keys"}),
            adr_keys=sorted(set(draft.adr_keys)),
            service_keys=sorted(set(draft.service_keys)),
            task_keys=sorted(set(draft.task_keys)),
        )
        return await anyio.to_thread.run_sync(self.repository.save_project_goal, goal)

    async def add_conversation_summary(
        self,
        draft: ConversationSummaryInput,
        recorded_by: str | None,
    ) -> ConversationSummary:
        conversation = ConversationSummary(
            key=draft.key or uuid4().hex,
            name=draft.summary[:120],
            source=draft.source,
            summary=draft.summary,
            timestamp=datetime.now(UTC),
            recorded_by=recorded_by,
            decision_keys=sorted(set(draft.decision_keys)),
            risk_keys=sorted(set(draft.risk_keys)),
            task_keys=sorted(set(draft.task_keys)),
        )
        return await anyio.to_thread.run_sync(
            self.repository.add_conversation_summary, conversation
        )

    async def index_python(self, repository: str, files: dict[str, str]) -> ArchitectureIndexResult:
        return await anyio.to_thread.run_sync(self._index_python, repository, files)

    def _index_python(self, repository: str, files: dict[str, str]) -> ArchitectureIndexResult:
        name = repository.strip()
        candidates = self.repository.search(
            ArchitectureSearch(query=name, entity_type="Repository", limit=100, include_stale=True)
        )
        roots = [
            node for node in candidates
            if isinstance(node, Repository) and node.role == "source" and node.name == name
        ]
        if len(roots) > 1:
            raise ArchitectureGraphError(f"Multiple source repositories named '{name}'")
        root = roots[0] if roots else None
        index = self.indexer.build(repository, files, root=root)
        count = self.repository.replace_index(index)
        return ArchitectureIndexResult(
            repository=index.repository,
            repository_key=root.key if root else architecture_key("python-repository", name),
            files_indexed=len(files),
            entities_indexed=len(index.entities),
            relationships_indexed=count,
        )

    async def import_memory(self, key: str) -> ArchitectureEntity:
        return await anyio.to_thread.run_sync(self._import_memory, key)

    def _import_memory(self, key: str) -> ArchitectureEntity:
        document = self.memory.read_document(key)
        if document is None or document.kind not in {"adr", "domain-concept"}:
            raise ArchitectureGraphError(f"Memory '{key}' is not an ADR or domain concept")
        kind = ArchitectureADR if document.kind == "adr" else DomainConcept
        entity = kind(
            key=architecture_key("memory", document.kind, key),
            memory_key=key,
            name=document.title,
            description=document.content,
        )
        return self.repository.put_entity(entity)
