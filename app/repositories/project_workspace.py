"""Transactional writes for project work and task executions."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.architecture import (
    Agent,
    ArchitectureLink,
    Artifact,
    Constraint,
    Execution,
    Goal,
    Observation,
    Risk,
    Task,
    architecture_key,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.project_workspace import (
    AgentArtifact,
    AgentResult,
    StartExecution,
    TaskUpdate,
    WorkDraft,
)


class ProjectWorkspaceRepository:
    def __init__(self, graph):
        self.graph = graph

    def entities(self, project):
        return self.graph.project_scope(project, strict_scope=True).entities

    def task(self, project, key):
        if not any(
            node.key == key and node.entity_type == "Task" for node in self.entities(project)
        ):
            raise ArchitectureGraphError("Task is outside this project")
        task = self.graph.read_entity(key)
        if not isinstance(task, Task):
            raise ArchitectureGraphError("Task no longer exists")
        return task

    def put(self, tx, node):
        tx.collection(self.graph._entities).insert(
            self.graph._entity_payload(node),
            overwrite_mode="replace",
        )

    def link(self, tx, left, right, kind, now):
        document = self.graph._edge_payload(
            ArchitectureLink(
                source_key=left,
                target_key=right,
                relationship_type=kind,
                source="project-workspace",
            ),
            now.isoformat(),
        )
        tx.collection(self.graph._edges).insert(document, overwrite_mode="ignore")

    def create(self, project, draft: WorkDraft, actor: str):
        now = datetime.now(UTC)
        base = dict(
            key=architecture_key("workspace", project, uuid4().hex),
            name=draft.name,
            description=draft.description,
            metadata={"workspace_project": project, "created_by": actor},
        )
        if draft.kind == "Constraint":
            if not draft.scope or not draft.authority:
                raise ArchitectureGraphError("Constraints require both scope and authority")
            base["constrained_entity_keys"] = [project]
            node = Constraint(
                **base,
                rule=draft.description,
                scope=draft.scope,
                authority=draft.authority,
                enforcement_status="proposed",
            )
        elif draft.kind == "Goal":
            node = Goal(**base, project_key=project)
        else:
            node = (Task if draft.kind == "Task" else Risk)(**base)
        with self.graph._transaction() as tx:
            self.graph._require_entity_type(tx.collection(self.graph._entities), project, "Project")
            self.put(tx, node)
            self.link(tx, project, node.key, "defines", now)
            if draft.kind == "Constraint":
                self.link(tx, node.key, project, "constrains", now)
        return node

    def observation(self, tx, project, task, summary, evidence, actor, related, now, identity):
        key = architecture_key("workspace-outcome", project, task.key, identity)
        observation = Observation(
            key=key,
            name=f"Task outcome: {task.name}"[:500],
            source="project-workspace",
            summary=summary,
            confidence=1,
            timestamp=now,
            recorded_by=actor,
            evidence=[item[:1200] for item in evidence[:50]],
            related_entity_keys=list(dict.fromkeys([task.key, *related])),
            metadata={"capture_project": project, "workspace_project": project},
        )
        self.put(tx, observation)
        self.link(tx, project, key, "defines", now)
        self.link(tx, task.key, key, "produced", now)
        for target in dict.fromkeys(related):
            self.link(tx, key, target, "references", now)
        return key

    def _task_in_transaction(self, tx, project, task):
        nodes = tx.collection(self.graph._entities)
        row = self.graph._require_entity_type(nodes, task.key, "Task")
        if self.graph._direct_projects(nodes, tx.collection(self.graph._edges), task.key) != {
            project
        }:
            raise ArchitectureGraphError("Task must belong exclusively to this project")
        return Task.model_validate(row)

    def task_update(self, project, key, update: TaskUpdate, actor: str):
        task = self.task(project, key)
        if update.status == "done" and not update.outcome:
            raise ArchitectureGraphError("Record an outcome before completing a task")
        now = datetime.now(UTC)
        with self.graph._transaction() as tx:
            task = self._task_in_transaction(tx, project, task)
            if task.metadata.get("active_execution"):
                raise ArchitectureGraphError("An execution is already active for this task")
            if task.status == update.status:
                return task
            if update.status == "done":
                latest = task.metadata.get("latest_execution")
                row = tx.collection(self.graph._entities).get(latest) if latest else None
                related = (
                    [latest]
                    if row and row.get("project_key") == project and row.get("task_key") == key
                    else []
                )
                self.observation(
                    tx,
                    project,
                    task,
                    update.outcome,
                    [f"Outcome recorded by {actor}"],
                    actor,
                    related,
                    now,
                    uuid4().hex,
                )
            task = task.model_copy(update={"status": update.status})
            self.put(tx, task)
        return task

    def start(self, project, key, command: StartExecution, actor: str) -> Execution:
        task = self.task(project, key)
        now = datetime.now(UTC)
        execution_key = architecture_key("task-execution", project, key, command.request_id)
        agent_key = architecture_key("execution-agent", project, command.agent)
        with self.graph._transaction() as tx:
            nodes = tx.collection(self.graph._entities)
            existing = nodes.get(execution_key)
            if existing:
                execution = Execution.model_validate(existing)
                if (execution.agent, execution.mode, execution.requested_by) != (
                    command.agent,
                    command.mode,
                    actor,
                ):
                    raise ArchitectureGraphError("Execution request ID was already used")
                return execution
            task = self._task_in_transaction(tx, project, task)
            if task.metadata.get("active_execution") or task.status in {"done", "cancelled"}:
                raise ArchitectureGraphError("Task must be open and have no active execution")
            job = Execution(
                key=execution_key,
                name=f"{task.name} execution"[:500],
                project_key=project,
                task_key=key,
                requested_by=actor,
                agent=command.agent,
                mode=command.mode,
                created_at=now,
                updated_at=now,
                timeline=[{"state": "READY", "timestamp": now.isoformat(), "actor": actor}],
            )
            self.put(tx, Agent(key=agent_key, name=command.agent, project_key=project))
            self.put(tx, job)
            self.put(
                tx,
                task.model_copy(
                    update={
                        "status": "in_progress",
                        "metadata": {**task.metadata, "active_execution": job.key},
                    }
                ),
            )
            self.link(tx, project, job.key, "defines", now)
            self.link(tx, project, agent_key, "defines", now)
            self.link(tx, task.key, job.key, "produced", now)
            self.link(tx, job.key, agent_key, "uses", now)
        return job

    def executions(self, project, task_key):
        self.task(project, task_key)
        return self._query(
            "FILTER job.project_key == @project AND job.task_key == @task "
            "SORT job.created_at DESC LIMIT 30",
            {"project": project, "task": task_key},
        )

    def ready(self):
        return self._query(
            'FILTER job.state == "READY" OR '
            '(job.state IN ["RUNNING", "BLOCKED", "CANCEL_REQUESTED"] '
            "AND DATE_TIMESTAMP(job.lease_until) < @now) "
            "SORT job.updated_at LIMIT 10",
            {"now": int(datetime.now(UTC).timestamp() * 1000)},
        )

    def _query(self, query, bindings):
        return [
            Execution.model_validate(row)
            for row in self.graph._database().aql.execute(
                'FOR job IN @@nodes FILTER job.entity_type == "Execution" ' + query + " RETURN job",
                bind_vars={"@nodes": self.graph._entities, **bindings},
            )
        ]

    def transition(
        self,
        job,
        state,
        *,
        context_keys=None,
        result: AgentResult | None = None,
        failure=None,
        actor="execution-worker",
    ):
        now = datetime.now(UTC)
        with self.graph._transaction() as tx:
            nodes = tx.collection(self.graph._entities)
            prior = Execution.model_validate(
                self.graph._require_entity_type(nodes, job.key, "Execution")
            )
            if prior.revision != job.revision:
                return None
            updates = {
                "state": state,
                "revision": prior.revision + 1,
                "updated_at": now,
                "failure": failure,
                "lease_until": now + timedelta(minutes=10)
                if state in {"RUNNING", "BLOCKED"} or (state == "CANCEL_REQUESTED" and failure)
                else (now if state == "CANCEL_REQUESTED" else None),
                "timeline": [
                    *prior.timeline,
                    {
                        "state": state,
                        "timestamp": now.isoformat(),
                        "phase": prior.phase,
                        "actor": actor,
                    },
                ][-100:],
            }
            if context_keys is not None:
                updates["context_keys"] = context_keys
                for key in context_keys:
                    if nodes.get(key):
                        self.link(tx, job.key, key, "references", now)
            if result:
                updates["results"] = [*prior.results, result.model_dump(mode="json")]
                updates["phase"] = prior.phase + 1
            if state in {"COMPLETED", "PLANNED", "FAILED", "REVIEW", "CANCELLED"}:
                task = Task.model_validate(
                    self.graph._require_entity_type(nodes, job.task_key, "Task")
                )
                metadata = dict(task.metadata)
                if metadata.get("active_execution") != job.key:
                    raise ArchitectureGraphError("Task no longer belongs to this execution")
                metadata.pop("active_execution", None)
                metadata["latest_execution"] = job.key
                status = (
                    "done" if state == "COMPLETED" and task.status != "cancelled" else task.status
                )
                self.put(tx, task.model_copy(update={"status": status, "metadata": metadata}))
                if state == "COMPLETED" and result:
                    artifact_keys = []
                    produced = []
                    for entry in updates["results"]:
                        produced.extend(
                            AgentArtifact.model_validate(item) for item in entry["artifacts"]
                        )
                        produced.extend(
                            AgentArtifact(name="Modified file", reference=path)
                            for path in entry["files_modified"]
                        )
                        if entry["pull_request"]:
                            produced.append(
                                AgentArtifact(name="Pull request", reference=entry["pull_request"])
                            )
                    unique = {(item.name, item.reference): item for item in produced}
                    for index, item in enumerate(list(unique.values())[:50]):
                        key = architecture_key("execution-artifact", job.key, str(index))
                        artifact = Artifact(
                            key=key,
                            name=item.name,
                            reference=item.reference,
                            project_key=job.project_key,
                        )
                        self.put(tx, artifact)
                        self.link(tx, job.project_key, key, "defines", now)
                        self.link(tx, job.key, key, "produced", now)
                        artifact_keys.append(key)
                    context = [key for key in prior.context_keys if nodes.get(key)]
                    summary = "\n\n".join(
                        dict.fromkeys(
                            entry["lesson"] or entry["summary"] for entry in updates["results"]
                        )
                    )
                    tests = [test for entry in updates["results"] for test in entry["tests"]]
                    self.observation(
                        tx,
                        job.project_key,
                        task,
                        summary[:16000],
                        [f"Execution {job.key}", *tests],
                        job.requested_by,
                        [job.key, *artifact_keys, *context[:45]],
                        now,
                        job.key,
                    )
            if state in {"COMPLETED", "PLANNED", "REVIEW", "FAILED", "CANCELLED"}:
                updates["metadata"] = {
                    **prior.metadata,
                    "duration_seconds": (now - prior.created_at).total_seconds(),
                    "review_findings": sum(
                        len(entry["review_comments"])
                        for entry in updates.get("results", prior.results)
                    ),
                    "memories_created": int(state == "COMPLETED"),
                    "artifacts_created": len(artifact_keys)
                    if state == "COMPLETED" and result
                    else 0,
                }
            updated = prior.model_copy(update=updates)
            self.put(tx, updated)
            return updated
