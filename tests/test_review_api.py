from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.domain.architecture import (
    ArchitectureADR,
    ArchitecturePath,
    ArchitectureRelationship,
    Project,
    Service,
    SourceFile,
)
from app.domain.architecture_events import TrackerActivity
from app.domain.exceptions import ADRError
from app.domain.models import (
    ADRConflict,
    ADRConflictRecord,
    ADRConflictResolution,
    ADRConflictScope,
    ArchitecturalDecisionRecord,
    MemoryDocument,
    MemoryRevalidationResult,
    MemorySearchHit,
)
from app.domain.review_details import ProjectDocument, ProjectEntity, ProjectScope, ReviewEntityLink
from app.domain.test_runs import TestRunDetail, TestRunHistory, TestRunSummary

_SECRET = "review-api-test-signing-key-32-bytes"
_AUDIENCE = "review"


def auth(subject: str, role: str, **profile: str) -> dict[str, str]:
    token = jwt.encode(
        {"sub": subject, "roles": [role], "aud": _AUDIENCE, **profile},
        _SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def review_api(monkeypatch, request):
    with_failed_run = getattr(request, "param", False)
    with patch.dict("os.environ", {"MGMCP_AUTH_REQUIRED": "false"}):
        from app.main import create_application
    adr = ArchitectureADR(key="graph-adr", name="Authentication ADR", memory_key="record")
    service = Service(key="service", name="Identity service", source_path="app/auth.py")

    source = SourceFile(key="source", name="auth.py", source_path="app/auth.py")

    class Graph:
        def count_by_entity_type(self, *, source_repositories_only=False):
            return {"ADR": 7, "Repository": 1 if source_repositories_only else 2}

        def find_memory_entities(self, key):
            return [adr] if key == "record" else []

        def search(self, request):
            return [service] if request.entity_type == "Service" else []

        def read_entity(self, key):
            return {
                "graph-adr": adr, "service": service,
                "project": Project(key="project", name="Identity"),
                "other": Project(key="other", name="Other"),
            }.get(key)

        def recent_tracker_activity(self, key, limit):
            assert limit == 10
            if key != "project":
                return []
            return [TrackerActivity(
                event_id="sync-2", event_type="task.upserted", task_key="task",
                task_name="Identity backlog", occurred_at="2026-11-22T09:00:00Z",
                recorded_at="2026-11-22T10:00:00Z", outcome="ignored_stale",
            )]

        def latest_test_run(self, project_key):
            assert project_key == "project"
            if with_failed_run:
                return TestRunDetail(
                    run_id="101:1", commit="b" * 40, finished_at="2026-11-22T13:00:00Z",
                    status="failed", passed=2, failed=1, skipped=0,
                    project_key=project_key, repository_key="repo", failures=[],
                )
            return None

        def latest_passing_test_run(self, project_key):
            return self.read_test_run(project_key, "100:2")

        def read_test_run(self, project_key, run_id):
            assert project_key == "project"
            if run_id != "100:2":
                return None
            return TestRunDetail(
                **self.list_test_runs(project_key, 1, None).runs[0].model_dump(),
                project_key=project_key, repository_key="repo", failures=[],
            )

        def list_test_runs(self, project_key, limit, cursor):
            assert (project_key, limit, cursor) == ("project", 1, None)
            return TestRunHistory(runs=[TestRunSummary(
                run_id="100:2", commit="a" * 40, finished_at="2026-11-22T12:00:00Z",
                status="passed", passed=3, failed=0, skipped=0,
            )], next_cursor="a" * 64)

        def project_scope(self, key):
            assert key == "project"
            return ProjectScope(
                entities=[
                    ProjectEntity(
                        key=adr.key, name=adr.name, entity_type=adr.entity_type,
                        memory_key=adr.memory_key, reason="Defined in a linked repository",
                    ),
                    ProjectEntity(
                        key=service.key, name=service.name, entity_type=service.entity_type,
                        reason="Indexed in Identity",
                    ),
                ],
                dependencies=[],
                repository_names=["Identity"],
            )

        def trace(self, request):
            if request.entity_types == ["SourceFile", "Service"]:
                if request.depth == 1:
                    return []
                assert request.min_depth == request.depth == 2
                assert request.entity_types == ["SourceFile", "Service"]
                return [
                    ArchitecturePath(
                        entity=source,
                        nodes=[adr, service, source],
                        relationships=[
                            ArchitectureRelationship(
                                source_key=left.key,
                                target_key=right.key,
                                relationship_type=kind,
                                source="design review",
                                created_at="2026-11-20T10:00:00Z",
                                updated_at="2026-11-20T10:00:00Z",
                            )
                            for left, right, kind in (
                                (adr, service, "implemented_by"),
                                (service, source, "defined_in"),
                            )
                        ],
                    )
                ]
            if request.entity_types == ["Service"]:
                if request.depth > 1:
                    assert request.shortest_paths
                    return []
            else:
                assert request.entity_types == ["ADR", "Service", "SourceFile", "TestSuite"]
            if request.key == "service":
                return []
            link = ArchitectureRelationship(
                source_key=adr.key,
                target_key=service.key,
                relationship_type="implemented_by",
                source="design review",
                created_at="2026-11-20T10:00:00Z",
                updated_at="2026-11-20T10:00:00Z",
            )
            return [
                ArchitecturePath(entity=service, nodes=[adr, service], relationships=[link])
            ]

    class Repository:
        architecture = Graph()

        def project_documents(self, key, repositories, linked_keys, query, as_of):
            assert (key, repositories, linked_keys) == ("project", ["Identity"], ["record"])
            assert query in ("", "identity")
            if as_of and as_of < date(2026, 1, 15):
                return []
            return [ProjectDocument(
                key="record", title="Authentication ADR", kind="adr",
                reason="Linked graph record", status="accepted",
                recorded_date="2026-01-08", last_recorded_update="2026-01-20",
            )]

        def read_document(self, key):
            if key == "unembedded":
                return MemoryDocument(key=key, title="No embedding", content="A legacy record")
            if key != "record":
                return None
            return ArchitecturalDecisionRecord(
                key=key,
                title="Authentication ADR",
                date="2026-01-01",
                content="Use OIDC",
                decision="Use OIDC",
                rationale="Tenant isolation",
                consequences="Configure clients",
                effective_date="2026-01-15",
                expiration_date="2027-01-01",
                confidence=0.4,
                embedding=[0.25],
                embedding_provider="ollama",
                embedding_model="nomic-embed-text",
                embedding_dimension=1,
                embedding_version="1",
                validation_history=[
                    {
                        "reviewer": "writer",
                        "checked_at": "2026-11-20T11:00:00Z",
                        "evidence": ["repo@abc123:auth.py"],
                        "previous_confidence": 0.8,
                        "new_confidence": 0.4,
                    }
                ],
            )

        def list_adr_supersession_links(self, key):
            assert key == "record"
            return [
                ReviewEntityLink(
                    key="old",
                    name="Old authentication ADR",
                    destination="records",
                    entity_type="ADR",
                    relationships=["supersedes"],
                )
            ]

        def semantic_search(self, request):
            assert request.exclude_key == "record" and request.limit == 6
            assert request.embedding == [0.25] and request.embedding_version == "1"
            assert request.embedding_provider == "ollama"
            return [MemorySearchHit(document=MemoryDocument(
                key="match", title="Security design", content="Match",
            ), score=0.875)]

        def search_question_documents(self, terms, as_of, limit, *, adrs_only=False):
            assert as_of == date(2026, 11, 21) and limit == 8
            if adrs_only or terms == "unknown":
                return []
            assert terms == "authentication"
            record = self.read_document("record")
            return [
                record.model_copy(
                    update={
                        "conflict_scope": ADRConflictScope(
                            topic="authentication",
                            system="customer-api",
                        )
                    }
                ),
                MemoryDocument(
                    key="note",
                    title="Authentication note",
                    content="USE OIDC.",
                    effective_date="2026-01-15",
                ),
            ]

        def find_adr_conflicts(self, request):
            assert request.key == "record" and request.as_of == date(2026, 11, 21)
            record = self.read_document(request.key)
            first = ADRConflictRecord.model_validate(record.model_dump())
            second = first.model_copy(update={"key": "competing", "decision": "Use mTLS"})
            return [
                ADRConflict(original=first, competing=second, overlap_start=None, overlap_end=None)
            ]

        def list_overdue_validations(self, as_of, limit, offset):
            assert (as_of, limit, offset) == (date(2026, 11, 21), 3, 1)
            return []

        def list_adr_conflicts(self, query):
            assert query.as_of == date(2026, 11, 21)
            assert query.limit == 3 and query.offset == 2
            return []

        def revalidate_memory(self, key, validation, confidence):
            assert key == "record"
            assert validation.reviewer in {"writer", "admin"}
            assert validation.evidence == ["repo@abc123:auth.py"]
            assert confidence == 0.4
            return MemoryRevalidationResult(
                key=key,
                validation=validation,
                last_validated=date(2026, 11, 21),
                next_review_due=date(2026, 12, 21),
            )

        def resolve_adr_conflict(self, source, target, decision):
            if source == target:
                raise ADRError("An ADR cannot resolve a conflict with itself")
            assert decision.resolved_by == "writer"
            return ADRConflictResolution(
                **decision.model_dump(),
                source_key=source,
                target_key=target,
                overlap_start=None,
                overlap_end=None,
            )

    monkeypatch.setattr("app.main.build_repository", lambda settings: Repository())
    return TestClient(
        create_application(
            Settings(
                _env_file=None,
                auth_required=True,
                jwt_secret_key=_SECRET,
                jwt_audience=_AUDIENCE,
                graph_event_producer_subject="tracker",
                graph_event_project_key="project",
                ci_github_repository="example/project" if with_failed_run else "",
                ci_repository_key="repo" if with_failed_run else "",
            )
        )
    )


def test_review_api_auth_and_queue(review_api):
    assert review_api.get("/api/review/session").status_code == 401
    assert review_api.get("/api/review/queue").status_code == 401
    reader = auth("reader", "reader")
    assert review_api.get("/api/review/session", headers=reader).json() == {
        "subject": "reader",
        "display_name": None,
        "can_write": False,
        "auth_required": True,
        "role": "reader",
        "browser_session": False,
    }
    named_reader = auth("reader-id", "reader", name="Local Reader", preferred_username="reader")
    named_session = review_api.get("/api/review/session", headers=named_reader).json()
    assert named_session["display_name"] == "Local Reader"
    assert named_session["subject"] == "reader-id"
    queue = review_api.get(
        "/api/review/queue?as_of=2026-11-21&limit=3&validation_offset=1&conflict_offset=2",
        headers=reader,
    )
    assert queue.status_code == 200
    assert queue.headers["Cache-Control"] == "no-store"
    assert queue.json() == {
        "review_date": "2026-11-21",
        "overdue_validations": [],
        "unresolved_adr_conflicts": [],
        "inferred_relationships": [],
    }


def test_review_api_writer_permissions_payload_validation_and_resolution(review_api):
    writer = auth("writer", "writer")
    reader = auth("reader", "reader")
    validation = {"evidence": ["repo@abc123:auth.py"], "confidence": 0.4}
    path = "/api/review/records/record/revalidate"
    assert review_api.post(path, json=validation).status_code == 401
    assert review_api.post(path, headers=reader, json=validation).status_code == 403
    invalid = review_api.post(path, headers=writer, json={**validation, "confidence": 2})
    assert invalid.status_code == 422
    saved = review_api.post(path, headers=writer, json=validation)
    assert saved.status_code == 200 and saved.json()["validation"]["reviewer"] == "writer"
    admin = auth("admin", "admin")
    assert review_api.get("/api/review/session", headers=admin).json()["role"] == "admin"
    saved = review_api.post(path, headers=admin, json=validation)
    assert saved.status_code == 200 and saved.json()["validation"]["reviewer"] == "admin"

    path = "/api/review/conflicts/resolve"
    resolution = {
        "source_key": "adr-a",
        "target_key": "adr-b",
        "action": "coexist",
        "rationale": "Different traffic",
    }
    assert review_api.post(path, headers=reader, json=resolution).status_code == 403
    same = review_api.post(path, headers=writer, json={**resolution, "target_key": "adr-a"})
    assert same.status_code == 409
    result = review_api.post(path, headers=writer, json=resolution)
    assert result.status_code == 200 and result.json()["resolved_by"] == "writer"


def test_review_record_details_history_and_typed_links(review_api):
    assert review_api.get("/api/review/records/record").status_code == 401
    reader = auth("reader", "reader")
    assert review_api.get("/api/review/records/missing", headers=reader).status_code == 404
    response = review_api.get("/api/review/records/record", headers=reader)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    detail = response.json()
    assert detail["record"]["effective_date"] == "2026-01-15"
    assert detail["record"]["expiration_date"] == "2027-01-01"
    assert "embedding" not in detail["record"]
    assert detail["embedding"] == {
        "provider": "ollama",
        "model": "nomic-embed-text",
        "dimension": 1,
        "version": "1",
        "state": "mismatch",
    }
    missing_embedding = review_api.get("/api/review/records/unembedded", headers=reader).json()
    assert missing_embedding["embedding"]["state"] == "missing"
    (validation,) = detail["record"]["validation_history"]
    assert validation["previous_confidence"] == 0.8
    assert validation["new_confidence"] == 0.4
    assert validation["evidence"] == ["repo@abc123:auth.py"]
    assert [(link["destination"], link["key"]) for link in detail["links"]] == [
        ("records", "old"),
        ("entities", "service"),
    ]
    service = review_api.get("/api/review/entities/service", headers=reader).json()
    assert service["record"] is None
    assert service["embedding"] is None
    assert service["entity"]["source_path"] == "app/auth.py"
    linked_adr = review_api.get("/api/review/entities/graph-adr", headers=reader).json()
    assert linked_adr["record"]["key"] == "record"


def test_ask_graph_uses_as_of_and_returns_record_citations_conflicts_and_gaps(review_api):
    headers = auth("reader", "reader")
    path = "/api/review/ask"
    body = {"question": "What is our current authentication strategy?", "as_of": "2026-11-21"}
    assert review_api.post(path, json=body).status_code == 401
    response = review_api.post(path, json=body, headers=headers)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    answer = response.json()
    assert answer["as_of"] == "2026-11-21"
    decisions = {
        finding["text"]: [cite["key"] for cite in finding["citations"]]
        for finding in answer["findings"]
    }
    assert decisions == {"Use OIDC": ["record", "note"], "Use mTLS": ["competing"]}
    assert "competing decisions" in answer["summary"]
    assert answer["conflicts"][0]["competing"]["decision"] == "Use mTLS"

    rationale = review_api.post(
        path,
        json={**body, "question": "Why this authentication strategy?"},
        headers=headers,
    ).json()
    assert rationale["findings"][0]["text"] == "Tenant isolation"
    assert {cite["key"] for cite in rationale["findings"][0]["citations"]} == {
        "record",
        "competing",
    }

    missing = review_api.post(path, json={**body, "question": "unknown"}, headers=headers).json()
    assert not missing["findings"]
    assert missing["gaps"]

def test_ask_graph_counts_only_requested_node_type_and_handles_historical_dates(review_api):
    path = "/api/review/ask"
    headers = auth("reader", "reader")

    def ask(question, as_of=None):
        response = review_api.post(path, headers=headers, json={
            "question": question,
            "as_of": as_of,
        })
        assert response.status_code == 200
        answer = response.json()
        assert not answer["findings"]
        assert not answer["conflicts"]
        return answer

    assert ask("How many projects are in the graph?")["summary"] == (
        "The architecture graph contains 0 projects."
    )
    today = datetime.now(UTC).date().isoformat()
    assert ask("How many repos are there?", today)["summary"] == (
        "The architecture graph contains 1 source repository."
    )
    summary = ask("Show me what concrete node types are in the graph.")["summary"]
    assert "ADR (7), Repository (2)" in summary
    historical = ask("How many projects are in the graph?", "2000-01-01")
    assert "unavailable" in historical["summary"]
    assert historical["gaps"]

def test_graph_explorer_exposes_typed_search_and_direct_relationships(review_api):
    base = "/api/review/graph"
    reader = auth("reader", "reader")
    assert review_api.get(f"{base}/metamodel").status_code == 401
    model = review_api.get(f"{base}/metamodel", headers=reader).json()
    assert {"Project", "Repository", "ADR"} <= set(model["entity_types"])
    assert model["relationships"]["implements"] == "implemented_by"
    search = review_api.get(f"{base}/entities?entity_type=Service", headers=reader)
    assert search.status_code == 200
    assert [node["key"] for node in search.json()] == ["service"]
    path = f"{base}/entities/graph-adr?entity_type=Service"
    graph = review_api.get(path, headers=reader).json()
    assert graph["center"]["key"] == "graph-adr"
    assert graph["paths"][0]["entity"]["key"] == "service"
    assert graph["paths"][0]["relationships"][0]["relationship_type"] == "implemented_by"
    assert not graph["truncated"]
    assert review_api.get(f"{base}/entities/missing", headers=reader).status_code == 404


def test_graph_explorer_hops_keep_intermediate_nodes(review_api):
    path = "/api/review/graph/entities/graph-adr"
    reader = auth("reader", "reader")
    response = review_api.get(
        path, params={"hops": 2, "entity_types": ["SourceFile", "Service"]}, headers=reader
    )
    assert response.status_code == 200
    (result,) = response.json()["paths"]
    assert [node["key"] for node in result["nodes"]] == ["graph-adr", "service", "source"]
    assert len(result["relationships"]) == 2
    for invalid in (0, 11):
        assert review_api.get(path, params={"hops": invalid}, headers=reader).status_code == 422
    response = review_api.get(path, params={"hops": 10, "entity_type": "Service"}, headers=reader)
    assert response.status_code == 200
    assert len(response.json()["paths"]) == 1


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_search_memory_uses_selected_mode_and_date(review_api, monkeypatch, mode):
    from app.domain.models import MemorySearchHit
    from app.services.search_service import MemorySearchService

    async def retrieve(self, query, limit, as_of, kinds):
        assert kinds == ["architecture", "adr"]
        assert query == "identity" and limit == 20 and as_of == date(2026, 11, 21)
        return [MemorySearchHit(
            document=MemoryDocument(
                key="identity", title="Identity strategy", kind="architecture",
                content="Evidence " * 100, source="design review", source_id="ADR-12",
                effective_date="2026-01-01", expiration_date="2027-01-01", embedding=[0.5],
            ),
            retrieval_mode=mode, fallback_used=(mode == "semantic"),
            metadata={
                "embedding_provider": "ollama", "embedding_model": "nomic-embed-text",
                "embedding_dimension": 768, "embedding_version": "1",
            },
        )]

    monkeypatch.setattr(MemorySearchService, f"{mode}_search", retrieve)
    path = "/api/review/search"
    body = {
        "query": " identity ", "mode": mode, "as_of": "2026-11-21",
        "kinds": ["architecture", "adr"],
    }
    assert review_api.post(path, json=body).status_code == 401
    headers = auth("reader", "reader")
    invalid = review_api.post(path, headers=headers, json={**body, "mode": "lexical"})
    assert invalid.status_code == 422
    response = review_api.post(path, headers=headers, json=body)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.json()
    assert data["fallback_used"] == (mode == "semantic")
    assert data["embedding"] == {
        "provider": "ollama", "model": "nomic-embed-text", "dimension": 768, "version": "1",
    }
    (match,) = data["matches"]
    excerpt = match.pop("excerpt")
    assert len(excerpt) <= 320 and excerpt.endswith("…")
    assert match == {
        "key": "identity", "title": "Identity strategy", "kind": "architecture",
        "source": "design review", "source_id": "ADR-12",
        "effective_date": "2026-01-01", "expiration_date": "2027-01-01",
    }


def test_project_overview_respects_graph_scope_and_date(review_api):
    path = "/api/review/projects/project"
    assert review_api.get(path).status_code == 401
    headers = auth("reader", "reader")
    response = review_api.get(path, headers=headers)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert {item["key"] for item in response.json()["entities"]} == {"graph-adr", "service"}
    historical = review_api.get(
        path, headers=headers, params={"query": "Identity", "as_of": "2025-12-31"}
    ).json()
    assert [item["key"] for item in historical["entities"]] == ["service"]
    assert historical["documents"] == []
    assert review_api.get("/api/review/projects/service", headers=headers).status_code == 404
    summary = review_api.get(f"{path}/summary", headers=headers)
    assert summary.status_code == 200
    assert summary.json() == {
        "open_tasks": 0,
        "proposed_adrs": 0,
        "earliest_record": "2026-01-08",
        "latest_record": "2026-01-20",
        "last_index_date": None,
    }


def test_similar_records_use_stored_profile_and_return_minimal_matches(review_api):
    base = "/api/review/records"
    assert review_api.get(f"{base}/record/similar").status_code == 401
    headers = auth("reader", "reader")
    response = review_api.get(f"{base}/record/similar", headers=headers)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"available": True, "matches": [
        {"key": "match", "title": "Security design", "kind": "memory", "similarity": 0.875},
    ]}
    assert review_api.get(f"{base}/unembedded/similar", headers=headers).json() == {
        "available": False, "matches": [],
    }
    assert review_api.get(f"{base}/missing/similar", headers=headers).status_code == 404


def test_project_testing_requires_reader_and_returns_empty_state(review_api):
    path = "/api/review/projects/project/testing"
    assert review_api.get(path).status_code == 401
    reader = auth("reader", "reader")
    response = review_api.get(path, headers=reader)
    assert response.status_code == 200
    assert response.json() == {
        "latest_run": None, "run_url": None, "can_suggest_fix": False, "last_passing_run": None,
        "health": "unknown",
        "can_draft_fix": False,
        "can_apply_draft": False,
        "can_complete_fix": False,
        "can_publish_fix": False,
        "can_review_fix": True,
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert review_api.get("/api/review/projects/missing/testing", headers=reader).status_code == 404


def test_project_testing_lists_and_opens_past_runs(review_api):
    path = "/api/review/projects/project/testing"
    assert review_api.get(f"{path}/history").status_code == 401
    reader = auth("reader", "reader")
    history = review_api.get(f"{path}/history?limit=1", headers=reader)
    assert history.status_code == 200
    assert history.json()["next_cursor"] == "a" * 64
    (summary,) = history.json()["runs"]
    assert summary["run_id"] == "100:2"
    assert "failures" not in summary
    run = review_api.get(path, params={"run_id": "100:2"}, headers=reader)
    assert run.status_code == 200
    assert run.json()["latest_run"]["passed"] == 3
    assert review_api.get(path, params={"run_id": "100:3"}, headers=reader).status_code == 404


@pytest.mark.parametrize("review_api", [True], indirect=True)
def test_project_testing_links_to_last_pass_after_latest_failure(review_api):
    response = review_api.get("/api/review/projects/project/testing", headers=auth("reader", "reader"))
    assert response.status_code == 200
    data = response.json()
    assert data["latest_run"]["run_id"] == "101:1"
    assert data["last_passing_run"] == {
        "run_id": "100:2",
        "finished_at": "2026-11-22T12:00:00Z",
        "run_url": "https://github.com/example/project/actions/runs/100/attempts/2",
    }


def test_draft_approval_requires_writer_and_explicit_confirmation(review_api):
    path = "/api/review/projects/project/testing/drafts/" + "a" * 32 + "/apply"
    issue = {"run_id": "100:2", "failure_index": 0}
    payload = {**issue, "approve": True}
    assert review_api.post(path, json=payload).status_code == 401
    assert review_api.post(path, headers=auth("reader", "reader"), json=payload).status_code == 403
    for payload in ({}, {"approve": False}):
        assert review_api.post(
            path, headers=auth("writer", "writer"), json={**issue, **payload},
        ).status_code == 422

    for action in ("post-verify-fix", "complete-fix"):
        endpoint = "/api/review/projects/project/testing/" + action
        assert review_api.post(endpoint, json=issue).status_code == 401
        assert review_api.post(endpoint, headers=auth("reader", "reader"), json=issue).status_code == 403

    publish = "/api/review/projects/project/testing/publish-fix"
    commit = {**issue, "commit": "b" * 40}
    assert review_api.post(
        publish, headers=auth("reader", "reader"), json={**commit, "approve": True},
    ).status_code == 403
    assert review_api.post(
        publish, headers=auth("writer", "writer"), json={**commit, "approve": False},
    ).status_code == 422

def test_tracker_overview_is_read_only_and_scoped_to_selected_project(review_api):
    path = "/api/review/projects/project/tracker"
    assert review_api.get(path).status_code == 401
    reader = auth("reader", "reader")
    response = review_api.get(path, headers=reader)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["configured"] is True
    assert response.json()["recent_events"] == [{
        "event_id": "sync-2", "event_type": "task.upserted", "task_key": "task",
        "task_name": "Identity backlog", "occurred_at": "2026-11-22T09:00:00Z",
        "recorded_at": "2026-11-22T10:00:00Z", "outcome": "ignored_stale",
    }]
    other = review_api.get("/api/review/projects/other/tracker", headers=reader)
    assert other.json() == {"configured": False, "recent_events": []}
    assert review_api.get("/api/review/projects/missing/tracker", headers=reader).status_code == 404
    assert review_api.post(path, headers=reader, json={}).status_code == 405
