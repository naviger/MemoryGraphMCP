from __future__ import annotations

from datetime import date

import anyio

from app.domain.architecture import (
    ArchitectureADR,
    ArchitectureEntity,
    ArchitectureNeighborhood,
    ArchitectureSearch,
    ArchitectureTrace,
    DomainConcept,
    EntityType,
    Project,
)
from app.domain.architecture_events import TrackerOverview
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import MemoryDocument, MemorySearchRequest
from app.domain.project_guidance import GuidanceEditor
from app.domain.review_details import (
    ProjectEntity,
    ProjectOverview,
    ProjectSummary,
    RecordEmbeddingDiagnostics,
    ReviewEntityDetail,
    ReviewEntityLink,
    ReviewRecord,
    SearchEmbeddingDiagnostics,
    SimilarRecord,
    SimilarRecords,
)
from app.domain.test_drafts import FixWorkflow
from app.domain.test_runs import (
    RecurringTestFailures,
    TestFixRequest,
    TestRunDetail,
    TestRunHistory,
)
from app.repositories.architecture_protocol import ArchitectureRepository
from app.repositories.protocol import MemoryRepository

_LINK_TYPES: list[EntityType] = ["ADR", "Service", "SourceFile", "TestSuite"]


class ReviewDetailService:
    def __init__(
        self,
        memory: MemoryRepository,
        architecture: ArchitectureRepository | None,
        embedding_profile: SearchEmbeddingDiagnostics | None = None,
    ) -> None:
        self.memory = memory
        self.architecture = architecture
        self.embedding_profile = embedding_profile

    def _embedding_diagnostics(self, document: MemoryDocument) -> RecordEmbeddingDiagnostics:
        stored = SearchEmbeddingDiagnostics(
            provider=document.embedding_provider,
            model=document.embedding_model,
            version=document.embedding_version,
            dimension=len(document.embedding) if document.embedding else document.embedding_dimension,
        )
        if not document.embedding:
            state = "missing"
        elif self.embedding_profile is None:
            state = "unknown"
        else:
            # Semantic search checks the profile identifiers and the actual vector length.
            state = "matches" if stored == self.embedding_profile else "mismatch"
        return RecordEmbeddingDiagnostics(**stored.model_dump(), state=state)

    async def search_graph(self, request: ArchitectureSearch) -> list[ArchitectureEntity]:
        if self.architecture is None:
            return []
        return await anyio.to_thread.run_sync(self.architecture.search, request)

    async def get_project(
        self, key: str, query: str, as_of: date | None
    ) -> ProjectOverview | None:
        return await anyio.to_thread.run_sync(self._get_project, key, query, as_of)

    async def get_project_summary(self, key: str) -> ProjectSummary | None:
        return await anyio.to_thread.run_sync(self._get_project_summary, key)

    async def get_guidance_editor(
        self, key: str, can_edit: bool, can_publish: bool, before_version: int | None,
    ) -> GuidanceEditor | None:
        return await anyio.to_thread.run_sync(
            self._get_guidance_editor, key, can_edit, can_publish, before_version,
        )

    def _get_guidance_editor(
        self, key: str, can_edit: bool, can_publish: bool, before_version: int | None,
    ) -> GuidanceEditor | None:
        if self.architecture is None or not isinstance(self.architecture.read_entity(key), Project):
            return None
        state = self.architecture.read_guidance_state(key)
        cutoff = (state.published.version if state.published else 0) + 1
        history = self.architecture.list_guidance_versions(
            key, min(cutoff, before_version or cutoff), 11,
        ) if state.published else []
        return GuidanceEditor(
            revision=state.revision, draft=state.draft if can_edit else None,
            published=state.published, can_edit=can_edit, can_publish=can_publish,
            history=history[:10], next_before_version=history[9].version if len(history) > 10 else None,
        )

    async def change_guidance(
        self, key: str, revision: int, actor: str, *, text: str | None = None,
    ) -> None:
        if self.architecture is None:
            raise ArchitectureGraphError("Project guidance is unavailable")
        if text is None:
            await anyio.to_thread.run_sync(self.architecture.approve_guidance, key, revision, actor)
        else:
            await anyio.to_thread.run_sync(
                self.architecture.save_guidance_draft, key, revision, text, actor,
            )

    async def get_tracker_overview(
        self, key: str, configured_project: str | None,
    ) -> TrackerOverview | None:
        if self.architecture is None:
            return None
        if not isinstance(await anyio.to_thread.run_sync(self.architecture.read_entity, key), Project):
            return None
        events = await anyio.to_thread.run_sync(self.architecture.recent_tracker_activity, key, 10)
        return TrackerOverview(configured=key == configured_project, recent_events=events)

    async def get_project_testing(
        self, key: str, run_id: str | None = None,
    ) -> tuple[bool, TestRunDetail | None]:
        return await anyio.to_thread.run_sync(self._get_project_testing, key, run_id)

    async def read_fix_workflow(self, key: str, issue: TestFixRequest) -> FixWorkflow:
        if self.architecture is None:
            return FixWorkflow(project_key=key, issue=issue)
        saved = await anyio.to_thread.run_sync(
            self.architecture.read_fix_workflow, key, issue.run_id, issue.issue_type,
            issue.failure_index,
        )
        return saved or FixWorkflow(project_key=key, issue=issue)

    async def save_fix_workflow(self, workflow: FixWorkflow, **changes) -> FixWorkflow:
        if self.architecture is None:
            raise ArchitectureGraphError("Fix workflow storage is unavailable.")
        return await anyio.to_thread.run_sync(
            self.architecture.save_fix_workflow, workflow.model_copy(update=changes),
        )

    def _get_project_testing(
        self, key: str, run_id: str | None,
    ) -> tuple[bool, TestRunDetail | None]:
        if self.architecture is None or not isinstance(self.architecture.read_entity(key), Project):
            return False, None
        run = (
            self.architecture.read_test_run(key, run_id) if run_id
            else self.architecture.latest_test_run(key)
        )
        return True, run

    async def get_test_run_history(
        self, key: str, limit: int, cursor: str | None,
    ) -> TestRunHistory | None:
        return await anyio.to_thread.run_sync(self._get_test_run_history, key, limit, cursor)

    async def get_test_run_for_commit(self, key: str, repo: str, commit: str):
        if self.architecture is None:
            return None
        return await anyio.to_thread.run_sync(
            self.architecture.read_test_run_for_commit, key, repo, commit,
        )

    async def latest_passing_test_run(self, key: str) -> TestRunDetail | None:
        if self.architecture is None:
            return None
        return await anyio.to_thread.run_sync(self.architecture.latest_passing_test_run, key)

    async def get_recurring_test_failures(self, key: str) -> RecurringTestFailures | None:
        return await anyio.to_thread.run_sync(self._get_recurring_test_failures, key)

    def _get_recurring_test_failures(self, key: str) -> RecurringTestFailures | None:
        if self.architecture is None or not isinstance(self.architecture.read_entity(key), Project):
            return None
        return self.architecture.recurring_test_failures(key, 20)

    def _get_test_run_history(
        self, key: str, limit: int, cursor: str | None,
    ) -> TestRunHistory | None:
        if self.architecture is None or not isinstance(self.architecture.read_entity(key), Project):
            return None
        return self.architecture.list_test_runs(key, limit, cursor)

    def _get_project_summary(self, key: str) -> ProjectSummary | None:
        if self.architecture is None or not isinstance(self.architecture.read_entity(key), Project):
            return None
        scope = self.architecture.project_scope(key)
        linked_keys = sorted({node.memory_key for node in scope.entities if node.memory_key})
        documents = self.memory.project_documents(key, scope.repository_names, linked_keys, "", None)
        adr_documents = {doc.key: doc.status for doc in documents if doc.kind == "adr"}
        proposed_adrs = sum(status == "proposed" for status in adr_documents.values())
        proposed_adrs += sum(
            node.entity_type == "ADR"
            and node.status == "proposed"
            and node.memory_key not in adr_documents
            for node in scope.entities
        )
        document_dates = [
            value for doc in documents
            for value in (doc.recorded_date, doc.last_recorded_update) if value is not None
        ]
        return ProjectSummary(
            open_tasks=sum(
                node.entity_type == "Task" and node.status in {"open", "in_progress"}
                for node in scope.entities
            ),
            proposed_adrs=proposed_adrs,
            earliest_record=min(
                (value for value in [scope.earliest_record, *document_dates] if value),
                default=None,
            ),
            latest_record=max(
                (value for value in [scope.latest_record, *document_dates] if value),
                default=None,
            ),
            last_index_date=scope.last_index_date,
        )

    def _get_project(
        self, key: str, query: str, as_of: date | None
    ) -> ProjectOverview | None:
        if self.architecture is None:
            return None
        project = self.architecture.read_entity(key)
        if not isinstance(project, Project):
            return None
        scope = self.architecture.project_scope(key)
        linked_keys = sorted({node.memory_key for node in scope.entities if node.memory_key})
        query = query.strip().lower()
        documents = self.memory.project_documents(
            key, scope.repository_names, linked_keys, query, as_of
        )
        document_keys = {document.key for document in documents}

        def matches(node: ProjectEntity) -> bool:
            return any(
                query in value.lower()
                for value in (node.name, node.source_path or "", node.qualified_name or "")
            )

        selected = {
            node.key for node in scope.entities
            if node.memory_key in document_keys
            or (matches(node) and (as_of is None or node.entity_type != "ADR"))
        }
        if query:
            by_key = {node.key: node for node in scope.entities}
            files = {
                (node.repository, node.source_path): node.key for node in scope.entities
                if node.entity_type == "SourceFile" and node.source_path
            }
            repositories = {
                node.name: node.key for node in scope.entities
                if node.entity_type == "Repository" and node.role == "source"
            }
            for key in tuple(selected):
                node = by_key[key]
                file_key = files.get((node.repository, node.source_path))
                repository_key = repositories.get(node.repository)
                if file_key:
                    selected.add(file_key)
                if repository_key:
                    selected.add(repository_key)
                while node.parent_key in by_key and node.parent_key not in selected:
                    selected.add(node.parent_key)
                    node = by_key[node.parent_key]

        return ProjectOverview(
            key=project.key, name=project.name, description=project.description,
            entities=[node for node in scope.entities if node.key in selected],
            dependencies=[node for node in scope.dependencies if matches(node)],
            documents=documents,
        )

    async def get_neighborhood(
        self, key: str, entity_types: list[EntityType], hops: int
    ) -> ArchitectureNeighborhood | None:
        return await anyio.to_thread.run_sync(self._get_neighborhood, key, entity_types, hops)

    def _get_neighborhood(
        self, key: str, entity_types: list[EntityType], hops: int
    ) -> ArchitectureNeighborhood | None:
        if self.architecture is None:
            return None
        center = self.architecture.read_entity(key)
        if center is None:
            return None
        # Sample each layer so a high-degree node cannot consume the entire path budget.
        layer_limit = 12 if hops == 1 else 8
        paths = []
        truncated = False
        for depth in range(1, hops + 1):
            layer = self.architecture.trace(
                ArchitectureTrace(
                    key=key,
                    min_depth=depth,
                    prioritize=depth == 1,
                    shortest_paths=depth > 1,
                    depth=depth,
                    limit=layer_limit + 1,
                    entity_types=entity_types,
                )
            )
            paths.extend(layer[:layer_limit])
            truncated |= len(layer) > layer_limit
        return ArchitectureNeighborhood(center=center, paths=paths, truncated=truncated)

    async def similar_records(self, key: str, limit: int) -> SimilarRecords | None:
        return await anyio.to_thread.run_sync(self._similar_records, key, limit)

    def _similar_records(self, key: str, limit: int) -> SimilarRecords | None:
        seed = self.memory.read_document(key)
        if seed is None:
            return None
        if (
            not seed.embedding
            or not any(seed.embedding)
            or not seed.embedding_provider
            or not seed.embedding_model
            or seed.embedding_dimension not in (None, len(seed.embedding))
        ):
            return SimilarRecords(available=False)
        hits = self.memory.semantic_search(MemorySearchRequest(
            query="", mode="semantic", limit=limit, exclude_key=key,
            embedding=seed.embedding, embedding_provider=seed.embedding_provider,
            embedding_model=seed.embedding_model, embedding_version=seed.embedding_version,
        ))
        return SimilarRecords(matches=[
            SimilarRecord(
                key=hit.document.key, title=hit.document.title,
                kind=hit.document.kind, similarity=hit.score,
            )
            for hit in hits
            if hit.document.key and hit.document.key != key and hit.score > 0
        ])

    async def get_record(self, key: str) -> ReviewEntityDetail | None:
        return await anyio.to_thread.run_sync(self._get_record, key)

    async def get_entity(self, key: str) -> ReviewEntityDetail | None:
        return await anyio.to_thread.run_sync(self._get_entity, key)

    def _get_record(self, key: str) -> ReviewEntityDetail | None:
        document = self.memory.read_document(key)
        if document is None:
            return None
        nodes = self.architecture.find_memory_entities(key) if self.architecture else []
        return self._detail(document, nodes)

    def _get_entity(self, key: str) -> ReviewEntityDetail | None:
        if self.architecture is None:
            return None
        entity = self.architecture.read_entity(key)
        if entity is None:
            return None
        document = (
            self.memory.read_document(entity.memory_key)
            if isinstance(entity, (ArchitectureADR, DomainConcept)) and entity.memory_key
            else None
        )
        return self._detail(document, [entity])

    def _detail(
        self,
        document: MemoryDocument | None,
        nodes: list[ArchitectureEntity],
    ) -> ReviewEntityDetail:
        projected = ReviewRecord.model_validate(document.model_dump()) if document else None
        links: dict[tuple[str, str], ReviewEntityLink] = {}
        if document and document.kind == "adr" and document.key:
            for link in self.memory.list_adr_supersession_links(document.key):
                links[(link.destination, link.key)] = link

        if self.architecture:
            seed_keys = {node.key for node in nodes}
            for node in nodes:
                for path in self.architecture.trace(
                    ArchitectureTrace(
                        key=node.key,
                        depth=4,
                        limit=100,
                        entity_types=_LINK_TYPES,
                    )
                ):
                    entity = path.entity
                    if entity.key in seed_keys:
                        continue
                    memory_key = entity.memory_key if isinstance(entity, ArchitectureADR) else None
                    if memory_key and document and memory_key == document.key:
                        continue
                    link = ReviewEntityLink(
                        key=entity.key,
                        destination="entities",
                        entity_type=entity.entity_type,
                        name=entity.name,
                        source_path=entity.source_path,
                        index_state=entity.index_state,
                        relationships=[step.relationship_type for step in path.relationships],
                    )
                    identity = (
                        ("records", memory_key) if memory_key else (link.destination, link.key)
                    )
                    previous = links.get(identity)
                    if previous is None or len(link.relationships) < len(previous.relationships):
                        links[identity] = link

        return ReviewEntityDetail(
            record=projected,
            entity=nodes[0] if nodes else None,
            embedding=self._embedding_diagnostics(document) if document else None,
            links=sorted(
                links.values(), key=lambda link: (link.entity_type, link.name.casefold(), link.key)
            ),
        )
