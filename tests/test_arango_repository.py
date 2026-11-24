from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from app.config.settings import Settings
from app.domain.architecture import (
    ArchitectureADR,
    ArchitectureLink,
    Project,
    Repository,
    architecture_key,
)
from app.domain.exceptions import ADRError, ArchitectureGraphError
from app.domain.knowledge_snapshot import KnowledgeSnapshot, SnapshotCoexistence
from app.domain.models import (
    ADRConflict,
    ADRConflictQuery,
    ADRConflictScope,
    ADRResolutionDecision,
    ArchitecturalDecisionRecord,
    GraphTraversalRequest,
    MemoryDocument,
    MemorySearchRequest,
    MemoryValidation,
)
from app.domain.project_guidance import GuidanceScope, PublishedGuidance
from app.mcp.server import build_mcp_server
from app.repositories.arango_architecture import ArangoArchitectureRepository
from app.repositories.arango_repository import ArangoMemoryRepository, ArangoRepositoryConfig
from app.repositories.knowledge_git import SNAPSHOT_PATH, git, verify_checkout
from app.services.ingestion_service import MemoryIngestionService
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

    def begin_transaction(self, *, exclusive=(), read=()):
        database = self
        writable = set(exclusive)
        readable = writable | ({read} if isinstance(read, str) else set(read or ()))

        class Transaction:
            collections = deepcopy(database.collections)

            @property
            def aql(self):
                return database.aql

            def collection(self, name):
                assert name in readable
                return self.collections[name]

            def commit_transaction(self):
                assert writable
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


@pytest.fixture()
def knowledge_source(repository, monkeypatch, tmp_path):
    repository.ensure_storage_ready()
    project = Project(key="project", name="Project")
    source = Repository(
        key="repo", name="Source", role="source", metadata={"git_checkout": str(tmp_path)},
    )
    for entity in (project, source):
        repository.database.collection("architecture_entities").insert(
            repository.architecture._entity_payload(entity),
        )
    provenance = []
    monkeypatch.setattr(
        "app.repositories.arango_repository.verify_checkout",
        lambda *args: provenance.append(args),
    )
    monkeypatch.setattr(ArangoArchitectureRepository, "read_entity", lambda self, key: project)
    monkeypatch.setattr(
        ArangoArchitectureRepository, "project_source_repositories", lambda self, key: [source],
    )

    def guidance_scope(architecture, key):
        database = architecture._database()
        nodes = database.collection("architecture_entities")
        edges = database.collection("architecture_edges")
        repositories = architecture.project_source_repositories(key)
        owners = {key, *(repo.key for repo in repositories)}
        linked = {
            edge["target_key"] for edge in edges.documents_by_key.values()
            if (edge["source_key"] in owners and edge["relationship_type"] == "defines")
            or (edge["source_key"] == key and edge["relationship_type"] != "uses")
        }
        linked.update(
            edge["source_key"] for edge in edges.find({"target_key": key})
            if edge["relationship_type"] != "uses"
        )
        linked.update(
            node["_key"] for repo in repositories for node in nodes.find({"repository": repo.name})
        )
        return GuidanceScope(
            entity_keys=[key],
            adr_keys=sorted({nodes.get(node)["memory_key"] for node in linked}),
        )

    monkeypatch.setattr(ArangoArchitectureRepository, "guidance_scope", guidance_scope)
    return project, source, provenance


def test_approved_knowledge_import_round_trip_and_drift(repository, knowledge_source, monkeypatch, tmp_path):
    project, source, provenance = knowledge_source
    decision = ArchitecturalDecisionRecord(
        key="adr", title="Decision", content="Original", date="2026-11-23",
        status="accepted", decision="Keep", rationale="Evidence", consequences="Preserved",
    )
    guidance = PublishedGuidance(
        text="Published guidance", edited_by="writer", approved_by="admin", version=3,
        edited_at="2026-11-22T12:00:00Z", published_at="2026-11-23T12:00:00Z",
    )
    revision, source_revision = "a" * 40, "b" * 40
    incoming = KnowledgeSnapshot.from_database(
        project.key, source.key, source_revision, [decision], guidance,
    )

    drift, imported = repository.reconcile_knowledge(incoming, revision, root=tmp_path, apply=False)
    assert not imported
    assert provenance[-1] == (tmp_path, str(tmp_path), revision, incoming)
    assert [item.status for item in drift] == ["git_only", "git_only"]
    assert repository.export_knowledge(project.key, source.key, revision, root=tmp_path).records() == {}

    with monkeypatch.context() as patch:
        patch.setattr(
            ArangoArchitectureRepository, "write_guidance_state", lambda *args: None,
        )
        with pytest.raises(ArchitectureGraphError, match="round-trip"):
            repository.reconcile_knowledge(incoming, revision, root=tmp_path, apply=True)
    assert repository.export_knowledge(project.key, source.key, revision, root=tmp_path).records() == {}

    assert repository.reconcile_knowledge(incoming, revision, root=tmp_path, apply=True)[1]
    assert repository.database.collection("memory_documents").get(decision.key)[
        "next_review_due"
    ] == "2027-02-21"
    assert repository.export_knowledge(
        project.key, source.key, revision, root=tmp_path,
    ).records() == incoming.records()
    assert repository.reconcile_knowledge(incoming, revision, root=tmp_path, apply=True) == ([], False)
    journal = repository.database.collection("architecture_events")
    event, = journal.find({"event_type": "project.knowledge.imported"})
    assert (event["git_revision"], event["source_revision"]) == (revision, source_revision)

    repository.database.collection("memory_documents").update({
        "_key": decision.key, "content": "Database edit",
    })
    drift, imported = repository.reconcile_knowledge(incoming, revision, root=tmp_path, apply=True)
    assert not imported
    difference, = drift
    assert difference.status == "different"
    assert difference.git["content"] == "Original"
    assert difference.database["content"] == "Database edit"
    assert repository.read_adr(decision.key).content == "Database edit"


def test_knowledge_drift_reports_deleted_review_schedule(repository, knowledge_source, tmp_path):
    project, source, _ = knowledge_source
    record = ArchitecturalDecisionRecord(
        key="adr", title="Decision", content="Original", date="2026-11-23",
        status="accepted", decision="Keep", rationale="Evidence", consequences="Preserved",
    )
    revision = "a" * 40
    incoming = KnowledgeSnapshot.from_database(project.key, source.key, revision, [record], None)
    assert repository.reconcile_knowledge(incoming, revision, root=tmp_path, apply=True)[1]
    repository.database.collection("memory_documents").get(record.key).pop("next_review_due")

    for apply in (False, True):
        drift, imported = repository.reconcile_knowledge(incoming, revision, root=tmp_path, apply=apply)
        assert not imported
        difference, = drift
        assert difference.status == "different"
        assert difference.git["next_review_due"] == "2027-02-21"
        assert difference.database["next_review_due"] is None
    assert "next_review_due" not in repository.database.collection("memory_documents").get(record.key)

    with pytest.raises(ArchitectureGraphError, match="Accepted ADR 'adr'.*Backfill"):
        repository.export_knowledge(project.key, source.key, revision, root=tmp_path)

    repository.database.collection("memory_documents").update({
        "_key": record.key, "next_review_due": "2027-03-01",
    })
    exported = repository.export_knowledge(project.key, source.key, revision, root=tmp_path)
    snapshot = KnowledgeSnapshot.model_validate_json(json.dumps(exported.portable_json()))
    assert snapshot.adrs[0].next_review_due == date(2027, 3, 1)
    assert repository.reconcile_knowledge(snapshot, "b" * 40, root=tmp_path, apply=False) == ([], False)


@pytest.fixture()
def overlapping_knowledge(repository, knowledge_source, monkeypatch):
    project, source, _ = knowledge_source
    adrs = [
        ArchitecturalDecisionRecord(
            key=key, title=decision, content=decision, decision=decision,
            rationale="Operational requirements", consequences="Update clients",
            date="2026-10-01", effective_date=effective_date, expiration_date="2027-01-01",
            status="accepted", source="design review", source_id=evidence,
            conflict_scope=ADRConflictScope(topic="authentication", system="customer API"),
        )
        for key, decision, effective_date, evidence in (
            ("first", "Use OIDC", "2026-10-01", "review-42"),
            ("second", "Use API keys", "2026-11-01", "review-43"),
        )
    ]

    def query_conflicts(transaction, request, *, key, competing_key, **kwargs):
        documents = transaction.collection("memory_documents")
        edges = transaction.collection("memory_edges")
        other = next(adr.key for adr in adrs if adr.key != key)
        if competing_key is not None:
            assert competing_key == other
        original, competing = documents.get(key), documents.get(other)
        assert original["status"] == "accepted"
        assert request.as_of is None
        if competing is None:
            return []
        original_scope = original.get("conflict_scope")
        competing_scope = competing.get("conflict_scope")
        if not original_scope or not competing_scope or (
            ADRConflictScope.model_validate(original_scope) != ADRConflictScope.model_validate(competing_scope)
        ):
            return []
        for edge in edges.find({"relation": "adr_conflict_resolution"}):
            if edge.get("historical"):
                continue
            if (
                {edge["source_key"], edge["target_key"]} == {key, other}
                and (edge.get("topic"), edge.get("namespace")) in (
                    (None, None), (original_scope["topic"], original.get("namespace") or "default"),
                )
                and all(edge.get(field) == original_scope.get(field) for field in (
                    "system", "environment",
                ))
                and (edge["overlap_start"], edge["overlap_end"]) == ("2026-11-01", "2027-01-01")
            ):
                return []
        return [ADRConflict(
            original=original, competing=competing,
            overlap_start="2026-11-01", overlap_end="2027-01-01",
        )]

    monkeypatch.setattr(repository, "_query_adr_conflicts", query_conflicts)
    return KnowledgeSnapshot.from_database(project.key, source.key, "a" * 40, adrs, None)


@pytest.mark.parametrize("existing", [False, True])
def test_knowledge_import_rejects_unresolved_overlap(repository, overlapping_knowledge, tmp_path, existing):
    incoming = overlapping_knowledge
    revision = incoming.source_revision
    if existing:
        first_only = incoming.model_copy(update={"adrs": incoming.adrs[:1]})
        assert repository.reconcile_knowledge(first_only, revision, root=tmp_path, apply=True)[1]

    with pytest.raises(ArchitectureGraphError, match="unresolved conflicts") as error:
        repository.reconcile_knowledge(incoming, "b" * 40, root=tmp_path, apply=True)

    report = json.loads(str(error.value).split(". ", 1)[1])
    conflict, = report["conflicts"]
    assert {
        (conflict[side]["decision"], conflict[side]["source_id"])
        for side in ("original", "competing")
    } == {("Use OIDC", "review-42"), ("Use API keys", "review-43")}
    assert conflict["overlap_start"] == "2026-11-01"
    stored = repository.export_knowledge(incoming.project_key, incoming.repository_key, revision, root=tmp_path)
    assert list(stored.records()) == (["adr/first"] if existing else [])


@pytest.mark.parametrize("replacement_action", [None, "coexist", "supersede"])
def test_knowledge_round_trip_restores_coexistence(
    repository, overlapping_knowledge, tmp_path, replacement_action,
):
    incoming = overlapping_knowledge
    revision = incoming.source_revision
    fresh_database = deepcopy(repository.database)
    # Seed the source with ADRs and record their resolution through the writer's workflow.
    documents = repository.database.collection("memory_documents")
    for adr in incoming.adrs:
        documents.insert(repository._document_payload(adr))
    resolution = repository.resolve_adr_conflict("second", "first", ADRResolutionDecision(
        action="coexist", resolved_by="writer", resolved_at="2026-11-23T18:30:00Z",
        rationale="Keep API keys for existing clients while OIDC serves new clients.",
    ))
    # Link the source ADRs by importing the already approved snapshot into the empty project.
    repository.__dict__["database"] = deepcopy(fresh_database)
    approved = KnowledgeSnapshot.from_database(
        incoming.project_key, incoming.repository_key, revision, incoming.adrs, None, [resolution],
    )
    assert repository.reconcile_knowledge(approved, revision, root=tmp_path, apply=True)[1]
    if replacement_action:
        repository.set_adr_conflict_scope("first", None)
    exported = repository.export_knowledge(incoming.project_key, incoming.repository_key, revision, root=tmp_path)
    restored = KnowledgeSnapshot.model_validate_json(json.dumps(exported.portable_json()))

    repository.__dict__["database"] = deepcopy(fresh_database)
    assert repository.reconcile_knowledge(restored, revision, root=tmp_path, apply=True)[1]
    assert repository.get_adr_conflict_resolution("first", "second") == resolution
    assert repository.reconcile_knowledge(restored, revision, root=tmp_path, apply=True) == ([], False)
    if replacement_action:
        assert repository.read_adr("first").conflict_scope is None
        new_scope = ADRConflictScope(topic="authorization", system="customer API")
        for key in ("first", "second"):
            repository.set_adr_conflict_scope(key, new_scope)
        changed = repository.export_knowledge(
            incoming.project_key, incoming.repository_key, revision, root=tmp_path,
        )
        reopened = repository.database
        repository.__dict__["database"] = deepcopy(fresh_database)
        with pytest.raises(ArchitectureGraphError, match="unresolved conflicts"):
            repository.reconcile_knowledge(changed, revision, root=tmp_path, apply=True)
        repository.__dict__["database"] = reopened
        assert repository.find_adr_conflicts(ADRConflictQuery(key="first"))
        decision = ADRResolutionDecision(
            action=replacement_action,
            rationale=resolution.rationale if replacement_action == "coexist" else "Standardize on API keys.",
            resolved_by=resolution.resolved_by,
            resolved_at="2026-11-24T18:30:00Z",
        )
        renewed = repository.resolve_adr_conflict("first", "second", decision)
        assert renewed.topic == "authorization"
        assert repository.get_adr_conflict_resolution("first", "second") == renewed
        history, = repository.database.collection("memory_edges").find({"historical": True})
        assert history["topic"] == "authentication"
        if replacement_action == "coexist":
            assert repository.find_adr_conflicts(ADRConflictQuery(key="first")) == []
        else:
            assert repository.read_adr("first").status == "superseded"
        return

    drift, imported = repository.reconcile_knowledge(incoming, revision, root=tmp_path, apply=True)
    assert not imported
    difference, = drift
    assert difference.record == "coexistence/first/second"
    assert difference.status == "database_only"


def test_knowledge_validates_resolution_only_import(repository, overlapping_knowledge, tmp_path):
    incoming = overlapping_knowledge
    revision = incoming.source_revision
    accepted_without_scope = incoming.model_copy(update={
        "adrs": [record.model_copy(update={"conflict_scope": None}) for record in incoming.adrs],
    })
    assert repository.reconcile_knowledge(
        accepted_without_scope, revision, root=tmp_path, apply=True,
    )[1]
    for record in incoming.adrs:
        repository.set_adr_conflict_scope(record.key, record.conflict_scope)
    decision = ADRResolutionDecision(
        action="coexist", resolved_by="writer", resolved_at="2026-11-23T18:30:00Z",
        rationale="Keep both authentication methods.",
    )
    unresolved = deepcopy(repository.database)
    resolution = repository.resolve_adr_conflict("first", "second", decision)
    approved = repository.export_knowledge(
        incoming.project_key, incoming.repository_key, revision, root=tmp_path,
    )
    repository.__dict__["database"] = unresolved
    invalid = approved.model_copy(update={
        "coexistence": [approved.coexistence[0].model_copy(update={"topic": "wrong topic"})],
    })
    with pytest.raises(ArchitectureGraphError, match="unresolved conflicts"):
        repository.reconcile_knowledge(invalid, "b" * 40, root=tmp_path, apply=True)
    assert repository.find_adr_conflicts(ADRConflictQuery(key="first"))
    drift, applied = repository.reconcile_knowledge(approved, "b" * 40, root=tmp_path, apply=True)
    assert applied
    assert [item.record for item in drift] == ["coexistence/first/second"]
    assert repository.get_adr_conflict_resolution("first", "second") == resolution


def test_knowledge_round_trip_preserves_legacy_coexistence_across_repositories(
    repository, overlapping_knowledge, knowledge_source, monkeypatch, tmp_path,
):
    incoming = overlapping_knowledge
    project, source, _ = knowledge_source
    other = Repository(key="other", name="Other", role="source")
    repository.database.collection("architecture_entities").insert(
        repository.architecture._entity_payload(other),
    )
    monkeypatch.setattr(
        ArangoArchitectureRepository, "project_source_repositories", lambda self, key: [source, other],
    )
    monkeypatch.setattr(ArangoArchitectureRepository, "project_access_keys", lambda self: [project.key])
    first, second = incoming.adrs
    other_snapshot = KnowledgeSnapshot.from_database(
        project.key, other.key, "b" * 40,
        [second.model_copy(update={"conflict_scope": None})], None,
    )
    assert repository.reconcile_knowledge(
        other_snapshot, other_snapshot.source_revision, root=tmp_path, apply=True,
    )[1]
    repository.set_adr_conflict_scope(second.key, second.conflict_scope)
    revision = incoming.source_revision
    clean = deepcopy(repository.database)
    repository.database.collection("memory_documents").insert(repository._document_payload(first))
    decision = ADRResolutionDecision(
        action="coexist", resolved_by="writer", resolved_at="2026-11-23T18:30:00Z",
        rationale="Keep both authentication methods.",
    )
    resolution = repository.resolve_adr_conflict("first", "second", decision)
    payload = resolution.model_dump(exclude={"topic", "namespace"})
    repository.__dict__["database"] = deepcopy(clean)
    dependency = repository._coexistence_dependency(
        repository.architecture, repository.database.collection("memory_documents"), second.key,
    )
    approved = KnowledgeSnapshot.from_database(
        incoming.project_key, incoming.repository_key, revision, [first], None,
        [SnapshotCoexistence(**payload, dependency=dependency)],
    )
    assert repository.reconcile_knowledge(approved, revision, root=tmp_path, apply=True)[1]
    edges = repository.database.collection("memory_edges")
    edge, = edges.documents_by_key.values()
    del edge["topic"], edge["namespace"]
    assert repository.find_adr_conflicts(ADRConflictQuery(key="first")) == []
    assert repository.resolve_adr_conflict("second", "first", decision).topic is None

    git(tmp_path, "init", "-q")

    def commit(message, *parents):
        tree = git(tmp_path, "write-tree").decode().strip()
        return git(
            tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit-tree", tree, *parents, "-m", message,
        ).decode().strip()

    source_revision = commit("Source revision")
    monkeypatch.setattr("app.repositories.arango_repository.verify_checkout", verify_checkout)
    exported = repository.export_knowledge(
        incoming.project_key, incoming.repository_key, source_revision, root=tmp_path,
    )
    assert [record.key for record in exported.adrs] == [first.key]
    assert exported.coexistence[0].dependency.adr_key == second.key
    snapshot_path = tmp_path / SNAPSHOT_PATH
    snapshot_path.parent.mkdir()
    snapshot_path.write_text(json.dumps(exported.portable_json()))
    git(tmp_path, "add", SNAPSHOT_PATH)
    snapshot_revision = commit("Approved snapshot", "-p", source_revision)
    repository.__dict__["database"] = clean
    restored = KnowledgeSnapshot.model_validate_json(
        git(tmp_path, "cat-file", "blob", f"{snapshot_revision}:{SNAPSHOT_PATH}"),
    )
    assert repository.reconcile_knowledge(restored, snapshot_revision, root=tmp_path, apply=True)[1]
    assert repository.find_adr_conflicts(ADRConflictQuery(key="first")) == []
    edges = repository.database.collection("architecture_edges")
    for record, owner in ((first, source), (second, other)):
        link, = edges.find({"target_key": architecture_key("memory", "adr", record.key)})
        assert link["source_key"] == owner.key
    assert repository.reconcile_knowledge(
        restored, snapshot_revision, root=tmp_path, apply=False,
    ) == ([], False)


def test_knowledge_round_trip_preserves_repository_ownership(
    repository, knowledge_source, monkeypatch, tmp_path,
):
    project, source, _ = knowledge_source
    other = Repository(key="other", name="Other", role="source")
    repository.database.collection("architecture_entities").insert(
        repository.architecture._entity_payload(other),
    )
    monkeypatch.setattr(
        ArangoArchitectureRepository, "project_source_repositories", lambda self, key: [source, other],
    )
    architecture = repository.architecture
    revision = "a" * 40

    def seed(key, owner=None, repository_name=None):
        adr = ArchitecturalDecisionRecord(
            key=key, title=key, content=key, decision=key, rationale="why",
            consequences="impact", date="2026-11-23", status="accepted",
            next_review_due="2027-02-21",
        )
        node = ArchitectureADR(
            key=architecture_key("memory", "adr", key),
            memory_key=key, name=key, repository=repository_name,
        )
        database = repository.database
        database.collection("memory_documents").insert(repository._document_payload(adr))
        database.collection("architecture_entities").insert(architecture._entity_payload(node))
        if owner:
            link = ArchitectureLink(
                source_key=owner, target_key=node.key, relationship_type="defines", source="test",
            )
            database.collection("architecture_edges").insert(
                architecture._edge_payload(link, "2026-11-23T18:00:00Z"),
            )
        return node.key

    seed("foreign", other.key)
    seed("foreign-indexed", project.key, other.name)
    destination = deepcopy(repository.database)
    local = seed("local", source.key)
    indexed = seed("local-indexed", repository_name=source.name)
    seed("shared", project.key)
    for source_key, target_key, relation in (
        (project.key, local, "related_to"),
        (indexed, project.key, "references"),
    ):
        link = ArchitectureLink(
            source_key=source_key, target_key=target_key, relationship_type=relation, source="test",
        )
        repository.database.collection("architecture_edges").insert(
            architecture._edge_payload(link, "2026-11-23T18:00:00Z"),
        )
    exported = repository.export_knowledge(project.key, source.key, revision, root=tmp_path)
    assert [adr.key for adr in exported.adrs] == ["local", "local-indexed"]
    restored = KnowledgeSnapshot.model_validate(exported.portable_json())
    repository.__dict__["database"] = destination
    assert repository.reconcile_knowledge(restored, revision, root=tmp_path, apply=True)[1]
    edges = repository.database.collection("architecture_edges")
    link, = edges.find({"target_key": local, "relationship_type": "defines"})
    assert link["source_key"] == source.key
    assert list(edges.find({"target_key": indexed})) == []
    related_source, related_target = sorted((project.key, local))
    assert list(edges.find({
        "source_key": related_source, "target_key": related_target, "relationship_type": "related_to",
    }))
    assert list(edges.find({
        "source_key": indexed, "target_key": project.key, "relationship_type": "references",
    }))
    assert repository.database.collection("architecture_entities").get(indexed)["repository"] == source.name
    assert repository.reconcile_knowledge(restored, revision, root=tmp_path, apply=False) == ([], False)
    edges.delete(link["_key"])
    edges.insert(architecture._edge_payload(
        ArchitectureLink(
            source_key=project.key, target_key=local, relationship_type="defines", source="test",
        ),
        "2026-11-23T18:00:00Z",
    ))
    drift, applied = repository.reconcile_knowledge(restored, revision, root=tmp_path, apply=True)
    assert not applied
    assert [(item.record, item.status) for item in drift] == [("adr/local", "occupied")]


def test_knowledge_round_trip_references_foreign_coexistence(
    repository, overlapping_knowledge, monkeypatch, tmp_path,
):
    incoming = overlapping_knowledge
    local, foreign = incoming.adrs
    revision = incoming.source_revision
    foreign_project = "other-project"
    repository.database.collection("architecture_entities").insert(
        repository.architecture._entity_payload(Project(key=foreign_project, name="Other project")),
    )
    monkeypatch.setattr(
        ArangoArchitectureRepository, "project_access_keys",
        lambda self: [incoming.project_key, foreign_project],
    )
    architecture = repository.architecture

    def seed(adr, project):
        database = repository.database
        database.collection("memory_documents").insert(repository._document_payload(adr))
        node = ArchitectureADR(
            key=architecture_key("memory", "adr", adr.key),
            memory_key=adr.key, name=adr.title,
        )
        database.collection("architecture_entities").insert(architecture._entity_payload(node))
        link = ArchitectureLink(
            source_key=project, target_key=node.key, relationship_type="defines", source="test",
        )
        database.collection("architecture_edges").insert(
            architecture._edge_payload(link, "2026-11-23T18:00:00Z"),
        )

    seed(foreign, foreign_project)
    destination = deepcopy(repository.database)
    seed(local, incoming.repository_key)
    resolution = repository.resolve_adr_conflict(foreign.key, local.key, ADRResolutionDecision(
        action="coexist", resolved_by="writer", resolved_at="2026-11-23T18:30:00Z",
        rationale="Use separate authentication methods for each set of clients.",
    ))
    exported = repository.export_knowledge(
        incoming.project_key, incoming.repository_key, revision, root=tmp_path,
    )
    payload = exported.portable_json()
    assert [item["key"] for item in payload["adrs"]] == [local.key]
    assert foreign.content not in json.dumps(payload)
    dependency = payload["coexistence"][0]["dependency"]
    assert dependency["adr_key"] == foreign.key
    assert dependency["project_keys"] == [foreign_project]

    restored = KnowledgeSnapshot.model_validate(payload)
    repository.__dict__["database"] = deepcopy(destination)
    for field, value, match in (
        ("dependency", {**dependency, "fingerprint": "0" * 64}, "ownership or evidence"),
        ("dependency", {**dependency, "project_keys": ["wrong-project"]}, "ownership or evidence"),
        ("overlap_start", "2026-11-02", "unresolved conflicts"),
        ("system", "wrong-system", "unresolved conflicts"),
        ("topic", "wrong-topic", "unresolved conflicts"),
        ("namespace", "wrong-namespace", "unresolved conflicts"),
    ):
        invalid = deepcopy(payload)
        invalid["coexistence"][0][field] = value
        with pytest.raises(ArchitectureGraphError, match=match):
            repository.reconcile_knowledge(
                KnowledgeSnapshot.model_validate(invalid), revision, root=tmp_path, apply=True,
            )
        assert repository.read_document(local.key) is None
        with pytest.raises(ADRError, match="No resolution recorded"):
            repository.get_adr_conflict_resolution(local.key, foreign.key)

    assert repository.reconcile_knowledge(restored, revision, root=tmp_path, apply=True)[1]
    assert repository.get_adr_conflict_resolution(local.key, foreign.key) == resolution
    assert repository.reconcile_knowledge(restored, revision, root=tmp_path, apply=True) == ([], False)
    assert repository.database.collection("memory_documents").get(foreign.key) == (
        destination.collection("memory_documents").get(foreign.key)
    )


def test_ensure_storage_ready_checks_collections_and_views(repository: ArangoMemoryRepository) -> None:
    repository.ensure_storage_ready()

    assert repository.database.properties_count == 1


def test_ensure_storage_ready_creates_missing_collections_and_views(repository: ArangoMemoryRepository) -> None:
    del repository.database.collections["memory_vectors"]
    del repository.database.views["memory_vectors_view"]

    repository.ensure_storage_ready()

    assert ("memory_vectors", False) in repository.database.created_collections
    assert repository.database.views["memory_vectors_view"] is True
    assert {name for name, _ in repository.database.created_views} == {"memory_search", "memory_vectors_view"}


def test_write_then_read_document(repository: ArangoMemoryRepository) -> None:
    document = MemoryDocument(key="memory-1", title="Boot Log", content="Storage is ready")

    written = repository.write_document(document)
    loaded = repository.read_document("memory-1")

    assert written.key == "memory-1"
    assert loaded is not None
    assert loaded.title == "Boot Log"
    assert loaded.content == "Storage is ready"
    assert repository.database.collections["memory_documents"].inserted[0]["title"] == "Boot Log"


def test_write_document_mirrors_embeddings_to_vector_collection(repository: ArangoMemoryRepository) -> None:
    document = MemoryDocument(
        key="memory-2",
        title="Vectorized",
        content="Stored with an embedding",
        embedding=[0.1, 0.2, 0.3],
        embedding_provider="deterministic",
        embedding_model="deterministic-hash",
        embedding_dimension=3,
        embedding_version="1",
    )

    repository.write_document(document)

    assert repository.database.collections["memory_vectors"].inserted[0]["embedding_provider"] == "deterministic"


def test_ensure_storage_ready_uses_edge_collection_for_relationships(repository: ArangoMemoryRepository) -> None:
    del repository.database.collections["memory_edges"]

    repository.ensure_storage_ready()

    assert ("memory_edges", True) in repository.database.created_collections


@pytest.mark.parametrize("operation", ["search_documents", "semantic_search", "list_documents", "find_related_entities"])
def test_aql_binds_match_query(repository, operation):
    class AQL:
        def execute(self, query, bind_vars):
            declared = {name.removeprefix("@") for name in re.findall(r"@@?\w+", query)}
            assert set(bind_vars) == declared
            if operation in {"semantic_search", "search_documents"}:
                assert bind_vars["as_of"] == "2026-11-21"
                assert query.index("FILTER @as_of") < query.index("LIMIT @limit")
            if operation == "semantic_search":
                assert bind_vars["embedding_provider"] == "ollama"
                assert bind_vars["embedding_model"] == "test-model"
                assert bind_vars["embedding_version"] == "2"
            return []

    repository.database.aql = AQL()
    if operation == "list_documents":
        assert repository.list_documents() == []
    elif operation == "find_related_entities":
        assert repository.find_related_entities(GraphTraversalRequest(seed_entity_id="memory_documents/m1")) == []
    else:
        request = MemorySearchRequest(
            query="test", as_of=date(2026, 11, 21), embedding=[0.1, 0.2], embedding_provider="ollama",
            embedding_model="test-model", embedding_version="2",
        )
        assert getattr(repository, operation)(request) == []


def test_reembedding_uses_existing_arango_key_and_preserves_other_fields(repository):
    documents = repository.database.collections["memory_documents"]
    vectors = repository.database.collections["memory_vectors"]
    documents.documents_by_key["legacy-id"] = {
        "_key": "legacy-id", "key": "old-app-key", "title": "Legacy",
        "content": "Existing content", "rank": 42,
    }

    document = repository.read_document("legacy-id")
    assert document.key == "legacy-id"
    for _ in range(2):
        document = repository.update_document_embedding(document.model_copy(update={"embedding": [0.5]}))
        repository.upsert_vector_document(document)

    assert list(documents.documents_by_key) == ["legacy-id"]
    assert list(vectors.documents_by_key) == ["legacy-id"]
    assert documents.documents_by_key["legacy-id"]["rank"] == 42
    assert documents.documents_by_key["legacy-id"]["embedding"] == [0.5]


@pytest.mark.asyncio
async def test_adr_mcp_lifecycle_and_graph_edge(repository, monkeypatch):
    settings = Settings(_env_file=None, auth_required=False)
    ingestion = MemoryIngestionService(repository)
    search = MemorySearchService(repository)
    server = build_mcp_server(settings, search, StaticKnowledgeBase(), ingestion_service=ingestion)

    class AQL:
        def execute(self, query, bind_vars):
            if "@@edges" in query:
                edges = repository.database.collection(bind_vars["@edges"])
                for edge in edges.documents_by_key.values():
                    if edge["_from"] == bind_vars["seed"]:
                        target = repository.read_adr(edge["_to"].split("/", 1)[1])
                        yield {"entity_id": edge["_to"], "label": target.title, "relation": edge["relation"]}
            else:
                documents = repository.database.collection(bind_vars["@documents"])
                for row in documents.documents_by_key.values():
                    if row["kind"] in bind_vars["kinds"]:
                        yield {**row, "score": 1.0}

    repository.database.aql = AQL()
    async with Client(server) as client:
        draft = {
            "key": "adr-a",
            "title": "Store decision records",
            "decision": "Use ArangoDB",
            "rationale": "Reuse the graph database",
            "consequences": "The ADR data shares the memory collection",
            "date": "2026-11-21",
            "effective_date": "2026-11-21",
            "last_validated": "2026-11-21",
            "confidence": 0.9,
            "tags": ["Security"],
        }
        created = await client.call_tool("create_adr", {"record": draft})
        assert created.structured_content["status"] == "proposed"
        assert created.structured_content["next_review_due"] == "2026-12-21"
        accepted = await client.call_tool("set_adr_status", {"key": "adr-a", "status": "accepted"})
        assert accepted.structured_content["conflict_check"]["status"] == "missing_scope"
        await client.call_tool("create_adr", {
            "record": {**draft, "key": "adr-b", "title": "Replacement", "status": "accepted",
                       "effective_date": "2026-12-01"},
        })
        with monkeypatch.context() as patch:
            def fail_update(*args, **kwargs):
                raise RuntimeError("status update failed")

            patch.setattr(FakeCollection, "update", fail_update)
            with pytest.raises(RuntimeError, match="status update failed"):
                repository.supersede_adr("adr-a", "adr-b")
        assert repository.read_adr("adr-a").status == "accepted"
        assert not repository.database.collection("memory_edges").documents_by_key

        link = await client.call_tool("supersede_adr", {"source_key": "adr-a", "target_key": "adr-b"})
        assert link.structured_content == {
            "source_id": "memory_documents/adr-a",
            "target_id": "memory_documents/adr-b",
            "relation": "superseded_by",
        }
        result = await client.call_tool("get_adr", {"key": "adr-a"})
        assert result.structured_content["status"] == "superseded"
        assert result.structured_content["date"] == "2026-11-21"
        assert result.structured_content["expiration_date"] == "2026-12-01"
        assert result.structured_content["last_validated"] == "2026-11-21"
        assert result.structured_content["confidence"] == 0.9

        hits = await client.call_tool("search_adrs", {"query": "ArangoDB"})
        assert json.loads(hits.content[0].text)[0]["document"]["rationale"] == draft["rationale"]
        related = await client.call_tool("find_related_entities", {"entity_id": link.structured_content["source_id"]})
        assert json.loads(related.content[0].text)[0]["entity_id"] == link.structured_content["target_id"]
        assert json.loads(related.content[0].text)[0]["relation"] == "superseded_by"

    with pytest.raises(ADRError, match="cannot supersede itself"):
        repository.supersede_adr("adr-b", "adr-b")
    repository.supersede_adr("adr-a", "adr-b")
    assert len(repository.database.collection("memory_edges").documents_by_key) == 1
    with pytest.raises(ADRError, match="must be accepted"):
        repository.supersede_adr("adr-b", "adr-a")


@pytest.mark.parametrize("fields", [
    {"confidence": -0.1},
    {"confidence": 1.1},
    {"effective_date": "2026-12-01", "expiration_date": "2026-12-01"},
])
def test_memory_rejects_invalid_temporal_fields(fields):
    with pytest.raises(ValueError):
        MemoryDocument(title="Authentication", content="Use OIDC", **fields)


@pytest.mark.asyncio
async def test_revalidation_records_writer_and_evidence_without_changing_expiration(
    repository, monkeypatch,
):
    settings = Settings(_env_file=None, auth_required=False)
    ingestion = MemoryIngestionService(repository)
    server = build_mcp_server(
        settings, MemorySearchService(repository), StaticKnowledgeBase(), ingestion_service=ingestion,
    )
    security_adr = {
        "key": "security-adr",
        "title": "Authentication",
        "decision": "Use OIDC",
        "rationale": "Use the central identity provider",
        "consequences": "Clients need an identity token",
        "tags": ["Security"],
        "date": "2026-01-01",
        "effective_date": "2026-01-01",
        "expiration_date": "2026-10-01",
        "confidence": 0.95,
        "status": "accepted",
    }
    repository.write_document(MemoryDocument(
        key="general", title="General memory", content="A reference", tags=["security"],
    ))
    claims = {"subject": "alice", "raw": {"roles": ["reader"]}}
    evidence = [" https://example.com/authentication/decision "]
    async with Client(server) as client:
        await client.call_tool("create_adr", {"record": security_adr})
        args = {"key": "security-adr", "evidence": evidence}
        with pytest.raises(ToolError, match="authenticated writer or admin"):
            await client.call_tool("revalidate_memory", args)

        request = SimpleNamespace(scope={"state": {"auth_claims": claims}})
        monkeypatch.setattr("app.mcp.tools.auth.get_http_request", lambda: request)
        with pytest.raises(ToolError, match="authenticated writer or admin"):
            await client.call_tool("revalidate_memory", args)
        assert not repository.read_adr("security-adr").validation_history

        claims["raw"] = {"realm_access": {"roles": ["writer"]}}
        with pytest.raises(ToolError):
            await client.call_tool("revalidate_memory", {**args, "evidence": [" "]})

        first = (await client.call_tool("revalidate_memory", args)).structured_content
        assert first["validation"]["reviewer"] == "alice"
        assert first["validation"]["evidence"] == [evidence[0].strip()]
        assert first["validation"]["previous_confidence"] == 0.95
        assert first["validation"]["new_confidence"] == 0.95
        checked_on = date.fromisoformat(first["last_validated"])
        assert first["validation"]["checked_at"].startswith(checked_on.isoformat())
        assert date.fromisoformat(first["next_review_due"]) == checked_on + timedelta(days=30)

        claims.update(subject="bob", raw={"roles": ["admin"]})
        with pytest.raises(ToolError):
            await client.call_tool("revalidate_memory", {**args, "confidence": 1.1})
        changed = (await client.call_tool("revalidate_memory", {
            **args, "evidence": ["repo@abc123:auth.py"], "confidence": 0,
        })).structured_content
        assert changed["validation"]["previous_confidence"] == 0.95
        assert changed["validation"]["new_confidence"] == 0
        assert changed["validation"]["evidence"] == ["repo@abc123:auth.py"]
        general = (await client.call_tool(
            "revalidate_memory", {"key": "general", "evidence": ["repo@abc123:README.md"]},
        )).structured_content
        assert date.fromisoformat(general["next_review_due"]) == (
            date.fromisoformat(general["last_validated"]) + timedelta(days=90)
        )

    stored = repository.read_adr("security-adr")
    assert [entry.reviewer for entry in stored.validation_history] == ["alice", "bob"]
    assert stored.effective_date == date(2026, 1, 1)
    assert stored.expiration_date == date(2026, 10, 1)
    assert stored.status == "accepted"
    assert stored.confidence == 0
    assert stored.validation_history[0].new_confidence == 0.95
    assert stored.validation_history[1].previous_confidence == 0.95
    assert stored.validation_history[1].new_confidence == 0
    mirror = repository.database.collection("memory_vectors").get(stored.key)
    assert mirror["expiration_date"] == "2026-10-01"
    assert mirror["confidence"] == 0
    assert len(mirror["validation_history"]) == 2


@pytest.mark.asyncio
async def test_adr_conflict_tool_returns_both_records_with_evidence(repository, monkeypatch):
    settings = Settings(_env_file=None, auth_required=False)
    server = build_mcp_server(
        settings, MemorySearchService(repository), StaticKnowledgeBase(),
        ingestion_service=MemoryIngestionService(repository),
    )
    draft = {
        "key": "original", "title": "OIDC for the customer API",
        "decision": "Use OIDC", "rationale": "Central identity",
        "consequences": "Clients need tokens", "status": "proposed", "date": "2026-01-01",
        "effective_date": "2026-01-01", "expiration_date": "2026-12-01",
    }
    scopes = {"topic": "  Authentication ", "system": "Customer API", "environment": "Production"}

    class AQL:
        def execute(self, query, bind_vars):
            assert set(bind_vars) == {
                name.removeprefix("@") for name in re.findall(r"@@?\w+", query)
            }
            if "LENGTH(node.suggestions)" in query:
                return []
            if "last_validation: LAST" in query:
                assert bind_vars == {
                    "@documents": "memory_documents", "as_of": "2026-11-21",
                    "limit": 5, "offset": 3, "document_keys": None,
                }
                return [{
                    "key": "due", "title": "Overdue validation", "kind": "memory",
                    "next_review_due": "2026-11-20",
                }]
            if "LET original = DOCUMENT(edge._from)" in query:
                assert bind_vars["document_prefix"] == "memory_documents/"
                assert bind_vars["system"] == "customer api"
                assert bind_vars["environment"] == "production"
                assert bind_vars["as_of"] == "2026-11-21"
                assert bind_vars["limit"] == 5
                assert bind_vars["offset"] == 2
                resolution, = repository.database.collection("memory_edges").documents_by_key.values()
                return [{
                    "original": repository.read_adr("original").model_dump(mode="json"),
                    "competing": repository.read_adr("competing").model_dump(mode="json"),
                    "overlap_start": resolution["overlap_start"],
                    "overlap_end": resolution["overlap_end"],
                    "resolution": resolution,
                }]
            if bind_vars["key"] == "competing":
                assert bind_vars["as_of"] is None
                return []
            assert "edge.topic == original.conflict_scope.topic" in query
            assert "edge.topic == null AND edge.namespace == null" in query
            assert 'edge.namespace == NOT_NULL(original.namespace, "default")' in query
            assert "edge.system == NOT_NULL(original_system, system)" in query
            assert "edge.environment == NOT_NULL(original_environment, environment)" in query
            assert "edge.overlap_start == overlap_start" in query
            assert "edge.overlap_end == overlap_end" in query
            if bind_vars["competing_key"] and repository.database.collection("memory_edges").has(
                repository._resolution_edge_key("memory_documents/original", "memory_documents/competing"),
            ):
                return []
            if bind_vars["key"] is None:
                assert (bind_vars["system"], bind_vars["environment"]) in {
                    ("customer api", "production"), (None, None),
                }
                assert bind_vars["offset"] == 2
                assert bind_vars["limit"] == 5
                assert "original._key < doc._key" in query
            else:
                assert bind_vars["key"] == "original"
            assert bind_vars["as_of"] in {None, "2026-11-21"}
            competing = repository.read_adr("competing")
            original = repository.read_adr("original").model_copy(update={"status": "accepted"})
            return [{
                "original": original.model_dump(mode="json"),
                "competing": competing.model_dump(mode="json"),
                "overlap_start": "2026-11-01", "overlap_end": "2026-12-01",
            }]

    repository.database.aql = AQL()
    async with Client(server) as client:
        proposed = await client.call_tool("create_adr", {"record": draft})
        assert proposed.structured_content["conflict_check"] is None
        await client.call_tool("set_adr_conflict_scope", {"key": "original", "scope": scopes})
        direct = await client.call_tool("create_adr", {"record": {
            **draft, "key": "competing", "title": "mTLS for the customer API", "status": "accepted",
            "decision": "Use mTLS", "effective_date": "2026-11-01", "expiration_date": None,
            "conflict_scope": scopes,
        }})
        assert direct.structured_content["conflict_check"] == {
            "status": "checked", "conflicts": [], "has_more": False,
        }
        for key in ("original", "competing"):
            repository.revalidate_memory(key, MemoryValidation(
                reviewer="test-writer", checked_at="2026-11-21T14:00:00Z",
                evidence=[f"repo@abc123:docs/{key}.md"],
            ))

        with monkeypatch.context() as patch:
            def fail_query(*args, **kwargs):
                raise RuntimeError("conflict query failed")

            patch.setattr(repository.database.aql, "execute", fail_query)
            with pytest.raises(RuntimeError, match="conflict query failed"):
                repository.set_adr_status("original", "accepted")
        assert repository.read_adr("original").status == "proposed"

        accepted = await client.call_tool("set_adr_status", {"key": "original", "status": "accepted"})
        assert accepted.structured_content["status"] == "accepted"
        conflict_check = accepted.structured_content["conflict_check"]
        assert conflict_check["status"] == "checked"
        assert conflict_check["has_more"] is False

        result = await client.call_tool("find_adr_conflicts", {
            "key": "original", "as_of": "2026-11-21",
        })
        conflicts = result.structured_content["result"]
        assert conflicts == conflict_check["conflicts"]
        listed = await client.call_tool("list_adr_conflicts", {
            "system": " Customer API ", "environment": "PRODUCTION",
            "as_of": "2026-11-21", "limit": 5, "offset": 2,
        })
        assert listed.structured_content["result"] == conflicts
        queue = (await client.call_tool("get_review_queue", {
            "as_of": "2026-11-21", "limit": 5, "validation_offset": 3, "conflict_offset": 2,
        })).structured_content
        assert queue["review_date"] == "2026-11-21"
        assert queue["overdue_validations"][0]["key"] == "due"
        assert queue["unresolved_adr_conflicts"] == conflicts
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert conflict["overlap_start"] == "2026-11-01"
        assert conflict["overlap_end"] == "2026-12-01"
        for side in ("original", "competing"):
            assert conflict[side]["validation_history"][0]["evidence"] == [
                f"repo@abc123:docs/{side}.md"
            ]
            assert "embedding" not in conflict[side]

        before = await client.call_tool("find_adr_conflicts", {
            "key": "original", "as_of": "2025-12-31",
        })
        assert before.structured_content["result"] == []

        args = {
            "source_key": "original", "target_key": "competing", "action": "coexist",
            "rationale": " OIDC covers user traffic. mTLS covers service traffic. ",
        }
        with pytest.raises(ToolError, match="authenticated writer or admin"):
            await client.call_tool("resolve_adr_conflict", args)
        claims = {"subject": "writer-1", "raw": {"roles": ["writer"]}}
        request = SimpleNamespace(scope={"state": {"auth_claims": claims}})
        monkeypatch.setattr("app.mcp.tools.auth.get_http_request", lambda: request)
        resolution = (await client.call_tool("resolve_adr_conflict", args)).structured_content
        assert resolution["resolved_by"] == "writer-1"
        assert resolution["resolved_at"].endswith("Z")
        assert resolution["rationale"] == args["rationale"].strip()
        assert resolution["overlap_start"] == "2026-11-01"
        assert resolution["overlap_end"] == "2026-12-01"
        retry = await client.call_tool("resolve_adr_conflict", {
            **args, "source_key": "competing", "target_key": "original",
        })
        assert retry.structured_content == resolution
        with pytest.raises(ToolError, match="different resolution"):
            await client.call_tool("resolve_adr_conflict", {**args, "action": "supersede"})
        saved = await client.call_tool("get_adr_conflict_resolution", {
            "source_key": "competing", "target_key": "original",
        })
        assert saved.structured_content == resolution
        resolved = await client.call_tool("list_resolved_adr_conflicts", {
            "system": " Customer API ", "environment": "PRODUCTION",
            "as_of": "2026-11-21", "limit": 5, "offset": 2,
        })
        pair, = resolved.structured_content["result"]
        assert pair["original"]["key"] == "original"
        assert pair["competing"]["key"] == "competing"
        assert pair["overlap_start"] == "2026-11-01"
        assert pair["overlap_end"] == "2026-12-01"
        assert pair["resolution"] == resolution
        assert resolution["system"] == "customer api"
        assert resolution["environment"] == "production"
        assert repository.read_adr("original").status == "accepted"
        assert repository.read_adr("competing").status == "accepted"


def test_adr_conflict_scope_requires_a_system_or_environment():
    with pytest.raises(ValueError, match="must name a system or environment"):
        ADRConflictScope(topic="authentication")
