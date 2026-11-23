from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from app.config.settings import Settings
from app.domain.architecture import ArchitectureLink, Project, Task
from app.domain.architecture_events import TaskUpserted
from app.mcp.server import build_mcp_server
from app.repositories.arango_repository import ArangoMemoryRepository, ArangoRepositoryConfig
from app.services.architecture_service import ArchitectureService
from app.services.knowledge_base import StaticKnowledgeBase
from app.services.search_service import MemorySearchService


class FakeCollection:
    def __init__(self) -> None:
        self.inserted: list[dict[str, object]] = []
        self.documents_by_key: dict[str, dict[str, object]] = {}

    def insert(self, document: dict[str, object], return_new: bool = False, **kwargs):
        self.inserted.append(document)
        key = document.get("_key", f"generated-{len(self.inserted)}")
        stored = {**document, "_key": key}
        self.documents_by_key[str(stored["_key"])] = stored
        return {"new": stored} if return_new else stored

    def update(self, document, return_new=False, **kwargs):
        stored = self.documents_by_key[document["_key"]]
        stored.update(document)
        return {"new": stored} if return_new else stored

    def find(self, filters):
        return (
            document for document in self.documents_by_key.values()
            if all(document.get(key) == value for key, value in filters.items())
        )

    def delete(self, key):
        del self.documents_by_key[key]

    def has(self, document_key: str) -> bool:
        return document_key in self.documents_by_key

    def get(self, document_key: str):
        return self.documents_by_key.get(document_key)


class FakeDatabase:
    def __init__(self) -> None:
        self.collections = {
            "memory_documents": FakeCollection(),
            "memory_edges": FakeCollection(),
            "memory_vectors": FakeCollection(),
        }
        self.views = {"memory_search": True, "memory_vectors_view": True}
        self.created_collections: list[tuple[str, bool]] = []
        self.created_views: list[tuple[str, dict[str, object]]] = []
        self.properties_count = 0

    def begin_transaction(self, *, exclusive, read=None):
        database = self

        class Transaction:
            collections = deepcopy(database.collections)

            @property
            def aql(self):
                return database.aql

            def collection(self, name):
                assert name in exclusive
                return self.collections[name]

            def commit_transaction(self):
                database.collections = self.collections

            def abort_transaction(self):
                pass

        return Transaction()

    def properties(self) -> dict[str, str]:
        self.properties_count += 1
        return {"name": "architecture"}

    def has_collection(self, name: str) -> bool:
        return name in self.collections

    def has_view(self, name: str) -> bool:
        return name in self.views

    def collection(self, name: str) -> FakeCollection:
        return self.collections[name]

    def create_collection(self, name: str, edge: bool = False):
        self.created_collections.append((name, edge))
        self.collections[name] = FakeCollection()
        return self.collections[name]

    def create_arangosearch_view(self, name: str, properties: dict[str, object]):
        self.created_views.append((name, properties))
        self.views[name] = True
        return {"name": name, "properties": properties}


@pytest.fixture()
def repository() -> ArangoMemoryRepository:
    config = ArangoRepositoryConfig(
        url="http://localhost:8530",
        database="architecture",
        username="architect",
        password="@rchitect",
        documents_collection="memory_documents",
        edges_collection="memory_edges",
        vectors_collection="memory_vectors",
        search_view="memory_search",
        vector_view="memory_vectors_view",
    )
    repository = object.__new__(ArangoMemoryRepository)
    repository._config = config
    repository.__dict__["database"] = FakeDatabase()
    return repository


async def test_observation_is_saved_with_required_links_and_stable_retry(repository):
    settings = Settings(_env_file=None, auth_required=False)
    graph = repository.architecture
    graph.ensure_storage_ready()
    server = build_mcp_server(
        settings, MemorySearchService(repository), StaticKnowledgeBase(),
        architecture_service=ArchitectureService(graph, repository),
    )
    draft = {
        "key": "agent-finding", "source": "commit:abc123",
        "summary": "The public endpoint delegates to the service.",
        "confidence": 0.8, "related_entity_keys": ["service", "endpoint"],
    }
    async with Client(server) as client:
        await client.call_tool("put_architecture_entity", {"entity": {
            "key": "service", "entity_type": "Service", "name": "Customer API",
        }})
        with pytest.raises(ToolError, match="entity 'endpoint' was not found"):
            await client.call_tool("add_observation", {"observation": draft})
        assert not repository.database.collection("architecture_entities").has("agent-finding")
        assert not repository.database.collection("architecture_edges").documents_by_key

        await client.call_tool("put_architecture_entity", {"entity": {
            "key": "endpoint", "entity_type": "ApiEndpoint",
            "name": "GET /customers", "method": "GET", "path": "/customers",
        }})
        initial = (await client.call_tool(
            "add_observation", {"observation": draft},
        )).structured_content
        retry = (await client.call_tool("add_observation", {"observation": {
            **draft, "related_entity_keys": ["endpoint", "service", "service"],
        }})).structured_content
        assert initial == retry
        assert initial["timestamp"].endswith("Z")
        assert initial["source"] == "commit:abc123"
        assert initial["summary"] == draft["summary"]
        assert initial["confidence"] == 0.8
        assert initial["related_entity_keys"] == ["endpoint", "service"]
        edges = repository.database.collection("architecture_edges").documents_by_key
        assert len(edges) == 2
        assert all(row["relationship_type"] == "related_to" for row in edges.values())


@pytest.mark.asyncio
async def test_project_goals_and_conversation_summaries_record_typed_links(repository):
    graph = repository.architecture
    graph.ensure_storage_ready()
    server = build_mcp_server(
        Settings(_env_file=None, auth_required=False),
        MemorySearchService(repository), StaticKnowledgeBase(),
        architecture_service=ArchitectureService(graph, repository),
    )
    goal = {
        "key": "security-goal", "name": "Consolidate identity", "project_key": "customer-api",
        "adr_keys": ["oidc-adr"], "service_keys": ["identity"], "task_keys": ["migrate"],
        "success_criteria": ["Every customer route uses the identity service"],
    }
    summary = {
        "key": "design-review", "source": "design-review:2026-11-21",
        "summary": "The team selected OIDC and found one migration risk.",
        "decision_keys": ["oidc-adr"], "risk_keys": ["compatibility"], "task_keys": ["migrate"],
    }
    async with Client(server) as client:
        for key, kind in [
            ("customer-api", "Project"), ("oidc-adr", "ADR"), ("identity", "Service"),
            ("migrate", "Task"), ("compatibility", "Risk"),
        ]:
            await client.call_tool("put_architecture_entity", {
                "entity": {"key": key, "entity_type": kind, "name": key},
            })
        bad_goal = {**goal, "service_keys": ["oidc-adr"]}
        with pytest.raises(ToolError, match="must exist as type Service"):
            await client.call_tool("save_project_goal", {"goal": bad_goal})
        assert not repository.database.collection("architecture_entities").has("security-goal")
        await client.call_tool("save_project_goal", {"goal": goal})
        await client.call_tool("link_architecture_entities", {"link": {
            "source_key": "migrate", "target_key": "security-goal",
            "relationship_type": "supports_goal", "source": "design review",
        }})
        updated = await client.call_tool("save_project_goal", {
            "goal": {**goal, "status": "achieved", "task_keys": []},
        })
        assert updated.structured_content["status"] == "achieved"
        assert updated.structured_content["task_keys"] == []
        with pytest.raises(ToolError):
            await client.call_tool("add_conversation_summary", {
                "conversation": {**summary, "messages": ["Full transcripts do not belong here"]},
            })
        first = await client.call_tool("add_conversation_summary", {"conversation": summary})
        retry = await client.call_tool("add_conversation_summary", {"conversation": summary})
        assert first.structured_content == retry.structured_content
        assert first.structured_content["summary"] == summary["summary"]

    edges = repository.database.collection("architecture_edges").documents_by_key.values()
    refs = {
        (row["source_key"], row["target_key"], row["relationship_type"], row["source"])
        for row in edges
    }
    assert refs == {
        ("customer-api", "security-goal", "defines", "project-goal:security-goal"),
        ("oidc-adr", "security-goal", "supports_goal", "project-goal:security-goal"),
        ("identity", "security-goal", "supports_goal", "project-goal:security-goal"),
        ("migrate", "security-goal", "supports_goal", "design review"),
        ("design-review", "oidc-adr", "produced", "conversation-summary:design-review"),
        ("design-review", "compatibility", "identified", "conversation-summary:design-review"),
        ("design-review", "migrate", "created", "conversation-summary:design-review"),
    }

async def test_architectural_constraint_writes_require_writer_and_preserve_cited_links(
    repository, monkeypatch,
):
    graph = repository.architecture
    graph.ensure_storage_ready()
    server = build_mcp_server(
        Settings(_env_file=None, auth_required=True, jwt_secret_key="test-key"),
        MemorySearchService(repository), StaticKnowledgeBase(),
        architecture_service=ArchitectureService(graph, repository),
    )
    claims = {"subject": "architect", "raw": {"roles": ["writer"]}}
    request = SimpleNamespace(scope={"state": {"auth_claims": claims}})
    monkeypatch.setattr("app.mcp.tools.auth.get_http_request", lambda: request)
    constraint = {
        "key": "oidc-rule", "name": "Authentication boundary",
        "rule": "Public requests must use OIDC",
        "scope": "Customer API in production", "authority": "ADR-042",
        "enforcement_status": "partially_enforced",
        "constrained_entity_keys": ["customer-service", "customer-service"],
        "test_suite_keys": ["oidc-tests"],
    }
    async with Client(server) as client:
        for key, kind in [
            ("oidc-rule", "Constraint"), ("customer-service", "Service"),
            ("oidc-tests", "TestSuite"), ("additional-tests", "TestSuite"),
        ]:
            await client.call_tool("put_architecture_entity", {"entity": {
                "key": key, "entity_type": kind, "name": key,
                "metadata": {"catalog": "engineering"},
            }})
        claims["raw"]["roles"] = ["reader"]
        with pytest.raises(ToolError, match="authenticated writer or admin"):
            await client.call_tool("save_architectural_constraint", {"constraint": constraint})
        claims["raw"]["roles"] = ["writer"]
        with pytest.raises(ToolError, match="must exist as type TestSuite"):
            await client.call_tool("save_architectural_constraint", {
                "constraint": {**constraint, "test_suite_keys": ["customer-service"]},
            })
        assert not repository.database.collection("architecture_edges").documents_by_key

        first = (await client.call_tool("save_architectural_constraint", {
            "constraint": constraint,
        })).structured_content
        assert first["rule"] == constraint["rule"]
        assert first["scope"] == constraint["scope"]
        assert first["authority"] == constraint["authority"]
        assert first["constrained_entity_keys"] == ["customer-service"]
        assert first["metadata"] == {"catalog": "engineering"}

        await client.call_tool("link_architecture_entities", {"link": {
            "source_key": "additional-tests", "target_key": "oidc-rule",
            "relationship_type": "validates", "source": "audit:42",
        }})
        updated = (await client.call_tool("save_architectural_constraint", {
            "constraint": {**constraint, "enforcement_status": "enforced", "test_suite_keys": []},
        })).structured_content
        assert updated["enforcement_status"] == "enforced"
        assert not updated["test_suite_keys"]

    edges = repository.database.collection("architecture_edges").documents_by_key.values()
    refs = {
        (row["source_key"], row["target_key"], row["relationship_type"], row["source"])
        for row in edges
    }
    assert refs == {
        ("customer-service", "oidc-rule", "constrained_by", "architectural-constraint:oidc-rule"),
        ("oidc-rule", "additional-tests", "validated_by", "audit:42"),
    }


@pytest.fixture
def tracker_graph(repository):
    graph = repository.architecture
    graph.ensure_storage_ready()
    events = "architecture_events"

    class TrackerQuery:
        def execute(self, query, bind_vars):
            assert 'row.outcome == "applied"' in query
            times = [
                row["occurred_at_us"]
                for row in repository.database.collection(events).documents_by_key.values()
                if row["source"] == bind_vars["source"]
                and row["project_key"] == bind_vars["project"]
                and row["task_key"] == bind_vars["task"]
                and row["outcome"] == "applied"
            ]
            return [max(times)] if times else []

    repository.database.aql = TrackerQuery()
    for key in ("owned", "other"):
        graph.put_entity(Project(key=key, name=key))
    return graph


async def test_tracker_events_are_scoped_replay_safe_and_ordered(
    repository, tracker_graph, monkeypatch,
):
    tracker_graph.put_entity(Task(key="foreign", name="Other project task"))
    tracker_graph.put_link(ArchitectureLink(
        source_key="other", target_key="foreign", relationship_type="defines", source="fixture",
    ))
    tracker_graph.put_entity(Task(key="unlinked", name="Unlinked task"))
    claims = {"subject": "tracker", "raw": {"roles": ["writer"]}}
    request = SimpleNamespace(scope={"state": {"auth_claims": claims}})
    monkeypatch.setattr("app.mcp.tools.auth.get_http_request", lambda: request)
    server = build_mcp_server(
        Settings(
            _env_file=None, auth_required=True, jwt_secret_key="test-key",
            graph_event_producer_subject="tracker", graph_event_project_key="owned",
        ),
        MemorySearchService(repository), StaticKnowledgeBase(),
        architecture_service=ArchitectureService(tracker_graph, repository),
    )
    event = {
        "event_id": "create", "event_type": "task.upserted", "task_key": "local",
        "project_key": "owned", "occurred_at": "2026-11-22T11:00:00Z", "name": "Scoped task",
    }
    async with Client(server) as client:
        async def send(**changes):
            return (await client.call_tool(
                "ingest_graph_event", {"event": {**event, **changes}},
            )).structured_content

        with pytest.raises(ToolError, match="configured tracker project"):
            await send(project_key="other")
        for task_key in ("foreign", "unlinked"):
            with pytest.raises(ToolError, match="not exclusively linked"):
                await send(task_key=task_key)
        claims["subject"] = "another-writer"
        with pytest.raises(ToolError, match="not the configured"):
            await send()
        claims["subject"] = "tracker"
        claims["raw"]["roles"] = ["reader"]
        with pytest.raises(ToolError, match="writer or admin"):
            await send()
        claims["raw"]["roles"] = ["writer"]
        assert not repository.database.collection("architecture_events").documents_by_key
        first = await send()
        assert first["affected_entity_keys"] == ["local", "owned"]
        assert first == await send()
        with pytest.raises(ToolError, match="different payload"):
            await send(name="Changed replay")
        receipt = await client.call_tool("get_graph_event", {"event_id": "create"})
        assert receipt.structured_content == first
        complete = {k: v for k, v in event.items() if k != "name"}
        complete.update(event_id="done", event_type="task.completed",
                        occurred_at="2026-11-22T12:00:00Z")
        await client.call_tool("ingest_graph_event", {"event": complete})
        for moment in ("10:00:00", "12:00:00"):
            stale = await send(event_id=moment, occurred_at=f"2026-11-22T{moment}Z", status="open")
            assert stale["outcome"] == "ignored_stale" and stale["affected_entity_keys"] == []
        assert tracker_graph.read_entity("local").status == "done"
        assert tracker_graph.read_entity("foreign").name == "Other project task"
        assert tracker_graph.read_entity("unlinked").name == "Unlinked task"


def test_tracker_event_rolls_back_task_and_link_when_receipt_write_fails(
    repository, tracker_graph, monkeypatch,
):
    original_insert = FakeCollection.insert

    def fail_receipt(self, document, **kwargs):
        if document.get("event_type") == "task.upserted":
            raise RuntimeError("Simulated journal failure")
        return original_insert(self, document, **kwargs)

    monkeypatch.setattr(FakeCollection, "insert", fail_receipt)
    event = TaskUpserted(
        project_key="owned", task_key="rollback", name="Rollback", event_type="task.upserted",
        event_id="rollback", occurred_at="2026-11-22T11:00:00Z",
    )
    with pytest.raises(RuntimeError, match="Simulated journal failure"):
        tracker_graph.ingest_event("tracker", "owned", event)
    assert tracker_graph.read_entity("rollback") is None
    assert not repository.database.collection("architecture_edges").documents_by_key
    assert not repository.database.collection("architecture_events").documents_by_key

def test_guidance_approval_publishes_saved_revision_and_preserves_history(repository, monkeypatch):
    with patch.dict("os.environ", {"MGMCP_AUTH_REQUIRED": "false"}):
        from app.main import create_application

    graph = repository.architecture
    graph.ensure_storage_ready()
    graph.put_entity(Project(key="project", name="Project"))

    class JournalQuery:
        def execute(self, query, bind_vars):
            rows = repository.database.collection("architecture_events").documents_by_key.values()
            versions = [
                entry for entry in rows
                if entry["event_type"] == bind_vars["event_type"]
                and entry["project_key"] == bind_vars["project"]
                and entry["version"] < bind_vars["before"]
            ]
            return sorted(versions, key=lambda entry: entry["version"],
                          reverse=True)[:bind_vars["limit"]]

    repository.database.aql = JournalQuery()
    monkeypatch.setattr("app.main.build_repository", lambda settings: repository)
    secret = "test-only-guidance-approval-signer-key"
    client = TestClient(create_application(
        Settings(_env_file=None, auth_required=True, jwt_secret_key=secret),
    ))
    path = "/api/review/projects/project/guidance"

    def auth(role):
        token = jwt.encode({"sub": role, "roles": [role]}, secret, algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    draft = {"revision": 0, "text": "First guidance"}
    assert client.get(path).status_code == 401
    assert client.post(path+"/draft", headers=auth("reader"), json=draft).status_code == 403
    saved = client.post(path+"/draft", headers=auth("writer"), json=draft)
    assert saved.status_code == 200 and saved.json()["draft"]["edited_by"] == "writer"
    assert client.get(path, headers=auth("reader")).json()["draft"] is None
    approval = {"revision": 1, "approve": True}
    assert client.post(path+"/approve", headers=auth("writer"), json=approval).status_code == 403
    assert client.post(
        path+"/approve", headers=auth("admin"), json={**approval, "approve": False},
    ).status_code == 422
    first = client.post(path+"/approve", headers=auth("admin"), json=approval).json()
    assert first["published"]["version"] == 1 and first["published"]["approved_by"] == "admin"
    assert first["draft"] is None
    changed = client.post(path+"/draft", headers=auth("writer"), json={
        "revision": first["revision"], "text": "Second guidance",
    }).json()
    assert client.get(path, headers=auth("reader")).json()["published"]["text"] == "First guidance"
    assert client.post(path+"/approve", headers=auth("admin"), json=approval).status_code == 409
    second = client.post(path+"/approve", headers=auth("admin"), json={
        "revision": changed["revision"], "approve": True,
    }).json()
    assert [entry["text"] for entry in second["history"]] == ["Second guidance", "First guidance"]
    older = client.get(path, headers=auth("reader"), params={"before_version": 2}).json()
    assert [entry["version"] for entry in older["history"]] == [1]

    global_path = "/api/review/guidance/global"
    assert client.get(global_path).status_code == 401
    for role in ("reader", "writer"):
        assert client.get(global_path, headers=auth(role)).status_code == 403
        assert client.post(f"{global_path}/draft", headers=auth(role), json=draft).status_code == 403
        assert client.post(f"{global_path}/approve", headers=auth(role), json=approval).status_code == 403

    admin = auth("admin")
    response = client.get(global_path, headers=admin)
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["revision"] == 0
    for revision, content in ((0, "Global baseline"), (2, "Updated global baseline")):
        saved = client.post(f"{global_path}/draft", headers=admin, json={
            "revision": revision, "text": content,
        })
        assert saved.status_code == 200
        assert saved.json()["draft"]["edited_by"] == "admin"
        if revision:
            stale = client.post(f"{global_path}/approve", headers=admin, json={
                "revision": revision, "approve": True,
            })
            assert stale.status_code == 409
        published = client.post(f"{global_path}/approve", headers=admin, json={
            "revision": revision + 1, "approve": True,
        })
        assert published.status_code == 200
        assert published.json()["published"]["approved_by"] == "admin"

    assert [entry["text"] for entry in published.json()["history"]] == [
        "Updated global baseline", "Global baseline",
    ]
    assert published.json()["draft"] is None
    older = client.get(global_path, headers=admin, params={"before_version": 2}).json()
    assert [entry["text"] for entry in older["history"]] == ["Global baseline"]
    assert client.get(path, headers=admin).json()["published"]["text"] == "Second guidance"


@pytest.mark.asyncio
async def test_post_commit_capture_links_entities_and_preserves_inferences_for_review(repository, tmp_path):
    from pathlib import Path
    from subprocess import run
    from sys import executable

    from app.domain.architecture import ArchitectureADR, Goal, Repository, Service, SourceFile
    from app.domain.knowledge_capture import CommitEnvelope
    from app.domain.review_details import ProjectEntity, ProjectScope
    from app.services.capture_worker import CaptureWorker
    from app.services.knowledge_capture import KnowledgeCaptureService

    graph = repository.architecture
    graph.ensure_storage_ready()
    nodes = [
        Project(key="project", name="Project"),
        Repository(key="repo", name="Source", role="source"),
        SourceFile(key="file", name="service.py", source_path="service.py", repository="Source"),
        Service(key="service", name="AssetService", source_path="service.py", repository="Source"),
        ArchitectureADR(key="decision", name="ADR-12", memory_key="adr-12"),
    ]
    for node in nodes:
        graph.put_entity(node)
    goal = Goal(key="goal", name="Ship assets", project_key="project", service_keys=["service"])
    closure = [*nodes[1:], goal]
    graph.capture_goal_support = lambda changed, goals: {}
    graph.project_scope = lambda key: ProjectScope(
        entities=[ProjectEntity(**node.model_dump(), reason="in project") for node in closure],
        dependencies=[], repository_names=["Source"],
    )
    graph.read_entity = lambda key, read=graph.read_entity: goal if key == goal.key else read(key)

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    spool = tmp_path / "spool"

    def git(*args):
        return run(["git", *args], cwd=checkout, check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.name", "Capture Test")
    git("config", "user.email", "capture@example.invalid")
    git("config", "core.hooksPath", ".git/hooks")
    installers = Path(__file__).resolve().parents[1] / "scripts"
    run([executable, str(installers / "install_capture_hook.py"), str(spool)],
        cwd=checkout, check=True, capture_output=True)
    (checkout / "service.py").write_text("class AssetService:\n    # Implements ADR-12.\n    pass\n")
    git("add", "service.py")
    git("commit", "-m", "Record asset implementation")
    envelope, = spool.glob("*.capture")
    first_payload = envelope.read_bytes()
    commit = CommitEnvelope.model_validate_json(first_payload).commit
    assert commit.sha == git("rev-parse", "HEAD").stdout.strip()
    assert commit.changed_files == ["service.py"]

    capture = KnowledgeCaptureService(graph, repository)
    worker = CaptureWorker(Settings(
        _env_file=None, auth_required=False, capture_project_key="project", capture_repository_key="repo",
        capture_spool=str(spool),
    ), capture)
    worker.read_spool()
    assert not envelope.exists()
    observation = capture.commit("project", "repo", commit)
    assert "Detected additions: service, ADR reference." in observation.summary
    assert set(observation.related_entity_keys) == {"project", "repo", "service", "file"}
    assert ("service", "decision", "implements") in {
        (link.source_key, link.target_key, link.relationship_type) for link in observation.suggestions
    }
    assert "Ship assets" in observation.summary
    assert any(link.target_key == "goal" for link in observation.suggestions)
    envelope.write_bytes(first_payload)
    worker.read_spool()
    assert capture.commit("project", "repo", commit) == observation
    edges = repository.database.collection("architecture_edges").documents_by_key.values()
    assert len(edges) == 4
    assert all(edge["relationship_type"] == "related_to" for edge in edges)

    server = build_mcp_server(
        Settings(_env_file=None, auth_required=False), MemorySearchService(repository), StaticKnowledgeBase(),
        architecture_service=ArchitectureService(graph, repository),
    )
    async with Client(server) as client:
        reflected = (await client.call_tool("review_change", {"change": {
            "project_key": "project", "diff": commit.diff, "test_results": "Two tests passed",
            "rationale": "Implement the accepted assets decision", "linked_entities": ["service"],
        }})).structured_content
        assert "Two tests passed" in reflected["summary"]
        assert "Implement the accepted assets decision" in reflected["summary"]
        assert "ADR-12" in reflected["summary"]
        with pytest.raises(ToolError, match="belong to the selected project"):
            await client.call_tool("review_change", {"change": {
                "project_key": "project", "diff": commit.diff, "linked_entities": ["unrelated"],
            }})
