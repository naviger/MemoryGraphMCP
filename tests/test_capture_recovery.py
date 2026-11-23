from datetime import UTC, datetime, timedelta
from functools import partial

import httpx
import pytest

from app.config.settings import Settings
from app.domain.capture_checkpoint import CaptureHistoryInput
from app.repositories.arango_architecture import ArangoArchitectureRepository
from app.services.capture_worker import CaptureWorker
from app.services.knowledge_capture import KnowledgeCaptureService

STAMP = datetime(2026, 2, 1, tzinfo=UTC)


def record(number):
    return {
        "sha": format(number, "040x"),
        "commit": {
            "committer": {"date": (STAMP + timedelta(minutes=number)).isoformat()},
            "message": f"Change {number}",
        },
        "html_url": "https://example.test/commit",
    }


@pytest.mark.anyio
async def test_recovery_restarts_after_partial_store_without_duplicate():
    now = STAMP + timedelta(days=1)
    source_head = 1
    fail = None
    written = set()
    attempts = {}

    class Store:
        checkpoint = None

        def read_capture_checkpoint(self, project, scope, stream):
            from app.domain.capture_checkpoint import CaptureCheckpoint

            return self.checkpoint or CaptureCheckpoint(stream=stream)

        def save_capture_checkpoint(self, project, scope, checkpoint):
            self.checkpoint = checkpoint.model_copy(update={"revision": checkpoint.revision + 1})
            return self.checkpoint

        def read_entity(self, key):
            return key in written

    def git(request):
        path = request.url.path
        if path.endswith("/commits"):
            if request.url.params.get("since") and request.url.params["per_page"] == "100":
                page = int(request.url.params["page"])
                numbers = list(range(source_head, 0, -1))[(page - 1) * 100 : page * 100]
                return httpx.Response(200, json=[record(number) for number in numbers])
            return httpx.Response(200, json=[record(source_head)])
        if "/compare/" in path:
            page = int(request.url.params["page"])
            segment = list(range(2, source_head + 1))[(page - 1) * 100 : page * 100]
            return httpx.Response(
                200,
                json={
                    "status": "ahead",
                    "total_commits": source_head - 1,
                    "commits": [record(number) for number in segment],
                },
            )
        sha = path.rsplit("/", 1)[1]
        attempts[sha] = attempts.get(sha, 0) + 1
        return (
            httpx.Response(503) if int(sha, 16) == fail else httpx.Response(200, json={"files": []})
        )

    graph = Store()

    class Capture:
        def __init__(self):
            self.graph = graph

        def change(self, project, source, event_id, stamp, diff, **kwargs):
            from app.domain.architecture import architecture_key

            key = architecture_key("capture", project, source, event_id)
            assert key not in written
            written.add(key)

    settings = Settings(
        _env_file=None,
        auth_required=False,
        capture_project_key="alpha",
        capture_repository_key="repo",
        capture_github_repository="example/test",
    )
    worker = CaptureWorker(settings, Capture())
    async with httpx.AsyncClient(
        base_url="https://example.test/", transport=httpx.MockTransport(git)
    ) as client:
        await worker.poll_stream("commits", partial(worker.scan_github, client))
        assert graph.checkpoint.cursor == record(1)["sha"]

        source_head, fail = 123, 111
        await worker.poll_stream("commits", partial(worker.scan_github, client))
        assert graph.checkpoint.cursor == record(1)["sha"]
        assert graph.checkpoint.observed_head == record(123)["sha"]
        assert graph.checkpoint.failure == "GitHub HTTP 503"
        assert len(written) == 110
        graph.checkpoint.retry_at = now
        fail = None

        restarted = CaptureWorker(settings, Capture())
        await restarted.poll_stream("commits", partial(restarted.scan_github, client))
        assert graph.checkpoint.cursor == record(123)["sha"]
        assert graph.checkpoint.failure is None
        assert graph.checkpoint.last_failure is not None
        assert len(written) == 123
        assert attempts[record(110)["sha"]] == 1
        assert attempts[record(111)["sha"]] == 2

        restarted.request_history(
            CaptureHistoryInput(stream="commits", action="replay", since=STAMP), "operator"
        )
        await restarted.poll_stream("commits", partial(restarted.scan_github, client))
        assert graph.checkpoint.cursor == record(123)["sha"]
        assert graph.checkpoint.last_history_action == "replay"
        assert len(written) == 123


def test_arango_recovery_and_workspace():
    """Opt-in integration check. Use an empty, disposable database."""
    import os
    from uuid import uuid4

    from app.domain.architecture import ArchitectureLink, Project, Repository, Service
    from app.domain.exceptions import ArchitectureGraphError
    from app.domain.project_workspace import (
        AgentArtifact,
        AgentResult,
        StartExecution,
        TaskUpdate,
        WorkDraft,
    )
    from app.repositories.factory import build_repository
    from app.repositories.project_workspace import ProjectWorkspaceRepository

    host = os.getenv("MGMCP_RECOVERY_TEST_HOST")
    if not host:
        pytest.skip("Requires a disposable ArangoDB")
    settings = Settings(
        _env_file=None,
        auth_required=False,
        ARANGO_HOST=host,
        ARANGO_DATABASE="_system",
        ARANGO_USERNAME="root",
        ARANGO_PASSWORD=os.environ["MGMCP_RECOVERY_TEST_PASSWORD"],
        capture_project_key="alpha",
        capture_repository_key="repo",
        capture_github_repository="example/test",
        embedding_provider="deterministic",
    )
    memory = build_repository(settings)
    assert isinstance(memory.architecture, ArangoArchitectureRepository)
    graph = memory.architecture
    graph.ensure_storage_ready()
    capture = KnowledgeCaptureService(graph, memory)
    workspace = ProjectWorkspaceRepository(graph)
    for node in (
        Project(key="alpha", name="Alpha"),
        Project(key="beta", name="Beta"),
        Repository(key="repo", name="Example", role="source"),
        Service(key="service", name="App"),
    ):
        graph.put_entity(node)
    for key in ("repo", "service"):
        graph.put_link(
            ArchitectureLink(
                source_key="alpha", target_key=key, relationship_type="defines", source="fixture"
            )
        )
    assert graph.capture_journal_lag("alpha") == (0, None)

    class GitSource:
        head = 1
        fail = False

        def serve(self, request):
            path = request.url.path
            if path.endswith("/commits"):
                return httpx.Response(200, json=[record(self.head)])
            if "/compare/" in path:
                return httpx.Response(
                    200,
                    json={
                        "status": "ahead",
                        "total_commits": 2,
                        "commits": [record(2), record(3)],
                    },
                )
            if self.fail and path.endswith(record(3)["sha"]):
                return httpx.Response(503)
            return httpx.Response(200, json={"files": []})

    github = GitSource()

    async def poll(worker):
        async with httpx.AsyncClient(
            base_url="https://example.test/", transport=httpx.MockTransport(github.serve)
        ) as client:
            await worker.poll_stream("commits", partial(worker.scan_github, client))

    import anyio

    worker = CaptureWorker(settings, capture)
    anyio.run(poll, worker)
    github.head, github.fail = 3, True
    anyio.run(poll, worker)
    assert worker.checkpoint("commits").cursor == record(1)["sha"]
    assert worker.checkpoint("commits").failure == "GitHub HTTP 503"
    assert (
        len([node for node in workspace.entities("alpha") if node.entity_type == "Observation"])
        == 2
    )
    restarted = CaptureWorker(settings, capture)
    restarted.save_checkpoint(restarted.checkpoint("commits").model_copy(update={"retry_at": None}))
    github.fail = False
    anyio.run(poll, restarted)
    anyio.run(poll, restarted)
    assert restarted.checkpoint("commits").cursor == record(3)["sha"]
    assert (
        len([node for node in workspace.entities("alpha") if node.entity_type == "Observation"])
        == 3
    )

    task = workspace.create(
        "alpha",
        WorkDraft(kind="Task", name="Example task", description="Implement example"),
        "writer",
    )
    with pytest.raises(ArchitectureGraphError):
        workspace.start(
            "beta",
            task.key,
            StartExecution(request_id=uuid4().hex, agent="codex", mode="IMPLEMENT"),
            "writer",
        )
    command = StartExecution(request_id=uuid4().hex, agent="codex", mode="IMPLEMENT")
    job = workspace.start("alpha", task.key, command, "writer")
    assert workspace.start("alpha", task.key, command, "writer").key == job.key
    assert any(queued.key == job.key for queued in workspace.ready())
    claimed = workspace.transition(job, "RUNNING", context_keys=["service"])
    assert workspace.transition(job, "RUNNING") is None
    result = AgentResult(
        summary="Implemented",
        lesson="Retain coverage",
        tests=["pytest passed"],
        artifacts=[AgentArtifact(name="Patch", reference="commit:example")],
        completed=True,
    )
    assert workspace.transition(claimed, "COMPLETED", result=result).state == "COMPLETED"
    assert workspace.transition(claimed, "COMPLETED", result=result) is None
    assert graph.read_entity(task.key).status == "done"
    assert graph.read_entity(task.key).metadata["latest_execution"] == job.key
    entities = workspace.entities("alpha")
    assert len([node for node in entities if node.entity_type == "Artifact"]) == 1
    assert len([node for node in entities if node.entity_type == "Observation"]) == 4
    assert workspace.entities("beta") == []
    with pytest.raises(ArchitectureGraphError):
        workspace.task_update("alpha", task.key, TaskUpdate(status="done"), "writer")

    from app.services.task_execution import TaskExecutor

    class FakeAgent:
        phases = []

        async def perform(self, phase, execution_key, prompt):
            self.phases.append((phase, execution_key))
            assert "Implement the next example" in prompt
            assert "App" in prompt
            return AgentResult(summary=f"Finished {phase}", completed=phase != "plan")

    executor = TaskExecutor(settings, workspace, memory)
    agent = FakeAgent()
    executor.providers["codex"] = agent
    second = workspace.create(
        "alpha",
        WorkDraft(kind="Task", name="Next task", description="Implement the next example"),
        "writer",
    )
    graph.put_link(
        ArchitectureLink(
            source_key=second.key,
            target_key="service",
            relationship_type="references",
            source="fixture",
        )
    )
    job = workspace.start(
        "alpha",
        second.key,
        StartExecution(
            request_id=uuid4().hex,
            agent="codex",
            mode="FULL_LIFECYCLE",
        ),
        "writer",
    )
    for phase in range(3):
        pending = next(item for item in workspace.ready() if item.key == job.key)
        assert pending.phase == phase
        anyio.run(executor.process, pending)
    saved = workspace.executions("alpha", second.key)[0]
    assert saved.state == "COMPLETED", saved.failure
    assert agent.phases == [(name, job.key) for name in ("plan", "execute", "review")]
    assert saved.metadata["memories_created"] == 1
    assert "service" in saved.context_keys

    from app.domain.execution_usage import ExecutionUsage

    third = workspace.create(
        "alpha",
        WorkDraft(kind="Task", name="Retry fixture", description="Record retry usage"),
        "writer",
    )
    retry_job = workspace.start(
        "alpha",
        third.key,
        StartExecution(
            request_id=uuid4().hex,
            agent="codex",
            mode="PLAN_ONLY",
        ),
        "writer",
    )
    running = workspace.transition(retry_job, "RUNNING")
    usage = ExecutionUsage(input_tokens=300, cached_input_tokens=20, output_tokens=150)
    blocked = workspace.transition(running, "BLOCKED", failure="Phase timed out", usage=usage)
    assert blocked.lease_until is not None
    assert blocked.attempts[0].state == "failed"
    assert blocked.attempts[0].usage == usage
    with pytest.raises(ArchitectureGraphError):
        workspace.retry_execution("beta", third.key, retry_job.key, "writer")
    queued = workspace.retry_execution("alpha", third.key, retry_job.key, "writer")
    assert queued.key == retry_job.key and queued.phase == blocked.phase
    assert queued.lease_until is None and queued.timeline[-1]["action"] == "retry"
    with pytest.raises(ArchitectureGraphError):
        workspace.retry_execution("alpha", third.key, retry_job.key, "writer")
    running_again = workspace.transition(queued, "RUNNING")
    failure = workspace.transition(running_again, "FAILED", failure="Adapter rejected operation")
    assert failure.attempts[1].usage is None
    assert workspace.retry_execution("alpha", third.key, retry_job.key, "writer").state == "READY"

    repair_task = workspace.create(
        "alpha",
        WorkDraft(
            kind="Task",
            name="Repair fixture",
            description="Revise the reviewed patch",
        ),
        "writer",
    )
    original = workspace.start(
        "alpha",
        repair_task.key,
        StartExecution(
            request_id=uuid4().hex,
            agent="codex",
            mode="IMPLEMENT",
        ),
        "writer",
    )
    executing = workspace.transition(original, "RUNNING")
    workspace.transition(
        executing,
        "REVIEW",
        result=AgentResult(
            summary="Saved patch",
            artifacts=[
                AgentArtifact(
                    name="Local implementation patch",
                    reference="local patch",
                )
            ],
        ),
    )
    review = workspace.start(
        "alpha",
        repair_task.key,
        StartExecution(
            request_id=uuid4().hex,
            agent="codex",
            mode="REVIEW",
            patch_execution_key=original.key,
        ),
        "writer",
    )
    reviewing = workspace.transition(review, "RUNNING")
    workspace.transition(
        reviewing,
        "REVIEW",
        result=AgentResult(
            summary="Validate the source before import.",
            review_verdict="blocking",
            reviewed_target={
                "kind": "implementation patch",
                "execution_id": original.key,
                "patch_sha256": "a" * 64,
            },
        ),
    )
    draft = workspace.repair_draft("alpha", repair_task.key, review.key)
    assert "Validate the source" in draft["prompt"]
    with pytest.raises(ArchitectureGraphError):
        workspace.repair_draft("beta", repair_task.key, review.key)
    from app.domain.repair_contract import RepairContract

    request = StartExecution(
        request_id=uuid4().hex,
        agent="codex",
        mode="IMPLEMENT",
        repair_review_key=review.key,
        repair_prompt="Edited repair prompt",
        repair_contract=RepairContract(
            acceptance_criteria=["Preserve source identity"],
            test_paths=["tests/test_arango_repository.py"],
            additional_tokens=100,
        ),
    )
    repair = workspace.start("alpha", repair_task.key, request, "writer")
    assert repair.patch_execution_key == original.key and repair.repair_patch_sha256 == "a" * 64
    assert repair.repair_prompt == "Edited repair prompt"
    assert repair.repair_token_ceiling == 100
    assert workspace.start("alpha", repair_task.key, request, "writer").key == repair.key

    repairing = workspace.transition(repair, "RUNNING")
    workspace.transition(
        repairing,
        "REVIEW",
        result=AgentResult(
            summary="Saved new patch",
            usage=ExecutionUsage(input_tokens=90, output_tokens=10),
            verification={"passed": True},
            artifacts=[
                AgentArtifact(
                    name="Local implementation patch",
                    reference="new patch",
                )
            ],
        ),
    )
    with pytest.raises(ArchitectureGraphError, match="superseded"):
        workspace.start(
            "alpha",
            repair_task.key,
            request.model_copy(
                update={
                    "request_id": uuid4().hex,
                }
            ),
            "writer",
        )
    review_request = StartExecution(
        request_id=uuid4().hex,
        agent="codex",
        mode="REVIEW",
        patch_execution_key=repair.key,
    )
    with pytest.raises(ArchitectureGraphError, match="budget is exhausted"):
        workspace.start("alpha", repair_task.key, review_request, "writer")
    second_review = workspace.start(
        "alpha",
        repair_task.key,
        review_request.model_copy(update={"additional_review_tokens": 50}),
        "writer",
    )
    assert second_review.repair_token_ceiling == 150
    examining = workspace.transition(second_review, "RUNNING")
    workspace.transition(
        examining,
        "REVIEW",
        result=AgentResult(
            summary="Still needs a scope decision.",
            review_verdict="blocking",
            reviewed_target={
                "kind": "implementation patch",
                "execution_id": repair.key,
                "patch_sha256": "b" * 64,
            },
        ),
    )
    escalated = workspace.repair_draft("alpha", repair_task.key, second_review.key)
    assert escalated["needs_design_review"]
    assert escalated["previous_review"]["execution_key"] == review.key
    next_request = request.model_copy(
        update={
            "request_id": uuid4().hex,
            "repair_review_key": second_review.key,
        }
    )
    with pytest.raises(ArchitectureGraphError, match="design direction"):
        workspace.start("alpha", repair_task.key, next_request, "writer")
    with pytest.raises(ArchitectureGraphError, match="criteria or test"):
        workspace.start(
            "alpha",
            repair_task.key,
            next_request.model_copy(
                update={
                    "repair_direction": "Retry against the same checks.",
                }
            ),
            "writer",
        )
    directed = workspace.start(
        "alpha",
        repair_task.key,
        next_request.model_copy(
            update={
                "repair_direction": "Keep legacy resolution behavior covered by end-to-end tests.",
                "repair_contract": request.repair_contract.model_copy(
                    update={
                        "acceptance_criteria": [
                            "Preserve legacy resolution and repository ownership"
                        ],
                    }
                ),
            }
        ),
        "writer",
    )
    assert directed.repair_direction.startswith("Keep legacy")


def test_workspace_routes_require_project_and_writer_grants():
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from app.api.project_workspace import build_workspace_router
    from app.auth.project_access import ProjectAccess, ProjectAccessDenied, current_project_access

    app = FastAPI()

    @app.exception_handler(ProjectAccessDenied)
    async def denied(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=403, content={"detail": "project grant required"})

    @app.middleware("http")
    async def claims(request: Request, call_next):
        role = request.headers["role"]
        request.state.auth_claims = {"subject": role, "raw": {"roles": [role]}}
        key = current_project_access.set(
            ProjectAccess(grants={"alpha": "write" if role == "writer" else "read"})
        )
        try:
            return await call_next(request)
        finally:
            current_project_access.reset(key)

    workspace = SimpleNamespace(
        entities=lambda project: [], create=lambda project, data, actor: data
    )

    async def refresh():
        return None

    executor = SimpleNamespace(modes=lambda: {}, local=SimpleNamespace(url=None), refresh=refresh)
    app.include_router(build_workspace_router("/api/review", workspace, executor, None))
    api = TestClient(app)
    path = "/api/review/projects/alpha/workspace"
    data = {"kind": "Task", "name": "Test", "description": "Test task"}
    assert api.get(path, headers={"role": "reader"}).json()["can_write"] is False
    assert api.post(path, headers={"role": "reader"}, json=data).status_code == 403
    assert api.post(path, headers={"role": "writer"}, json=data).status_code == 200
    assert api.get(path.replace("alpha", "beta"), headers={"role": "writer"}).status_code == 403
