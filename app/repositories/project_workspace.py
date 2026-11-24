"""Transactional writes for project work and task executions."""

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

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
from app.domain.execution_usage import ExecutionAttempt, ExecutionUsage
from app.domain.project_workspace import (
    AgentArtifact,
    AgentResult,
    StartExecution,
    TaskUpdate,
    WorkDraft,
)
from app.domain.repair_contract import TestPath


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

    @staticmethod
    def repair_target(review, project, task_key):
        if review.project_key != project or review.task_key != task_key or review.agent != "codex":
            raise ArchitectureGraphError("Review does not belong to this task")
        for result in reversed(review.results):
            target = result.get("reviewed_target") or {}
            if (
                result.get("review_verdict") == "blocking"
                and target.get("kind") == "implementation patch"
                and target.get("execution_id")
                and target.get("patch_sha256")
            ):
                return result, target
        raise ArchitectureGraphError("This review has no blocking findings on an identified patch")

    def previous_blocking_review(self, source, project, task_key, read):
        if source.project_key != project or source.task_key != task_key or source.agent != "codex":
            raise ArchitectureGraphError("Implementation does not belong to this task")
        if not source.repair_review_key:
            return None
        earlier = read(source.repair_review_key)
        if not isinstance(earlier, Execution):
            raise ArchitectureGraphError("The previous review is unavailable")
        result, _ = self.repair_target(earlier, project, task_key)
        return {"execution_key": earlier.key, "findings": result["summary"]}

    def _usage(self, database, project, task_key):
        return next(
            iter(
                database.aql.execute(
                    """
            FOR job IN @@nodes
                FILTER job.entity_type == "Execution" AND job.task_key == @task
                FILTER job.project_key == @project
                LET attempts = (FOR attempt IN (IS_ARRAY(job.attempts) ? job.attempts : [])
                    FILTER attempt.ended_at != null RETURN attempt)
                LET tokens = SUM(
                    FOR attempt IN attempts FILTER attempt.usage != null
                    RETURN attempt.usage.input_tokens + attempt.usage.output_tokens
                )
                LET missing = LENGTH(FOR attempt IN attempts FILTER attempt.usage == null RETURN 1)
                LET legacy = LENGTH(attempts) == 0 AND LENGTH(job.results) > 0 ? 1 : 0
                COLLECT AGGREGATE total = SUM(tokens), unknown = SUM(missing + legacy)
                RETURN {tokens: total, unknown}
            """,
                    bind_vars={
                        "@nodes": self.graph._entities,
                        "project": project,
                        "task": task_key,
                    },
                )
            ),
            {"tokens": 0, "unknown": 0},
        )

    def task_token_total(self, project, task_key):
        return self._usage(self.graph._database(), project, task_key)["tokens"]

    @staticmethod
    def suggested_repair_contract(source, findings):
        existing = source.repair_contract
        criteria = [
            item.strip()[:1000]
            for item in re.split(r"(?:^|\n|(?<=\.))\s*\d+[.)]\s+", findings)
            if item.strip()
        ][:8]
        test_paths = list(existing.test_paths) if existing else []
        test_path = TypeAdapter(TestPath)
        for result in source.results:
            for path in result.get("files_modified", []):
                try:
                    path = test_path.validate_python(path)
                except ValidationError:
                    continue
                if path not in test_paths:
                    test_paths.append(path)
        return {"acceptance_criteria": criteria, "test_paths": test_paths[:8]}

    def repair_draft(self, project, task_key, review_key):
        self.task(project, task_key)
        review = self.graph.read_entity(review_key)
        if not isinstance(review, Execution):
            raise ArchitectureGraphError("Review does not belong to this task")
        result, target = self.repair_target(review, project, task_key)
        source = self.graph.read_entity(target["execution_id"])
        if not isinstance(source, Execution):
            raise ArchitectureGraphError("The reviewed implementation is unavailable")
        earlier = self.previous_blocking_review(source, project, task_key, self.graph.read_entity)
        return {
            "needs_design_review": earlier is not None,
            "usage": self._usage(self.graph._database(), project, task_key),
            "previous_contract": source.repair_contract,
            "suggested_contract": self.suggested_repair_contract(source, result["summary"]),
            "suggested_direction": (
                "Adjust the approach to address the latest blockers. First add a regression "
                "case for each blocker, then make the smallest change that passes those checks."
            ),
            "previous_review": earlier,
            "agent": "codex",
            "model": "Codex CLI default",
            "reason": "Codex can resume this exact local patch in an isolated clone.",
            "patch_execution_key": target["execution_id"],
            "patch_sha256": target["patch_sha256"],
            "prompt": (
                "Address the blocking review findings below in the staged implementation. "
                "Preserve unrelated changes and run focused regression tests. "
                "Report how each finding was addressed.\n\n" + result["summary"]
            )[:8000],
        }

    def apply_approved(self, project, key, review_key, actor) -> Execution:
        task = self.task(project, key)
        now = datetime.now(UTC)
        with self.graph._transaction() as tx:
            nodes = tx.collection(self.graph._entities)
            task = self._task_in_transaction(tx, project, task)
            review = Execution.model_validate(
                self.graph._require_entity_type(nodes, review_key, "Execution")
            )
            result = review.results[-1] if review.results else {}
            target = result.get("reviewed_target") or {}
            if (
                (review.project_key, review.task_key, review.agent) != (project, key, "codex")
                or review.state not in {"REVIEW", "COMPLETED"}
                or result.get("review_verdict") != "passed"
                or target.get("kind") != "implementation patch"
                or not all(
                    target.get(field) for field in ("execution_id", "base_commit", "patch_sha256")
                )
                or not review.repair_contract
                or not (result.get("verification") or {}).get("passed")
            ):
                raise ArchitectureGraphError(
                    "A passing exact-patch review with required tests is needed"
                )
            source_key = target["execution_id"]
            source = Execution.model_validate(
                self.graph._require_entity_type(nodes, source_key, "Execution")
            )
            if (source.project_key, source.task_key, source.agent) != (
                project,
                key,
                "codex",
            ) or not any(
                artifact.get("name") == "Local implementation patch"
                and artifact.get("sha256") == target.get("patch_sha256")
                for entry in source.results
                for artifact in entry.get("artifacts", [])
            ):
                raise ArchitectureGraphError("The reviewed patch is unavailable for this task")
            execution_key = architecture_key("approved-patch", project, key, source_key)
            existing = nodes.get(execution_key)
            if existing:
                return Execution.model_validate(existing)
            if task.metadata.get("active_execution") or task.status == "cancelled":
                raise ArchitectureGraphError(
                    "The task already has an active execution or is cancelled"
                )
            newer = tx.aql.execute(
                """
                FOR job IN @@nodes
                    FILTER job.entity_type == "Execution" AND job.project_key == @project
                    FILTER job.task_key == @task AND job.patch_execution_key == @source
                    FILTER job._key != @review
                    FOR entry IN job.results
                        FILTER (job.repair_review_key != null
                            AND "Local implementation patch" IN entry.artifacts[*].name)
                            OR (job.created_at >= @created
                            AND entry.reviewed_target.kind == "implementation patch"
                            AND entry.review_verdict != null)
                        LIMIT 1 RETURN true
                """,
                bind_vars={
                    "@nodes": self.graph._entities,
                    "project": project,
                    "task": key,
                    "source": source_key,
                    "review": review.key,
                    "created": review.model_dump(mode="json")["created_at"],
                },
            )
            if next(iter(newer), False):
                raise ArchitectureGraphError("A newer patch or review superseded this approval")
            job = Execution(
                key=execution_key,
                name=f"{task.name} approved patch"[:500],
                project_key=project,
                task_key=key,
                agent="codex",
                mode="APPLY_APPROVED",
                patch_execution_key=source_key,
                approved_review_key=review.key,
                approved_patch=target,
                repair_contract=review.repair_contract,
                requested_by=actor,
                created_at=now,
                updated_at=now,
                timeline=[{"state": "READY", "timestamp": now.isoformat(), "actor": actor}],
            )
            self.put(tx, job)
            self.put(
                tx,
                task.model_copy(
                    update={
                        "metadata": {**task.metadata, "active_execution": job.key},
                    }
                ),
            )
            self.link(tx, project, job.key, "defines", now)
            self.link(tx, task.key, job.key, "produced", now)
            self.link(tx, job.key, review.key, "references", now)
            self.link(tx, job.key, source.key, "references", now)
        return job

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
                if (
                    execution.agent,
                    execution.mode,
                    execution.requested_by,
                    execution.patch_execution_key if not execution.repair_review_key else None,
                    execution.repair_review_key,
                    execution.repair_prompt,
                    execution.repair_direction,
                    execution.repair_contract if execution.repair_review_key else None,
                    execution.additional_review_tokens,
                    execution.approve_review_attempt,
                ) != (
                    command.agent,
                    command.mode,
                    actor,
                    command.patch_execution_key,
                    command.repair_review_key,
                    command.repair_prompt,
                    command.repair_direction,
                    command.repair_contract,
                    command.additional_review_tokens,
                    command.approve_review_attempt,
                ):
                    raise ArchitectureGraphError("Execution request ID was already used")
                return execution
            task = self._task_in_transaction(tx, project, task)
            if (
                task.metadata.get("active_execution")
                or task.status == "cancelled"
                or (
                    task.status == "done"
                    and not (command.patch_execution_key or command.repair_review_key)
                )
            ):
                raise ArchitectureGraphError("Task must be open and have no active execution")
            if (command.additional_review_tokens or command.approve_review_attempt) and (
                not command.patch_execution_key or command.mode != "REVIEW"
            ):
                raise ArchitectureGraphError("Extra review tokens apply only to exact patch review")
            source_key = command.patch_execution_key
            patch_hash = None
            contract = command.repair_contract
            ceiling = None
            if command.repair_review_key:
                if source_key or command.mode != "IMPLEMENT" or command.agent != "codex":
                    raise ArchitectureGraphError("Repair requires the Codex Implement workflow")
                if not command.repair_prompt:
                    raise ArchitectureGraphError("A repair prompt is required")
                review = Execution.model_validate(
                    self.graph._require_entity_type(
                        nodes,
                        command.repair_review_key,
                        "Execution",
                    )
                )
                _, target = self.repair_target(review, project, key)
                source_key, patch_hash = target["execution_id"], target["patch_sha256"]
            elif command.repair_prompt or command.repair_direction or contract:
                raise ArchitectureGraphError("A repair prompt requires a blocking review")
            if source_key:
                if not command.repair_review_key and (
                    command.mode != "REVIEW" or command.agent != "codex"
                ):
                    raise ArchitectureGraphError("Patch review requires the Codex Review workflow")
                source = Execution.model_validate(
                    self.graph._require_entity_type(
                        nodes,
                        source_key,
                        "Execution",
                    )
                )
                if (
                    source.project_key != project
                    or source.task_key != key
                    or source.agent != "codex"
                    or not any(
                        artifact.get("name") == "Local implementation patch"
                        for result in source.results
                        for artifact in result.get("artifacts", [])
                    )
                ):
                    raise ArchitectureGraphError("Execution has no reviewable patch for this task")
                if command.repair_review_key:

                    def read_in_transaction(execution_key):
                        row = nodes.get(execution_key)
                        if row and row["entity_type"] == "Execution":
                            return Execution.model_validate(row)
                        return None

                    earlier = self.previous_blocking_review(
                        source, project, key, read_in_transaction
                    )
                    if earlier and not command.repair_direction:
                        raise ArchitectureGraphError(
                            "This revised patch is blocked again. Record design direction first."
                        )
                    if not contract:
                        raise ArchitectureGraphError(
                            "Confirm the acceptance criteria and regression checks first."
                        )
                    if (
                        earlier
                        and source.repair_contract
                        and (
                            source.repair_contract.acceptance_criteria
                            == contract.acceptance_criteria
                            and source.repair_contract.test_paths == contract.test_paths
                        )
                    ):
                        raise ArchitectureGraphError(
                            "Revise the acceptance criteria or test selection before continuing."
                        )
                    if contract.additional_tokens is not None:
                        ceiling = (
                            self._usage(tx, project, key)["tokens"] + contract.additional_tokens
                        )
                    superseded = tx.aql.execute(
                        """
                        FOR job IN @@nodes
                            FILTER job.entity_type == "Execution" AND job.task_key == @task
                            FILTER job.project_key == @project
                            FILTER job.patch_execution_key == @source
                            FILTER job.repair_review_key != null
                            FOR result IN job.results
                                FOR artifact IN result.artifacts
                                    FILTER artifact.name == "Local implementation patch"
                                    LIMIT 1 RETURN true
                        """,
                        bind_vars={
                            "@nodes": self.graph._entities,
                            "task": key,
                            "project": project,
                            "source": source_key,
                        },
                    )
                    if next(iter(superseded), False):
                        raise ArchitectureGraphError("A newer patch has superseded this review")
                elif source.repair_contract:
                    implementation = next(
                        (value for value in reversed(source.results) if value.get("verification")),
                        {},
                    )
                    if not implementation.get("verification", {}).get("passed"):
                        raise ArchitectureGraphError("Required tests must pass before model review")
                    contract = source.repair_contract
                    ceiling = source.repair_token_ceiling
                    used = self._usage(tx, project, key)["tokens"]
                    if command.approve_review_attempt:
                        ceiling = None
                    elif command.additional_review_tokens or command.approve_review_attempt:
                        ceiling = max(used, ceiling or 0) + command.additional_review_tokens
                    if ceiling is not None and used >= ceiling:
                        raise ArchitectureGraphError(
                            "The approved repair token budget is exhausted"
                        )
                elif command.additional_review_tokens:
                    raise ArchitectureGraphError("This patch has no repair token budget to extend")
            job = Execution(
                key=execution_key,
                name=f"{task.name} execution"[:500],
                project_key=project,
                task_key=key,
                patch_execution_key=source_key,
                repair_review_key=command.repair_review_key,
                repair_patch_sha256=patch_hash,
                repair_prompt=command.repair_prompt,
                repair_direction=command.repair_direction,
                repair_contract=contract,
                repair_token_ceiling=ceiling,
                additional_review_tokens=command.additional_review_tokens,
                approve_review_attempt=command.approve_review_attempt,
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
                        "status": task.status if command.patch_execution_key else "in_progress",
                        "metadata": {**task.metadata, "active_execution": job.key},
                    }
                ),
            )
            self.link(tx, project, job.key, "defines", now)
            if job.repair_review_key:
                self.link(tx, job.key, job.repair_review_key, "references", now)
            if job.patch_execution_key:
                self.link(tx, job.key, job.patch_execution_key, "references", now)
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

    def retry_execution(self, project, task_key, execution_key, actor):
        task = self.task(project, task_key)
        now = datetime.now(UTC)
        with self.graph._transaction() as tx:
            task = self._task_in_transaction(tx, project, task)
            row = self.graph._require_entity_type(
                tx.collection(self.graph._entities),
                execution_key,
                "Execution",
            )
            job = Execution.model_validate(row)
            if job.project_key != project or job.task_key != task_key:
                raise ArchitectureGraphError("Execution does not belong to this task")
            if (
                job.repair_token_ceiling is not None
                and self._usage(tx, project, task_key)["tokens"] >= job.repair_token_ceiling
            ):
                raise ArchitectureGraphError("The approved repair token budget is exhausted")
            active = task.metadata.get("active_execution")
            if (
                job.state not in {"BLOCKED", "FAILED"}
                or active
                and active != job.key
                or task.status == "cancelled"
                or (task.status == "done" and not job.approved_review_key)
            ):
                raise ArchitectureGraphError(
                    "This execution cannot be retried in its current state"
                )
            self.put(
                tx,
                task.model_copy(
                    update={
                        "status": task.status if job.approved_review_key else "in_progress",
                        "metadata": {**task.metadata, "active_execution": job.key},
                    }
                ),
            )
            updated = job.model_copy(
                update={
                    "state": "READY",
                    "failure": None,
                    "lease_until": None,
                    "updated_at": now,
                    "revision": job.revision + 1,
                    "timeline": [
                        *job.timeline,
                        {
                            "state": "READY",
                            "timestamp": now.isoformat(),
                            "phase": job.phase,
                            "actor": actor,
                            "action": "retry",
                        },
                    ][-100:],
                }
            )
            self.put(tx, updated)
            return updated

    def transition(
        self,
        job,
        state,
        *,
        context_keys=None,
        result: AgentResult | None = None,
        failure=None,
        usage: ExecutionUsage | None = None,
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
            attempts = list(prior.attempts)
            if state == "RUNNING" and context_keys is None:
                if attempts and attempts[-1].state == "running":
                    attempts[-1] = attempts[-1].model_copy(
                        update={
                            "state": "interrupted",
                            "ended_at": now,
                        }
                    )
                attempts.append(ExecutionAttempt(phase=prior.phase, started_at=now))
            elif state != "RUNNING" and attempts and attempts[-1].state == "running":
                attempt_state = (
                    "succeeded"
                    if result
                    else ("interrupted" if state.startswith("CANCEL") else "failed")
                )
                attempts[-1] = attempts[-1].model_copy(
                    update={
                        "state": attempt_state,
                        "ended_at": now,
                        "failure": failure,
                        "usage": result.usage if result else usage,
                    }
                )
            updates["attempts"] = attempts
            if context_keys is not None:
                updates["context_keys"] = context_keys
                for key in context_keys:
                    if nodes.get(key):
                        self.link(tx, job.key, key, "references", now)
            if result:
                if (
                    result.generated_contract
                    and prior.agent == "codex"
                    and not prior.repair_contract
                ):
                    updates["repair_contract"] = result.generated_contract
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
