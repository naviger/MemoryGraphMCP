from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

from arango.database import StandardDatabase, TransactionDatabase
from arango.exceptions import ArangoServerError, CollectionCreateError
from pydantic import TypeAdapter

from app.domain.architecture import (
    FORWARD_RELATIONSHIPS,
    INVERSE_RELATIONSHIPS,
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
    Project,
    RelationshipType,
    Repository,
    Task,
    architecture_key,
)
from app.domain.architecture_events import (
    GraphEvent,
    GraphEventReceipt,
    TaskUpserted,
    TrackerActivity,
)
from app.domain.capture_checkpoint import CaptureCheckpoint, CaptureStream
from app.domain.context_usage import UsageOutcome, UsageReceipt, UsageScope
from app.domain.effectiveness import DevelopmentOutcome, OutcomeReceipt
from app.domain.exceptions import ArchitectureGraphError
from app.domain.knowledge_capture import CaptureReviewItem
from app.domain.project_changes import BranchCleanup
from app.domain.project_guidance import (
    GuidanceDraft,
    GuidanceScope,
    GuidanceState,
    PublishedGuidance,
)
from app.domain.review_details import ProjectScope
from app.domain.test_drafts import FixWorkflow
from app.domain.test_runs import (
    FailingAttempt,
    RecurringTestFailure,
    RecurringTestFailures,
    TestFailureDetail,
    TestRun,
    TestRunDetail,
    TestRunHistory,
    TestValidationLink,
)

_ENTITY = TypeAdapter(ArchitectureEntity)
_STATIC_SOURCE = "static-analysis"
_OBSERVATION_SOURCE_PREFIX = "observation:"
_GOAL_SOURCE_PREFIX = "project-goal:"
_CONSTRAINT_SOURCE_PREFIX = "architectural-constraint:"
_CONVERSATION_SOURCE_PREFIX = "conversation-summary:"
_BATCH_SIZE = 500


class ArangoArchitectureRepository:
    def __init__(
        self,
        database: Callable[[], StandardDatabase],
        entities_collection: str = "architecture_entities",
        edges_collection: str = "architecture_edges",
        events_collection: str = "architecture_events",
    ) -> None:
        self._database = database
        self._entities = entities_collection
        self._edges = edges_collection
        self._events = events_collection

    def ensure_storage_ready(self) -> None:
        database = self._database()
        for name, edge in ((self._entities, False), (self._edges, True), (self._events, False)):
            if not database.has_collection(name):
                try:
                    database.create_collection(name, edge=edge)
                except CollectionCreateError as exc:
                    if exc.error_code != 1207:  # Another instance created the collection.
                        raise

    @contextmanager
    def _transaction(
        self, *, with_events: bool = False, read_edges: bool = False, write_edges: bool = False,
    ) -> Iterator[TransactionDatabase]:
        collections = [self._entities, self._events if with_events else self._edges]
        if with_events and write_edges:
            collections.append(self._edges)
        options = {"read": [self._edges]} if read_edges else {}
        transaction = self._database().begin_transaction(exclusive=collections, **options)
        try:
            yield transaction
            transaction.commit_transaction()
        except BaseException:
            with suppress(ArangoServerError):
                transaction.abort_transaction()
            raise

    @staticmethod
    def _entity_payload(entity: ArchitectureEntity) -> dict:
        return {**entity.model_dump(mode="json"), "_key": entity.key}

    def put_entity(self, entity: ArchitectureEntity) -> ArchitectureEntity:
        if isinstance(entity, Observation):
            raise ArchitectureGraphError(
                "Use add_observation to record an observation and its links"
            )
        if isinstance(entity, ConversationSummary):
            raise ArchitectureGraphError("Use add_conversation_summary to record a concise summary")
        if isinstance(entity, Goal) and (
            entity.project_key or entity.adr_keys or entity.service_keys or entity.task_keys
        ):
            raise ArchitectureGraphError("Use save_project_goal to manage project goal links")
        if isinstance(entity, Constraint) and (
            entity.rule
            or entity.scope
            or entity.authority
            or entity.enforcement_status
            or entity.constrained_entity_keys
            or entity.test_suite_keys
        ):
            raise ArchitectureGraphError("Use save_architectural_constraint to manage rules")
        if entity.index_state is not None:
            raise ArchitectureGraphError("Index state is assigned by Python ingestion")
        with self._transaction() as transaction:
            nodes = transaction.collection(self._entities)
            existing = nodes.get(entity.key)
            if existing and existing.get("index_state") is not None:
                raise ArchitectureGraphError(
                    "Update indexed entities by reindexing their repository"
                )
            if existing and existing["entity_type"] == "Goal" and existing.get("project_key"):
                raise ArchitectureGraphError("Use save_project_goal to update a project goal")
            if existing and existing["entity_type"] == "Constraint" and existing.get("rule"):
                raise ArchitectureGraphError("Use save_architectural_constraint to update a rule")
            if existing and existing["entity_type"] != entity.entity_type:
                raise ArchitectureGraphError(f"Entity '{entity.key}' already has a different type")
            if isinstance(entity, Project):
                now = datetime.now(UTC)
                if existing:
                    previous = Project.model_validate(existing)
                    timestamps = {"created_at", "updated_at"}
                    if previous.model_dump(exclude=timestamps) == entity.model_dump(
                        exclude=timestamps
                    ):
                        return previous
                    entity = entity.model_copy(update={
                        "created_at": previous.created_at, "updated_at": now,
                    })
                else:
                    entity = entity.model_copy(update={"created_at": now, "updated_at": now})
            result = nodes.insert(
                self._entity_payload(entity), overwrite_mode="replace", return_new=True
            )
            return _ENTITY.validate_python(result["new"])

    def _edge_payload(self, link: ArchitectureLink, now: str) -> dict:
        source, target = link.source_key, link.target_key
        kind = link.relationship_type
        if source == target:
            raise ArchitectureGraphError("A relationship requires two different entities")
        if kind not in FORWARD_RELATIONSHIPS:
            source, target = target, source
            kind = INVERSE_RELATIONSHIPS[kind]
        elif kind == "related_to":
            source, target = sorted((source, target))
        return {
            "_key": architecture_key(source, kind, target, link.source),
            "_from": f"{self._entities}/{source}",
            "_to": f"{self._entities}/{target}",
            "source_key": source,
            "target_key": target,
            "relationship_type": kind,
            "confidence": link.confidence,
            "source": link.source,
            "created_at": now,
            "updated_at": now,
        }

    def add_observation(self, observation: Observation, *, replay: bool = False) -> Observation:
        with self._transaction() as transaction:
            nodes = transaction.collection(self._entities)
            existing = nodes.get(observation.key)
            if existing is not None:
                if existing["entity_type"] == "Observation":
                    previous = Observation.model_validate(existing)
                    if (replay and previous.metadata == observation.metadata) or previous.model_dump(
                        exclude={"timestamp"}
                    ) == observation.model_dump(
                        exclude={"timestamp"}
                    ):
                        return previous
                raise ArchitectureGraphError(f"Entity key '{observation.key}' is already in use")

            for key in observation.related_entity_keys:
                if key == observation.key:
                    raise ArchitectureGraphError("An observation cannot link to itself")
                if not nodes.has(key):
                    raise ArchitectureGraphError(f"Architecture entity '{key}' was not found")

            result = nodes.insert(self._entity_payload(observation), return_new=True)
            edges = transaction.collection(self._edges)
            for key in observation.related_entity_keys:
                link = ArchitectureLink(
                    source_key=observation.key,
                    target_key=key,
                    relationship_type="related_to",
                    source=f"{_OBSERVATION_SOURCE_PREFIX}{observation.key}",
                    confidence=observation.confidence,
                )
                edges.insert(self._edge_payload(link, observation.timestamp.isoformat()))
            return Observation.model_validate(result["new"])

    def last_index_revision(self, repository_key: str) -> str | None:
        row = self._database().collection(self._entities).get(repository_key)
        return row.get("last_indexed_at") if row else None

    def capture_goal_support(self, changed_keys: list[str], goal_keys: list[str]) -> dict[str, list[str]]:
        if not changed_keys or not goal_keys:
            return {}
        rows = self._database().aql.execute(
            """
            FOR edge IN @@edges
                FILTER edge.relationship_type == "supports_goal"
                FILTER edge.source_key IN @changed AND edge.target_key IN @goals
                COLLECT goal = edge.target_key INTO sources = edge.source_key
                RETURN {goal, sources: UNIQUE(sources)}
            """,
            bind_vars={"@edges": self._edges, "changed": changed_keys, "goals": goal_keys},
        )
        return {row["goal"]: row["sources"] for row in rows}

    def capture_conversations(self, entity_keys: list[str]) -> list[ConversationSummary]:
        rows = self._database().aql.execute(
            """
            FOR node IN @@entities
                FILTER node.entity_type == "ConversationSummary"
                LET outcomes = UNION_DISTINCT(node.decision_keys, node.risk_keys, node.task_keys)
                FILTER LENGTH(outcomes) > 0
                FILTER LENGTH(MINUS(outcomes, @keys)) == 0
                RETURN node
            """,
            bind_vars={"@entities": self._entities, "keys": entity_keys},
        )
        return [ConversationSummary.model_validate(row) for row in rows]

    def read_capture_checkpoint(
        self, project: str, scope: str, stream: CaptureStream,
    ) -> CaptureCheckpoint:
        key = architecture_key("capture-checkpoint", project, scope, stream)
        row = self._database().collection(self._events).get(key)
        return CaptureCheckpoint.model_validate(row["payload"]) if row else CaptureCheckpoint(stream=stream)

    def save_capture_checkpoint(
        self, project: str, scope: str, checkpoint: CaptureCheckpoint,
    ) -> CaptureCheckpoint:
        key = architecture_key("capture-checkpoint", project, scope, checkpoint.stream)
        with self._transaction(with_events=True) as transaction:
            events = transaction.collection(self._events)
            row = events.get(key)
            revision = row["payload"]["revision"] if row else 0
            if revision != checkpoint.revision:
                raise ArchitectureGraphError("Capture checkpoint changed concurrently")
            updated = checkpoint.model_copy(update={"revision": revision + 1})
            events.insert({
                "_key": key, "event_type": "capture.checkpoint", "project_key": project,
                "payload": updated.model_dump(mode="json"),
            }, overwrite_mode="replace")
        return updated

    def capture_journal_lag(self, project_key: str) -> tuple[int, datetime | None]:
        rows = self._database().aql.execute(
            """
            FOR event IN @@events
                FILTER event.project_key == @project
                FILTER event.event_type == "test.run.finished" OR event.outcome == "applied"
                FILTER DOCUMENT(@entities, CONCAT("capture-", event._key)) == null
                COLLECT AGGREGATE count = LENGTH(1), oldest = MIN(event.recorded_at)
                RETURN {count, oldest}
            """,
            bind_vars={"@events": self._events, "entities": self._entities, "project": project_key},
        )
        row = next(iter(rows), {"count": 0, "oldest": None})
        return row["count"], datetime.fromisoformat(row["oldest"]) if row["oldest"] else None

    def capture_journal(self, project_key: str, limit: int) -> list[dict]:
        return list(self._database().aql.execute(
            """
            FOR event IN @@events
                FILTER event.project_key == @project
                FILTER event.event_type == "test.run.finished" OR event.outcome == "applied"
                FILTER DOCUMENT(@entities, CONCAT("capture-", event._key)) == null
                SORT event.recorded_at, event._key
                LIMIT @limit
                RETURN event
            """,
            bind_vars={
                "@events": self._events, "entities": self._entities,
                "project": project_key, "limit": limit,
            },
        ))

    def capture_review(
        self, project_key: str | None, limit: int, as_of: str | None, *, allowed_projects: list[str] | None = None, allowed_entity_keys: list[str] | None = None,
    ) -> list[CaptureReviewItem]:
        rows = self._database().aql.execute(
            """
            FOR node IN @@entities
                FILTER node.entity_type == "Observation" AND LENGTH(node.suggestions) > 0
                LET project = node.metadata.capture_project
                FILTER IS_STRING(project) AND (@project == null OR project == @project)
                FILTER @as_of == null OR LEFT(node.timestamp, 10) <= @as_of
                FILTER @projects == null OR project IN @projects
                FILTER @entities_allowed == null OR node._key IN @entities_allowed
                LET suggestions = (
                    FOR link IN node.suggestions
                        FILTER @entities_allowed == null OR
                            (link.source_key IN @entities_allowed AND link.target_key IN @entities_allowed)
                        RETURN link
                )
                FILTER LENGTH(suggestions) > 0
                SORT node.timestamp DESC, node._key DESC
                LIMIT @limit
                RETURN {key: node._key, project_key: project, summary: node.summary,
                        timestamp: node.timestamp, suggestions}
            """,
            bind_vars={
                "@entities": self._entities, "project": project_key,
                "limit": limit, "as_of": as_of, "projects": allowed_projects,
                "entities_allowed": allowed_entity_keys,
            },
        )
        return [CaptureReviewItem.model_validate(row) for row in rows]

    def save_project_goal(self, goal: Goal) -> Goal:
        if goal.project_key is None:
            raise ArchitectureGraphError("A project goal requires a project")
        source = f"{_GOAL_SOURCE_PREFIX}{goal.key}"
        supporting: tuple[tuple[EntityType, list[str]], ...] = (
            ("ADR", goal.adr_keys),
            ("Service", goal.service_keys),
            ("Task", goal.task_keys),
        )
        with self._transaction() as transaction:
            nodes = transaction.collection(self._entities)
            existing = nodes.get(goal.key)
            if existing and (existing["entity_type"] != "Goal" or existing.get("index_state")):
                raise ArchitectureGraphError(f"Entity key '{goal.key}' is already in use")
            self._require_entity_type(nodes, goal.project_key, "Project")
            links = [
                ArchitectureLink(
                    source_key=goal.project_key,
                    target_key=goal.key,
                    relationship_type="defines",
                    source=source,
                )
            ]
            for kind, keys in supporting:
                for key in keys:
                    self._require_entity_type(nodes, key, kind)
                    links.append(
                        ArchitectureLink(
                            source_key=key,
                            target_key=goal.key,
                            relationship_type="supports_goal",
                            source=source,
                        )
                    )
            result = nodes.insert(
                self._entity_payload(goal), return_new=True, overwrite_mode="replace"
            )
            self._replace_managed_links(transaction, goal.key, source, links)
            return Goal.model_validate(result["new"])

    def save_architectural_constraint(self, draft: ConstraintInput) -> Constraint:
        source = f"{_CONSTRAINT_SOURCE_PREFIX}{draft.key}"
        with self._transaction() as transaction:
            nodes = transaction.collection(self._entities)
            existing = nodes.get(draft.key)
            if existing and (
                existing["entity_type"] != "Constraint" or existing.get("index_state")
            ):
                raise ArchitectureGraphError(f"Entity key '{draft.key}' is already in use")

            links = []
            for key in draft.constrained_entity_keys:
                if key == draft.key:
                    raise ArchitectureGraphError("A constraint cannot constrain itself")
                if not nodes.has(key):
                    raise ArchitectureGraphError(f"Architecture entity '{key}' was not found")
                links.append(
                    ArchitectureLink(
                        source_key=draft.key,
                        target_key=key,
                        relationship_type="constrains",
                        source=source,
                    )
                )
            for key in draft.test_suite_keys:
                self._require_entity_type(nodes, key, "TestSuite")
                links.append(
                    ArchitectureLink(
                        source_key=draft.key,
                        target_key=key,
                        relationship_type="validated_by",
                        source=source,
                    )
                )

            if existing is None:
                constraint = Constraint.model_validate(draft.model_dump())
                result = nodes.insert(self._entity_payload(constraint), return_new=True)
            else:
                result = nodes.update(
                    {"_key": draft.key, **draft.model_dump(mode="json")},
                    return_new=True,
                )
            self._replace_managed_links(transaction, draft.key, source, links)
            return Constraint.model_validate(result["new"])

    def add_conversation_summary(self, conversation: ConversationSummary) -> ConversationSummary:
        outcomes: tuple[tuple[EntityType, RelationshipType, list[str]], ...] = (
            ("ADR", "produced", conversation.decision_keys),
            ("Risk", "identified", conversation.risk_keys),
            ("Task", "created", conversation.task_keys),
        )
        source = f"{_CONVERSATION_SOURCE_PREFIX}{conversation.key}"
        with self._transaction() as transaction:
            nodes = transaction.collection(self._entities)
            existing = nodes.get(conversation.key)
            if existing:
                if existing["entity_type"] == "ConversationSummary":
                    previous = ConversationSummary.model_validate(existing)
                    if previous.model_dump(exclude={"timestamp"}) == conversation.model_dump(
                        exclude={"timestamp"}
                    ):
                        return previous
                raise ArchitectureGraphError(f"Entity key '{conversation.key}' is already in use")

            links = []
            for kind, relation, keys in outcomes:
                for key in keys:
                    self._require_entity_type(nodes, key, kind)
                    links.append(
                        ArchitectureLink(
                            source_key=conversation.key,
                            target_key=key,
                            relationship_type=relation,
                            source=source,
                        )
                    )
            result = nodes.insert(self._entity_payload(conversation), return_new=True)
            edges = transaction.collection(self._edges)
            for link in links:
                edges.insert(self._edge_payload(link, conversation.timestamp.isoformat()))
            return ConversationSummary.model_validate(result["new"])

    @staticmethod
    def _require_entity_type(nodes, key: str, expected: EntityType) -> dict:
        node = nodes.get(key)
        if node is None or node["entity_type"] != expected:
            raise ArchitectureGraphError(
                f"Architecture entity '{key}' must exist as type {expected}"
            )
        return node

    def _replace_managed_links(
        self,
        transaction: TransactionDatabase,
        owner_key: str,
        source: str,
        links: list[ArchitectureLink],
    ) -> None:
        edges = transaction.collection(self._edges)
        now = datetime.now(UTC).isoformat()
        previous = {
            edge["_key"]
            for endpoint in ("_from", "_to")
            for edge in edges.find({"source": source, endpoint: f"{self._entities}/{owner_key}"})
        }
        for link in links:
            payload = self._edge_payload(link, now)
            key = payload["_key"]
            if key in previous:
                previous.remove(key)
            else:
                edges.insert(payload)
        for key in previous:
            edges.delete(key)

    def recent_tracker_activity(self, project_key: str, limit: int) -> list[TrackerActivity]:
        rows = self._database().aql.execute(
            """
            WITH @@entities
            FOR row IN @@events
                FILTER row.project_key == @project
                FILTER row.event_type IN ["task.upserted", "task.completed"]
                SORT DATE_TIMESTAMP(row.recorded_at) DESC, row._key DESC
                LIMIT @limit
                LET task_name = FIRST(
                    FOR task, edge IN 1..1 ANY @seed @@edges
                        FILTER task._key == row.task_key AND task.entity_type == "Task"
                        FILTER edge.relationship_type IN ["defines", "related_to", "references"]
                        LIMIT 1
                        RETURN task.name
                )
                RETURN MERGE(KEEP(row, [
                    "event_id", "event_type", "task_key", "occurred_at", "recorded_at", "outcome"
                ]), {task_name})
            """,
            bind_vars={
                "@events": self._events, "@entities": self._entities, "@edges": self._edges,
                "project": project_key, "seed": f"{self._entities}/{project_key}", "limit": limit,
            },
        )
        return [TrackerActivity.model_validate(row) for row in rows]

    def get_event(self, source: str, project_key: str, event_id: str) -> GraphEventReceipt:
        key = architecture_key("tracker-event", source, project_key, event_id)
        row = self._database().collection(self._events).get(key)
        if row is None:
            raise ArchitectureGraphError(f"Event '{event_id}' was not found")
        return GraphEventReceipt.model_validate(row)

    def _direct_projects(self, nodes, edges, task_key: str) -> set[str]:
        task_id = f"{self._entities}/{task_key}"
        owners = set()
        for endpoint, other in (("_from", "target_key"), ("_to", "source_key")):
            for edge in edges.find({endpoint: task_id}):
                if edge["relationship_type"] not in {"defines", "related_to", "references"}:
                    continue
                node = nodes.get(edge[other])
                if node and node["entity_type"] == "Project":
                    owners.add(node["_key"])
        return owners

    def ingest_event(
        self, source: str, project_key: str, event: GraphEvent,
    ) -> GraphEventReceipt:
        if event.project_key != project_key:
            raise ArchitectureGraphError(
                "Event project does not match the configured tracker project"
            )
        key = architecture_key("tracker-event", source, project_key, event.event_id)
        payload = event.model_dump(mode="json")
        with self._transaction(with_events=True, write_edges=True) as transaction:
            journal = transaction.collection(self._events)
            previous_event = journal.get(key)
            if previous_event is not None:
                if previous_event["payload"] != payload:
                    raise ArchitectureGraphError("This event ID already has a different payload")
                return GraphEventReceipt.model_validate(previous_event)

            nodes = transaction.collection(self._entities)
            edges = transaction.collection(self._edges)
            self._require_entity_type(nodes, project_key, "Project")
            task = nodes.get(event.task_key)
            if task is not None and (
                task["entity_type"] != "Task"
                or self._direct_projects(nodes, edges, event.task_key) != {project_key}
            ):
                raise ArchitectureGraphError("Task is not exclusively linked to the tracker project")

            latest = next(iter(transaction.aql.execute(
                """
                FOR row IN @@events
                    FILTER row.event_type IN ["task.upserted", "task.completed"]
                    FILTER row.source == @source AND row.project_key == @project
                    FILTER row.task_key == @task AND row.outcome == "applied"
                    SORT row.occurred_at_us DESC
                    LIMIT 1
                    RETURN row.occurred_at_us
                """,
                bind_vars={
                    "@events": self._events, "source": source,
                    "project": project_key, "task": event.task_key,
                },
            )), None)
            stale = latest is not None and event.occurred_at_us <= latest
            now = datetime.now(UTC)
            affected = []
            if not stale:
                affected.append(event.task_key)
                if isinstance(event, TaskUpserted):
                    updates = event.model_dump(
                        include={"name", "description", "status"}, exclude_none=True,
                    )
                    if task is None:
                        nodes.insert(self._entity_payload(Task(key=event.task_key, **updates)))
                        link = ArchitectureLink(
                            source_key=project_key, target_key=event.task_key,
                            relationship_type="defines", source="project-tracker",
                        )
                        edges.insert(self._edge_payload(link, now.isoformat()))
                        affected.append(project_key)
                    else:
                        nodes.update({"_key": event.task_key, **updates})
                else:
                    if task is None:
                        raise ArchitectureGraphError(f"Task '{event.task_key}' was not found")
                    nodes.update({"_key": event.task_key, "status": "done"})

            receipt = GraphEventReceipt(
                event_id=event.event_id, event_type=event.event_type,
                source=source, project_key=project_key, task_key=event.task_key,
                occurred_at=event.occurred_at, recorded_at=now,
                affected_entity_keys=affected, outcome="ignored_stale" if stale else "applied",
            )
            journal.insert({
                **receipt.model_dump(mode="json"), "_key": key,
                "payload": payload, "occurred_at_us": event.occurred_at_us,
            })
            return receipt

    def known_test_run_ids(self, repository_key: str, run_ids: list[str]) -> set[str]:
        if not run_ids:
            return set()
        ids_by_key = {
            architecture_key("test-run", "github-actions", repository_key, run_id): run_id
            for run_id in run_ids
        }
        existing = self._database().aql.execute(
            """
            FOR key IN @keys
                LET event = DOCUMENT(@collection, key)
                FILTER event.event_type == "test.run.finished"
                RETURN key
            """,
            bind_vars={"collection": self._events, "keys": list(ids_by_key)},
        )
        return {ids_by_key[key] for key in existing}

    def put_test_run(self, run: TestRun) -> TestRun:
        key = architecture_key("test-run", run.source, run.repository_key, run.run_id)
        payload = run.model_dump(mode="json")
        with self._transaction(with_events=True, read_edges=True) as transaction:
            events = transaction.collection(self._events)
            existing = events.get(key)
            if existing is not None:
                previous = TestRun.model_validate(existing["payload"])
                if previous != run:
                    raise ArchitectureGraphError("This test run ID already has a different payload")
                return previous
            nodes = transaction.collection(self._entities)
            self._require_entity_type(nodes, run.project_key, "Project")
            repository = self._require_entity_type(nodes, run.repository_key, "Repository")
            linked = next(iter(transaction.aql.execute(
                """
                WITH @@entities
                FOR repo, edge IN 1..1 ANY @project @@edges
                    FILTER repo._key == @repository AND edge.relationship_type != "uses"
                    LIMIT 1
                    RETURN true
                """,
                bind_vars={
                    "@entities": self._entities, "@edges": self._edges,
                    "project": f"{self._entities}/{run.project_key}",
                    "repository": run.repository_key,
                },
            )), False)
            if repository.get("role") != "source" or not linked:
                raise ArchitectureGraphError("The source repository must be linked to the project")
            events.insert({
                "_key": key,
                "event_type": "test.run.finished",
                "source": run.source,
                "project_key": run.project_key,
                "occurred_at_us": int(run.finished_at.timestamp() * 1_000_000),
                "recorded_at": datetime.now(UTC).isoformat(),
                "affected_entity_keys": [run.project_key, run.repository_key],
                "payload": payload,
            })
        return run

    def read_fix_workflow(
        self, project_key: str, run_id: str, issue_type: str, issue_index: int,
    ) -> FixWorkflow | None:
        key = architecture_key("ci-fix-workflow", project_key, run_id, issue_type, str(issue_index))
        row = self._database().collection(self._events).get(key)
        return FixWorkflow.model_validate(row["payload"]) if row else None

    def save_fix_workflow(self, workflow: FixWorkflow) -> FixWorkflow:
        issue = workflow.issue
        key = architecture_key(
            "ci-fix-workflow", workflow.project_key, issue.run_id, issue.issue_type,
            str(issue.failure_index),
        )
        with self._transaction(with_events=True) as transaction:
            events = transaction.collection(self._events)
            existing = events.get(key)
            revision = existing["payload"]["revision"] if existing else 0
            if revision != workflow.revision:
                raise ArchitectureGraphError("The saved fix workflow changed. Reopen the issue.")
            updated = workflow.model_copy(update={"revision": revision + 1})
            events.insert({
                "_key": key, "event_type": "test.fix.workflow", "project_key": workflow.project_key,
                "affected_entity_keys": [workflow.project_key],
                "payload": updated.model_dump(mode="json"),
            }, overwrite_mode="replace")
        return updated

    def latest_test_run(self, project_key: str) -> TestRunDetail | None:
        return self._read_test_run(project_key, None)

    def latest_passing_test_run(self, project_key: str) -> TestRunDetail | None:
        return self._read_test_run(project_key, None, passed_only=True)

    def read_test_run(self, project_key: str, run_id: str) -> TestRunDetail | None:
        return self._read_test_run(project_key, run_id)

    def list_test_runs(
        self, project_key: str, limit: int, cursor: str | None,
    ) -> TestRunHistory:
        rows = list(self._database().aql.execute(
            """
            LET boundary = @cursor == null ? null : DOCUMENT(@events, @cursor)
            FOR event IN @@events
                FILTER event.event_type == "test.run.finished" AND event.project_key == @key
                FILTER @cursor == null OR (
                    boundary != null AND boundary.project_key == @key
                    AND boundary.event_type == "test.run.finished"
                    AND (event.occurred_at_us < boundary.occurred_at_us
                        OR (event.occurred_at_us == boundary.occurred_at_us
                            AND event._key < boundary._key))
                )
                SORT event.occurred_at_us DESC, event._key DESC
                LIMIT @limit
                RETURN {cursor: event._key, run: KEEP(event.payload, [
                    "run_id", "commit", "finished_at", "status", "passed", "failed", "skipped",
                    "warning_count", "build_status"
                ])}
            """,
            bind_vars={
                "@events": self._events, "events": self._events, "key": project_key,
                "cursor": cursor, "limit": limit + 1,
            },
        ))
        return TestRunHistory(
            runs=[row["run"] for row in rows[:limit]],
            next_cursor=rows[limit - 1]["cursor"] if len(rows) > limit else None,
        )

    def recurring_test_failures(self, project_key: str, limit: int) -> RecurringTestFailures:
        rows = list(self._database().aql.execute(
            """
            FOR event IN @@events
                FILTER event.event_type == "test.run.finished" AND event.project_key == @key
                SORT event.occurred_at_us DESC, event._key DESC
                LIMIT @limit
                RETURN KEEP(event.payload, ["run_id", "finished_at", "failures"])
            """,
            bind_vars={"@events": self._events, "key": project_key, "limit": limit},
        ))
        groups: dict[tuple[str, str | None], dict[str, FailingAttempt]] = {}
        for row in rows:
            attempt = FailingAttempt.model_validate(row)
            for failure in row["failures"]:
                key = (failure["name"], failure.get("source_path"))
                groups.setdefault(key, {})[attempt.run_id] = attempt
        failures = [
            RecurringTestFailure(name=name, source_path=path, attempts=list(attempts.values()))
            for (name, path), attempts in groups.items() if len(attempts) >= 2
        ]
        failures.sort(
            key=lambda failure: (-len(failure.attempts), failure.name, failure.source_path or "")
        )
        return RecurringTestFailures(
            window_size=limit, attempts_examined=len(rows), failures=failures,
        )

    def read_test_run_for_commit(
        self, project_key: str, repository_key: str, commit: str,
    ) -> TestRun | None:
        rows = self._database().aql.execute(
            """
            FOR event IN @@events
                FILTER event.event_type == "test.run.finished" AND event.project_key == @project
                FILTER event.payload.repository_key == @repository AND event.payload.commit == @commit
                SORT event.occurred_at_us DESC, event._key DESC
                LIMIT 1
                RETURN event.payload
            """,
            bind_vars={
                "@events": self._events, "project": project_key,
                "repository": repository_key, "commit": commit,
            },
        )
        row = next(iter(rows), None)
        return TestRun.model_validate(row) if row else None

    def _read_test_run(
        self, project_key: str, run_id: str | None, *, passed_only: bool = False,
    ) -> TestRunDetail | None:
        database = self._database()
        payload = next(iter(database.aql.execute(
            """
            FOR event IN @@events
                FILTER event.event_type == "test.run.finished" AND event.project_key == @key
                FILTER @run_id == null OR event.payload.run_id == @run_id
                FILTER !@passed_only OR event.payload.status == "passed"
                SORT event.occurred_at_us DESC, event._key DESC
                LIMIT 1
                RETURN event.payload
            """,
            bind_vars={
                "@events": self._events, "key": project_key,
                "run_id": run_id, "passed_only": passed_only,
            },
        )), None)
        if payload is None:
            return None
        run = TestRun.model_validate(payload)
        paths = sorted({failure.source_path for failure in run.failures if failure.source_path})
        links: dict[str, list[TestValidationLink]] = {}
        if paths:
            rows = database.aql.execute(
                """
                WITH @@entities
                LET repo = DOCUMENT(@repository)
                FOR suite IN @@entities
                    FILTER repo != null AND suite.entity_type == "TestSuite"
                    FILTER suite.repository == repo.name AND suite.source_path IN @paths
                    FOR target, edge IN 1..1 INBOUND suite._id @@edges
                        FILTER edge.relationship_type == "validated_by"
                        FILTER target.entity_type IN ["ADR", "Constraint"]
                        SORT target.entity_type, target.name, target._key
                        RETURN DISTINCT {path: suite.source_path, link: {
                            key: target._key, name: target.name, entity_type: target.entity_type,
                            memory_key: target.memory_key
                        }}
                """,
                bind_vars={
                    "@entities": self._entities, "@edges": self._edges,
                    "repository": f"{self._entities}/{run.repository_key}", "paths": paths,
                },
            )
            for row in rows:
                links.setdefault(row["path"], []).append(TestValidationLink.model_validate(row["link"]))
        return TestRunDetail(
            **run.model_dump(exclude={"failures"}),
            failures=[
                TestFailureDetail(**failure.model_dump(), validations=links.get(failure.source_path, []))
                for failure in run.failures
            ],
        )

    def put_link(self, link: ArchitectureLink) -> ArchitectureRelationship:
        if link.source == _STATIC_SOURCE:
            raise ArchitectureGraphError(
                "The static-analysis source is reserved for Python ingestion"
            )
        if link.source.startswith(
            (
                _OBSERVATION_SOURCE_PREFIX,
                _GOAL_SOURCE_PREFIX,
                _CONSTRAINT_SOURCE_PREFIX,
                _CONVERSATION_SOURCE_PREFIX,
            )
        ):
            raise ArchitectureGraphError("This relationship source prefix is reserved")
        payload = self._edge_payload(link, datetime.now(UTC).isoformat())
        with self._transaction() as transaction:
            nodes = transaction.collection(self._entities)
            for key in (link.source_key, link.target_key):
                if not nodes.has(key):
                    raise ArchitectureGraphError(f"Architecture entity '{key}' was not found")
            edges = transaction.collection(self._edges)
            previous = edges.get(payload["_key"])
            if previous:
                payload["created_at"] = previous["created_at"]
            result = edges.insert(payload, overwrite_mode="replace", return_new=True)
        return ArchitectureRelationship.model_validate(result["new"]).model_copy(
            update={
                "source_key": link.source_key,
                "target_key": link.target_key,
                "relationship_type": link.relationship_type,
            }
        )

    def replace_index(
        self, index: ArchitectureIndex, *, capture_order: str | None = None,
    ) -> int:
        entities = [
            {**self._entity_payload(node), "repository": index.repository, "index_state": "current"}
            for node in index.entities
        ]
        keys = {node["_key"] for node in entities}
        if len(keys) != len(entities) or any(
            link.source_key not in keys
            or link.target_key not in keys
            or link.source != _STATIC_SOURCE
            for link in index.links
        ):
            raise ArchitectureGraphError(
                "An index must contain unique entities and all static link endpoints"
            )
        roots = [
            node["_key"] for node in entities
            if node["entity_type"] == "Repository" and node.get("role") == "source"
            and node["name"] == index.repository
        ]
        root_key = roots[0] if len(roots) == 1 else None
        now = datetime.now(UTC).isoformat()
        for node in entities:
            if node["_key"] == root_key:
                node["last_indexed_at"] = now
        edges = {}
        for link in index.links:
            payload = self._edge_payload(link, now)
            edges[payload["_key"]] = {**payload, "index_repository": index.repository}

        with self._transaction() as transaction:
            if capture_order and root_key:
                root = transaction.collection(self._entities).get(root_key) or {}
                if root.get("capture_index_order", "") > capture_order:
                    return 0
                for node in entities:
                    if node["_key"] == root_key:
                        node["capture_index_order"] = capture_order
            elif root_key:
                root = transaction.collection(self._entities).get(root_key) or {}
                for node in entities:
                    if node["_key"] == root_key and "capture_index_order" in root:
                        node["capture_index_order"] = root["capture_index_order"]
            collision = list(
                transaction.aql.execute(
                    """
                FOR node IN @@entities
                    FILTER node._key IN @keys
                    FILTER node.index_state == null OR node.repository != @repository
                    FILTER !(node._key == @root_key AND node.index_state == null
                        AND node.entity_type == "Repository" AND node.role == "source"
                        AND node.name == @repository
                        AND (node.repository == null OR node.repository == @repository))
                    LIMIT 1
                    RETURN node._key
                """,
                    bind_vars={
                        "@entities": self._entities,
                        "keys": sorted(keys),
                        "repository": index.repository,
                        "root_key": root_key,
                    },
                )
            )
            if collision:
                raise ArchitectureGraphError(
                    f"Indexed key '{collision[0]}' is owned by another source"
                )

            for start in range(0, len(entities), _BATCH_SIZE):
                list(
                    transaction.aql.execute(
                        """
                    FOR node IN @nodes
                        INSERT node IN @@entities OPTIONS {overwriteMode: "replace"}
                    """,
                        bind_vars={
                            "@entities": self._entities,
                            "nodes": entities[start : start + _BATCH_SIZE],
                        },
                    )
                )
            edge_rows = list(edges.values())
            for start in range(0, len(edge_rows), _BATCH_SIZE):
                list(
                    transaction.aql.execute(
                        """
                    FOR link IN @links
                        UPSERT {_key: link._key}
                            INSERT link
                            UPDATE UNSET(link, "created_at") IN @@edges
                    """,
                        bind_vars={
                            "@edges": self._edges,
                            "links": edge_rows[start : start + _BATCH_SIZE],
                        },
                    )
                )
            list(
                transaction.aql.execute(
                    """
                FOR edge IN @@edges
                    FILTER edge.index_repository == @repository AND edge.source == @source
                    FILTER edge._key NOT IN @keys
                    REMOVE edge IN @@edges
                """,
                    bind_vars={
                        "@edges": self._edges,
                        "repository": index.repository,
                        "source": _STATIC_SOURCE,
                        "keys": list(edges),
                    },
                )
            )
            list(
                transaction.aql.execute(
                    """
                FOR node IN @@entities
                    FILTER node.repository == @repository AND node.index_state == "current"
                    FILTER node._key NOT IN @keys
                    UPDATE node WITH {index_state: "stale"} IN @@entities
                """,
                    bind_vars={
                        "@entities": self._entities,
                        "repository": index.repository,
                        "keys": sorted(keys),
                    },
                )
            )
        return len(edges)

    def read_entity(self, key: str) -> ArchitectureEntity | None:
        row = self._database().collection(self._entities).get(key)
        return _ENTITY.validate_python(row) if row is not None else None

    def find_memory_entities(self, key: str) -> list[ArchitectureEntity]:
        rows = self._database().aql.execute(
            """
            FOR node IN @@entities
                FILTER node.memory_key == @key AND node.entity_type IN ["ADR", "DomainConcept"]
                FILTER node.index_state != "stale"
                SORT node._key
                RETURN node
            """,
            bind_vars={"@entities": self._entities, "key": key},
        )
        return [_ENTITY.validate_python(row) for row in rows]

    def project_source_repositories(self, key: str) -> list[Repository]:
        rows = self._database().aql.execute(
            """
            WITH @@entities
            FOR node, edge IN 1..1 ANY @seed @@edges
                FILTER node.entity_type == "Repository" AND node.role == "source"
                FILTER node.index_state != "stale" AND edge.relationship_type != "uses"
                SORT LOWER(node.name), node._key
                RETURN DISTINCT node
            """,
            bind_vars={
                "@entities": self._entities, "@edges": self._edges,
                "seed": f"{self._entities}/{key}",
            },
        )
        return [Repository.model_validate({**row, "key": row["_key"]}) for row in rows]

    def change_test_runs(
        self, project: str, repository: str, commits: list[str],
    ) -> dict[str, TestRun]:
        rows = self._database().aql.execute(
            """
            FOR event IN @@events
                FILTER event.event_type == "test.run.finished" AND event.project_key == @project
                FILTER event.payload.repository_key == @repository AND event.payload.commit IN @shas
                SORT event.occurred_at_us DESC, event._key DESC
                RETURN event.payload
            """,
            bind_vars={
                "@events": self._events, "project": project,
                "repository": repository, "shas": commits,
            },
        )
        runs: dict[str, TestRun] = {}
        for row in rows:
            if row["commit"] not in runs:
                runs[row["commit"]] = TestRun.model_validate(row)
        return runs

    def change_branch_cleanups(self, project: str, urls: list[str]) -> dict[str, BranchCleanup]:
        rows = self._database().aql.execute(
            """
            FOR event IN @@events
                FILTER event.event_type == "test.fix.workflow" AND event.project_key == @project
                LET fix = event.payload
                FILTER fix.publication.url IN @urls AND fix.branch_deleted_at != null
                SORT fix.branch_deleted_at DESC, event._key DESC
                RETURN { url: fix.publication.url, branch: fix.fix_branch.branch,
                         deleted_at: fix.branch_deleted_at }
            """,
            bind_vars={"@events": self._events, "project": project, "urls": urls},
        )
        cleanups: dict[str, BranchCleanup] = {}
        for row in rows:
            if row["url"] not in cleanups:
                cleanups[row["url"]] = BranchCleanup.model_validate(row)
        return cleanups

    def record_usage(self, receipt: UsageReceipt, owner: str, session: str | None) -> None:
        self._database().collection(self._events).insert({
            "_key": f"usage-{receipt.request_id}", "event_type": "retrieval.usage",
            "owner": owner, "usage_session": session, **receipt.model_dump(mode="json"),
        })

    def read_usage(self, request_id: str, owner: str) -> UsageReceipt | None:
        row = self._database().collection(self._events).get(f"usage-{request_id}")
        if not row or row.get("owner") != owner or row.get("event_type") != "retrieval.usage":
            return None
        return UsageReceipt.model_validate(row)

    def report_usage(self, request_id: str, owner: str, outcome: UsageOutcome) -> UsageReceipt | None:
        key = f"usage-{request_id}"
        with self._transaction(with_events=True) as transaction:
            journal = transaction.collection(self._events)
            row = journal.get(key)
            if not row or row.get("owner") != owner or row.get("event_type") != "retrieval.usage":
                return None
            result = UsageReceipt.model_validate(row)
            result.outcome = outcome
            result.response_tokens = outcome.response_tokens
            result.total_tokens = result.query_tokens + result.context_tokens + (outcome.response_tokens or 0)
            if result.savings_tokens is not None:
                result.naive_rag_tokens = result.total_tokens + result.savings_tokens
            journal.update({"_key": key, **result.model_dump(mode="json")})
            return result

    def query_usage(
        self, scope: UsageScope, project: str | None, session: str | None, *, allowed_projects: list[str] | None = None,
    ) -> tuple[list[dict], list[UsageReceipt]]:
        if (scope == "session" and not session) or (scope == "project" and not project):
            return [], []
        database = self._database()
        bindings = {"@events": self._events, "scope": scope, "project": project, "session": session, "allowed": allowed_projects}
        selection = """
        FOR row IN @@events
            FILTER row.event_type == "retrieval.usage"
            FILTER @allowed == null OR row.project_key IN @allowed
            FILTER @scope != "project" OR row.project_key == @project
            FILTER @scope != "session" OR row.usage_session == @session
        """
        groups = list(database.aql.execute(selection + """
            COLLECT day = LEFT(row.timestamp, 10), method = row.method
                AGGREGATE requests = COUNT(row),
                    query_tokens = SUM(row.query_tokens),
                    context_tokens = SUM(row.context_tokens),
                    response_tokens = SUM(NOT_NULL(row.response_tokens, 0)),
                    total_tokens = SUM(row.total_tokens),
                    savings_tokens = SUM(NOT_NULL(row.savings_tokens, 0)),
                    measured_savings_requests = SUM(row.savings_tokens == null ? 0 : 1),
                    reported_outcomes = SUM(row.outcome == null ? 0 : 1),
                    successful_completions = SUM(row.outcome.completed == true ? 1 : 0),
                    retries = SUM(NOT_NULL(row.outcome.follow_up_retries, 0))
            RETURN {day, method, requests, query_tokens, context_tokens, response_tokens,
                    total_tokens, savings_tokens, measured_savings_requests, reported_outcomes,
                    successful_completions, retries}
        """, bind_vars=bindings))
        recent = database.aql.execute(selection + """
            SORT row.timestamp DESC, row.request_id DESC
            LIMIT 20
            RETURN row
        """, bind_vars=bindings)
        return groups, [UsageReceipt.model_validate(row) for row in recent]

    def save_development_outcome(self, outcome: DevelopmentOutcome, actor: str) -> OutcomeReceipt:
        key = architecture_key("development-outcome", outcome.project_key, outcome.repository_key, outcome.change_id)
        with self._transaction(with_events=True, read_edges=True) as transaction:
            journal = transaction.collection(self._events)
            existing = journal.get(key)
            if existing is not None:
                previous = OutcomeReceipt.model_validate(existing)
                if previous.outcome != outcome:
                    raise ArchitectureGraphError("A different outcome already exists for this change")
                return previous
            nodes = transaction.collection(self._entities)
            self._require_entity_type(nodes, outcome.project_key, "Project")
            repo = self._require_entity_type(nodes, outcome.repository_key, "Repository")
            edges = transaction.collection(self._edges)
            if repo.get("role") != "source" or outcome.project_key not in self._direct_projects(
                nodes, edges, outcome.repository_key,
            ):
                raise ArchitectureGraphError("Source repository is outside the project")
            if outcome.task_key:
                self._require_entity_type(nodes, outcome.task_key, "Task")
                if self._direct_projects(nodes, edges, outcome.task_key) != {outcome.project_key}:
                    raise ArchitectureGraphError("Task is outside the project")
            for rule in outcome.violated_constraints:
                constraint = self._require_entity_type(nodes, rule, "Constraint")
                if constraint.get("repository") != repo["name"] and not (
                    outcome.project_key in self._direct_projects(nodes, edges, rule)
                    or self._constraint_governs_repository(nodes, edges, rule, repo["name"])
                ):
                    raise ArchitectureGraphError("Constraint is outside the project and repository")
            receipt = OutcomeReceipt(
                key=key, outcome=outcome, recorded_at=datetime.now(UTC), recorded_by=actor,
            )
            journal.insert({
                "_key": key, "event_type": "development.outcome",
                "project_key": outcome.project_key,
                **receipt.model_dump(mode="json"),
            })
            return receipt

    def _constraint_governs_repository(self, nodes, edges, rule: str, repository: str) -> bool:
        handle = f"{self._entities}/{rule}"
        for edge in edges.find({"_to": handle, "relationship_type": "constrained_by"}):
            target = nodes.get(edge["source_key"])
            if target and target.get("repository") == repository and target.get("index_state") != "stale":
                return True
        return False

    def effectiveness_graph(
        self, project: str, allowed: list[str], repositories: list[str], repository: str | None, *, strict_scope: bool = False,
    ) -> dict:
        rows = self._database().aql.execute(
            """
            WITH @@entities
            LET owned = (
                FOR node IN @@entities
                    FILTER !@strict AND node.repository IN @repositories AND node.index_state == "stale"
                    RETURN node._key
            )
            LET seeds = @repository == null ? @allowed : (
                FOR node IN @@entities
                    FILTER node._key IN @allowed
                    FILTER node.repository == @repository OR
                        (node.entity_type == "Repository" AND node.role == "source" AND node.name == @repository)
                    RETURN node._key
            )
            LET nearby = (
                FOR key IN seeds
                    FOR node, edge IN 1..1 ANY CONCAT(@entities_name, "/", key) @@edges
                        FILTER node.index_state != "stale"
                        FILTER node.metadata.capture_project == null OR node.metadata.capture_project == @project
                        FILTER (node._key IN @allowed AND node.entity_type IN
                                ["ADR", "Constraint", "Goal", "Observation", "Task", "TestSuite"]
                                AND edge.relationship_type != "uses")
                            OR (!@strict AND node.entity_type IN ["ADR", "Constraint", "Goal"] AND edge.relationship_type IN
                                ["implements", "constrained_by", "supports_goal", "validated_by"])
                        RETURN node._key
            )
            LET keys = UNION_DISTINCT(seeds, nearby, @repository == null ? [@project] : [])
            LET nodes = (
                FOR key IN keys
                    LET node = DOCUMENT(@entities_name, key)
                    FILTER node != null AND node.index_state != "stale"
                    RETURN {key, type: node.entity_type, kind: node.role, memory_key: node.memory_key,
                            status: node.status, enforcement: node.enforcement_status,
                            documented: (IS_STRING(node.rule) AND LENGTH(node.rule) > 0
                                AND IS_STRING(node.authority) AND LENGTH(node.authority) > 0)
                                OR (node.entity_type == "Goal" AND LENGTH(node.success_criteria) > 0),
                            automatic: node.metadata.capture_project == @project AND node.source != "agent_reflection",
                            suggestions: LENGTH(node.suggestions)}
            )
            LET links = (
                FOR edge IN @@edges
                    FILTER edge.source_key IN keys AND edge.target_key IN keys
                    RETURN {source: edge.source_key, target: edge.target_key,
                        type: edge.relationship_type, inferred: edge.source == "static-analysis" AND edge.confidence < 1}
            )
            RETURN {nodes, links, stale: LENGTH(owned)}
            """,
            bind_vars={
                "@entities": self._entities, "@edges": self._edges, "entities_name": self._entities,
                "strict": strict_scope, "project": project, "allowed": allowed, "repositories": repositories, "repository": repository,
            },
        )
        return next(iter(rows))

    def effectiveness_events(
        self, project: str, repository: str | None, start: str | None, end: str | None,
    ) -> list[dict]:
        return list(self._database().aql.execute(
            """
            FOR row IN @@events
                FILTER row.project_key == @project
                LET outcome = IS_OBJECT(row.outcome) ? row.outcome : {}
                LET repo = NOT_NULL(outcome.repository_key, row.repository_key, row.payload.repository_key)
                FILTER @repository == null OR repo == @repository
                LET day = LEFT(NOT_NULL(outcome.occurred_at, row.occurred_at, row.timestamp, row.payload.finished_at, row.recorded_at), 10)
                FILTER (@start == null OR day >= @start) AND (@end == null OR day <= @end)
                FILTER row.event_type IN ["development.outcome", "task.completed", "task.upserted", "retrieval.usage", "test.run.finished"]
                RETURN KEEP(row, ["event_type", "outcome", "payload", "task_key", "context_tokens", "savings_tokens",
                    "occurred_at", "recorded_at"])
            """,
            bind_vars={
                "@events": self._events, "project": project, "repository": repository,
                "start": start, "end": end,
            },
        ))

    def read_agent_registry(self) -> dict:
        return self._database().collection(self._events).get("agent-registry") or {
            "revision": 0, "agents": {},
        }

    def save_agent_registry(self, agent: str, config: dict, revision: int, actor: str) -> None:
        with self._transaction(with_events=True) as tx:
            journal = tx.collection(self._events)
            previous = journal.get("agent-registry") or {"revision": 0, "agents": {}}
            if previous["revision"] != revision:
                raise ArchitectureGraphError("Agent settings changed. Reload before saving.")
            active = tx.aql.execute(
                """
                FOR job IN @@entities
                    FILTER job.entity_type == "Execution" AND job.agent == @agent
                    FILTER job.state IN ["READY", "RUNNING", "BLOCKED", "CANCEL_REQUESTED"]
                    LIMIT 1 RETURN true
                """,
                bind_vars={"@entities": self._entities, "agent": agent},
            )
            if next(iter(active), False):
                raise ArchitectureGraphError("Wait for active executions for this agent to finish.")
            journal.insert({
                "_key": "agent-registry", "event_type": "agent.settings",
                "revision": revision + 1, "agents": previous["agents"] | {agent: config},
                "updated_by": actor, "updated_at": datetime.now(UTC).isoformat(),
            }, overwrite_mode="replace")

    def read_guidance_state(self, project_key: str | None) -> GuidanceState:
        key = _guidance_key(project_key)
        return GuidanceState.model_validate(
            self._database().collection(self._events).get(key) or {},
        )

    def list_guidance_versions(
        self, project_key: str | None, before: int, limit: int,
    ) -> list[PublishedGuidance]:
        rows = self._database().aql.execute(
            """
            FOR event IN @@events
                FILTER event.event_type == @event_type
                FILTER event.project_key == @project AND event.version < @before
                SORT event.version DESC
                LIMIT @limit
                RETURN event
            """,
            bind_vars={
                "@events": self._events, "project": project_key, "before": before, "limit": limit,
                "event_type": _guidance_event_type(project_key, "published"),
            },
        )
        return [PublishedGuidance.model_validate(row) for row in rows]

    def save_guidance_draft(
        self, project_key: str | None, revision: int, text: str, actor: str,
    ) -> None:
        self._change_guidance(project_key, revision, actor, text=text)

    def approve_guidance(self, project_key: str | None, revision: int, actor: str) -> None:
        self._change_guidance(project_key, revision, actor, text=None)

    def _change_guidance(
        self, project_key: str | None, revision: int, actor: str, *, text: str | None,
    ) -> None:
        key = _guidance_key(project_key)
        with self._transaction(with_events=True) as transaction:
            if project_key is not None:
                self._require_entity_type(transaction.collection(self._entities), project_key, "Project")
            journal = transaction.collection(self._events)
            state = GuidanceState.model_validate(journal.get(key) or {})
            if revision != state.revision:
                raise ArchitectureGraphError("Guidance changed. Reload it before continuing.")
            state.revision += 1
            now = datetime.now(UTC)
            if text is not None:
                state.draft = GuidanceDraft(text=text, edited_by=actor, edited_at=now)
            else:
                if state.draft is None:
                    raise ArchitectureGraphError("Save a draft before approving it.")
                version = (state.published.version if state.published else 0) + 1
                state.published = PublishedGuidance(
                    **state.draft.model_dump(), version=version, approved_by=actor, published_at=now,
                )
                journal.insert({
                    "_key": _guidance_key(project_key, version),
                    "event_type": _guidance_event_type(project_key, "published"),
                    "project_key": project_key,
                    **state.published.model_dump(mode="json"),
                })
                state.draft = None
            journal.insert({
                "_key": key, "event_type": _guidance_event_type(project_key, "state"),
                "project_key": project_key,
                **state.model_dump(mode="json"),
            }, overwrite_mode="replace")

    def guidance_scope(self, project_key: str) -> GuidanceScope:
        rows = self._database().aql.execute(
            """
            WITH @@entities
            LET direct = (
                FOR node, edge IN 1..1 ANY @seed @@edges
                    FILTER node.index_state != "stale" AND node.entity_type != "Project"
                    FILTER edge.relationship_type != "uses"
                    RETURN DISTINCT node._key
            )
            LET repositories = (
                FOR key IN direct
                    LET node = DOCUMENT(@entity_collection, key)
                    FILTER node.entity_type == "Repository" AND node.role == "source"
                    RETURN node
            )
            LET defined = (
                FOR repo IN repositories
                    FOR node, edge IN 1..1 OUTBOUND repo._id @@edges
                        FILTER edge.relationship_type == "defines" AND node.index_state != "stale"
                        RETURN DISTINCT node._key
            )
            LET owned = (
                FOR node IN @@entities
                    FILTER node.repository IN repositories[*].name AND node.index_state != "stale"
                    RETURN node._key
            )
            LET keys = UNION_DISTINCT(direct, defined, owned)
            LET adr_keys = (
                FOR key IN keys
                    LET node = DOCUMENT(@entity_collection, key)
                    FILTER node.entity_type == "ADR" AND node.memory_key != null
                    SORT node.memory_key
                    RETURN DISTINCT node.memory_key
            )
            RETURN {entity_keys: PUSH(keys, @key, true), adr_keys}
            """,
            bind_vars={
                "@entities": self._entities, "@edges": self._edges,
                "seed": f"{self._entities}/{project_key}", "key": project_key,
                "entity_collection": self._entities,
            },
        )
        return GuidanceScope.model_validate(next(iter(rows)))

    def guidance_constraints(self, scoped_keys: list[str]) -> list[Constraint]:
        rows = self._database().aql.execute(
            """
            WITH @@entities
            FOR node IN @@entities
                FILTER node.entity_type == "Constraint" AND node.index_state != "stale"
                FILTER node.enforcement_status IN ["unenforced", "partially_enforced", "enforced"]
                FILTER node.rule != null
                FILTER node._key IN @keys OR LENGTH(
                    FOR target, edge IN 1..1 INBOUND node._id @@edges
                        FILTER edge.relationship_type == "constrained_by" AND target._key IN @keys
                        LIMIT 1
                        RETURN 1
                ) > 0
                SORT LOWER(node.name), node._key
                RETURN MERGE(node, {key: node._key})
            """,
            bind_vars={"@entities": self._entities, "@edges": self._edges, "keys": scoped_keys},
        )
        return [Constraint.model_validate(row) for row in rows]

    def project_access_keys(self) -> list[str]:
        return list(self._database().aql.execute(
            "FOR node IN @@entities FILTER node.entity_type == 'Project' RETURN node._key",
            bind_vars={"@entities": self._entities},
        ))

    def project_scope(self, key: str, *, strict_scope: bool = False) -> ProjectScope:
        rows = self._database().aql.execute(
            """
            WITH @@entities
            LET direct = (
                FOR node, edge IN 1..1 ANY @seed @@edges
                    FILTER node.index_state != "stale" AND node.entity_type != "Project"
                    FILTER @strict ? edge.relationship_type IN ["defines", "related_to", "references"]
                        : edge.relationship_type != "uses"
                    RETURN DISTINCT node._key
            )
            LET repositories = (
                FOR key IN direct
                    LET node = DOCUMENT(@entities, key)
                    FILTER node.entity_type == "Repository" AND node.role == "source"
                    FILTER !@strict OR LENGTH(
                        FOR candidate IN @@entities
                            FILTER candidate.entity_type == "Repository" AND candidate.role == "source"
                            FILTER candidate.name == node.name
                            LIMIT 2
                            RETURN true
                    ) == 1
                    RETURN node
            )
            LET repository_names = UNIQUE(repositories[*].name)
            LET defined = (
                FOR repo IN repositories
                    FOR node, edge IN 1..1 OUTBOUND repo._id @@edges
                        FILTER edge.relationship_type == "defines" AND node.index_state != "stale"
                        FILTER !@strict OR node.entity_type != "Project"
                        RETURN DISTINCT node._key
            )
            LET owned = (
                FOR node IN @@entities
                    FILTER node.repository IN repository_names AND node.index_state != "stale"
                    FILTER !@strict OR node.entity_type != "Project"
                    RETURN node._key
            )
            LET scoped = (
                FOR key IN UNION_DISTINCT(direct, defined, owned)
                    LET node = DOCUMENT(@entities, key)
                    FILTER !@strict OR (
                        (node.metadata.capture_project == null OR node.metadata.capture_project == @key)
                        AND (node.project_key == null OR node.project_key == @key)
                    )
                    RETURN key
            )
            LET project_keys = APPEND(scoped, @key)
            LET record_dates = (
                FOR key IN project_keys
                    LET node = DOCUMENT(@entities, key)
                    FOR value IN [node.created_at, node.updated_at, node.timestamp,
                                  node.last_indexed_at]
                        FILTER IS_STRING(value)
                        RETURN LEFT(value, 10)
            )
            LET edge_dates = (
                FOR key IN project_keys
                    LET node = DOCUMENT(@entities, key)
                    FOR target, edge IN 1..1 OUTBOUND node._id @@edges
                        FILTER target._key IN project_keys
                        RETURN {created: LEFT(edge.created_at, 10),
                                updated: LEFT(edge.updated_at, 10),
                                index_repo: edge.source == @static ? edge.index_repository : null}
            )
            LET index_dates = (
                FOR repo IN repositories
                    RETURN repo.last_indexed_at != null ? LEFT(repo.last_indexed_at, 10) : MAX(
                        FOR edge IN edge_dates
                            FILTER edge.index_repo == repo.name
                            RETURN edge.updated
                    )
            )
            LET entities = (
                FOR key IN scoped
                    LET node = DOCUMENT(@entities, key)
                    LET decision_links = (
                        FOR adr IN (node.entity_type == "ADR" ? [node] : [])
                        FOR related, edge IN 1..1 ANY adr._id @@edges
                            FILTER related.index_state != "stale"
                            FILTER (edge.relationship_type == "implements" AND edge._to == adr._id)
                                OR (edge.relationship_type == "validated_by"
                                    AND edge._from == adr._id)
                            RETURN DISTINCT {
                                key: related._key, name: related.name,
                                relationship: edge.relationship_type == "implements"
                                    ? "implemented_by" : "validated_by"
                            }
                    )
                    LET parent_key = FIRST(
                        FOR symbol IN (node.entity_type IN ["Class", "Function"] ? [node] : [])
                        FOR parent, edge IN 1..1 INBOUND symbol._id @@edges
                            FILTER edge.relationship_type == "defines" AND edge.source == @static
                            FILTER parent.entity_type IN ["Class", "Function"]
                            FILTER parent._key IN scoped
                                AND parent.source_path == symbol.source_path
                            SORT parent._key
                            LIMIT 1
                            RETURN parent._key
                    )
                    SORT node.entity_type, LOWER(node.name), key
                    RETURN MERGE(KEEP(node, [
                        "name", "entity_type", "source_path", "memory_key",
                        "repository", "role", "qualified_name", "status"
                    ]), {
                        key, decision_links, parent_key,
                        reason: key IN direct ? "Linked to project"
                            : key IN defined ? "Defined in a linked repository"
                            : CONCAT("Indexed in ", node.repository)
                    })
            )
            LET dependencies = (
                FOR key IN APPEND(scoped, @key)
                    LET owner = DOCUMENT(@entities, key)
                    FOR target, link IN 1..1 OUTBOUND owner._id @@edges
                        FILTER link.relationship_type == "uses" AND target.index_state != "stale"
                        FILTER target._key NOT IN scoped AND target._key != @key
                        FILTER target.repository != null
                            OR target.entity_type IN ["Project", "Repository"]
                        COLLECT target_key = target._key
                        LET node = DOCUMENT(@entities, target_key)
                        SORT LOWER(node.name), target_key
                        RETURN MERGE(
                            KEEP(node, ["name", "entity_type", "source_path", "memory_key"]),
                            {key: target_key, reason: "Used by a project entity"}
                        )
            )
            LET dates = APPEND(record_dates, APPEND(edge_dates[*].created, edge_dates[*].updated))
            RETURN {entities, dependencies, repository_names,
                    earliest_record: MIN(dates), latest_record: MAX(dates),
                    last_index_date: MAX(index_dates)}
            """,
            bind_vars={
                "@entities": self._entities, "strict": strict_scope, "@edges": self._edges,
                "entities": self._entities, "seed": f"{self._entities}/{key}", "key": key,
                "static": _STATIC_SOURCE,
            },
        )
        return ProjectScope.model_validate(next(iter(rows)))

    def count_by_entity_type(
        self, *, source_repositories_only: bool = False, allowed_entity_keys: list[str] | None = None
    ) -> dict[EntityType, int]:
        rows = self._database().aql.execute(
            """
            FOR node IN @@entities
                FILTER !@source_repositories_only
                    OR node.entity_type != "Repository" OR node.role == "source"
                COLLECT entity_type = node.entity_type WITH COUNT INTO total
                RETURN {entity_type, total}
            """,
            bind_vars={
                "@entities": self._entities, "allowed": allowed_entity_keys,
                "source_repositories_only": source_repositories_only,
            },
        )
        return {row["entity_type"]: row["total"] for row in rows}

    def search(self, request: ArchitectureSearch, *, allowed_entity_keys: list[str] | None = None) -> list[ArchitectureEntity]:
        query = """
        FOR node IN @@entities
            FILTER @allowed == null OR node._key IN @allowed
            FILTER @include_stale OR node.index_state != "stale"
            FILTER @entity_type == null OR node.entity_type == @entity_type
            FILTER @query == "" OR CONTAINS(LOWER(node.name), @query)
                OR CONTAINS(LOWER(NOT_NULL(node.qualified_name, "")), @query)
                OR CONTAINS(LOWER(NOT_NULL(node.source_path, "")), @query)
                OR CONTAINS(LOWER(NOT_NULL(node.description, "")), @query)
                OR CONTAINS(LOWER(NOT_NULL(node.summary, "")), @query)
                OR CONTAINS(LOWER(NOT_NULL(node.rule, "")), @query)
                OR CONTAINS(LOWER(NOT_NULL(node.scope, "")), @query)
                OR CONTAINS(LOWER(NOT_NULL(node.authority, "")), @query)
                OR CONTAINS(LOWER(NOT_NULL(node.memory_key, "")), @query)
            SORT LOWER(node.name), node._key
            LIMIT @limit
            RETURN node
        """
        rows = self._database().aql.execute(
            query,
            bind_vars={
                "@entities": self._entities, "allowed": allowed_entity_keys,
                "query": request.query.strip().lower(),
                "entity_type": request.entity_type,
                "include_stale": request.include_stale,
                "limit": request.limit,
            },
        )
        return [_ENTITY.validate_python(row) for row in rows]

    def trace(self, request: ArchitectureTrace, *, allowed_entity_keys: list[str] | None = None) -> list[ArchitecturePath]:
        database = self._database()
        if not database.collection(self._entities).has(request.key):
            raise ArchitectureGraphError(f"Architecture entity '{request.key}' was not found")
        uniqueness = "global" if request.shortest_paths else "path"
        query = """
        WITH @@entities
        FOR entity, edge, path IN @min_depth..@depth ANY @seed @@edges
            PRUNE @allowed != null AND entity._key NOT IN @allowed
            OPTIONS {order: "bfs", uniqueVertices: @uniqueness}
            FILTER @allowed == null OR entity._key IN @allowed
            FILTER LENGTH(@entity_types) == 0 OR entity.entity_type IN @entity_types
            LET relationships = (
                FOR i IN 0..LENGTH(path.edges) - 1
                    LET link = path.edges[i]
                    LET from = path.vertices[i]
                    LET to = path.vertices[i + 1]
                    RETURN MERGE(link, {
                        source_key: from._key,
                        target_key: to._key,
                        relationship_type: link._from == from._id
                            ? link.relationship_type : @inverse[link.relationship_type]
                    })
            )
            FILTER LENGTH(@types) == 0 OR LAST(relationships).relationship_type IN @types
        """
        if request.prioritize:
            query += """
                LET kind = LAST(relationships).relationship_type
                LET priority = kind IN ["implements", "implemented_by", "validates",
                    "validated_by", "supersedes", "superseded_by"] ? 0
                    : kind IN ["constrains", "constrained_by", "persists_to", "persisted_by",
                        "uses", "used_by"] ? 1 : 2
                SORT priority, edge.confidence DESC, entity._key, edge._key
            """
        query += """
            LIMIT @limit
            RETURN {entity, nodes: path.vertices, relationships}
        """
        rows = database.aql.execute(
            query,
            bind_vars={
                "@entities": self._entities, "allowed": allowed_entity_keys,
                "@edges": self._edges,
                "seed": f"{self._entities}/{request.key}",
                "depth": request.depth,
                "min_depth": request.min_depth,
                "uniqueness": uniqueness,
                "inverse": INVERSE_RELATIONSHIPS,
                "types": request.relationship_types,
                "entity_types": request.entity_types,
                "limit": request.limit,
            },
        )
        return [ArchitecturePath.model_validate(row) for row in rows]



def _guidance_key(project_key: str | None, version: int | None = None) -> str:
    scope = ("global-guidance",) if project_key is None else ("project-guidance", project_key)
    prefix, *parts = scope
    if version is None:
        return architecture_key(prefix, *parts)
    return architecture_key(f"{prefix}-version", *parts, str(version))


def _guidance_event_type(project_key: str | None, event: str) -> str:
    scope = "global" if project_key is None else "project"
    return f"{scope}.guidance.{event}"
