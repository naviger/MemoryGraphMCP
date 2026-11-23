"""End-to-end restore checks against a disposable ArangoDB server."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from arango import ArangoClient

from app.domain.architecture import (
    ArchitectureADR,
    ArchitectureLink,
    Project,
    Repository,
    architecture_key,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import ADRConflictQuery, ADRResolutionDecision, ArchitecturalDecisionRecord
from app.repositories.arango_repository import ArangoMemoryRepository, ArangoRepositoryConfig
from app.repositories.knowledge_git import SNAPSHOT_PATH, git, read_snapshot, resolve_commit

PROJECT = "snapshot-project"
SHARED_PROJECT = "shared-project"
REPOSITORIES = ("repo-alpha", "repo-beta")
ADR_KEYS = ("adr-alpha", "adr-beta")


@pytest.fixture
def databases():
    host = os.getenv("MGMCP_RECOVERY_TEST_HOST")
    if not host:
        pytest.skip("Requires a disposable ArangoDB server")
    password = os.environ["MGMCP_RECOVERY_TEST_PASSWORD"]
    system = ArangoClient(hosts=host).db("_system", username="root", password=password)
    names = ["snapshot_" + uuid4().hex for _ in range(2)]
    repositories = []
    try:
        for name in names:
            system.create_database(name)
            repo = ArangoMemoryRepository(
                ArangoRepositoryConfig(
                    host,
                    name,
                    "root",
                    password,
                    "memory_documents",
                    "memory_edges",
                    "memory_vectors",
                    "memory_search",
                    "memory_vectors_view",
                )
            )
            repo.ensure_storage_ready()
            repositories.append(repo)
        yield tuple(repositories)
    finally:
        for name in names:
            if system.has_database(name):
                system.delete_database(name)


def commit(root: Path, message: str, *, allow_empty=False) -> str:
    flags = ["--allow-empty"] if allow_empty else []
    git(
        root,
        "-c",
        "user.name=Snapshot Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        *flags,
        "-m",
        message,
    )
    return resolve_commit(root, "HEAD")


def adr_node(key):
    return architecture_key("memory", "adr", key)


def define(graph, owner, item):
    graph.put_link(
        ArchitectureLink(
            source_key=owner,
            target_key=item,
            relationship_type="defines",
            source="fixture",
        )
    )


def prepare(databases, tmp_path, *, coexist=False, share=False):
    source, destination = databases
    roots = {}
    source_revisions = {}
    for key in REPOSITORIES:
        root = tmp_path / key
        root.mkdir()
        git(root, "init", "-q")
        roots[key] = root
        source_revisions[key] = commit(root, "Source revision", allow_empty=True)
    for db in databases:
        graph = db.architecture
        graph.put_entity(Project(key=PROJECT, name="Snapshot project"))
        graph.put_entity(Project(key=SHARED_PROJECT, name="Shared project"))
        for key in REPOSITORIES:
            graph.put_entity(
                Repository(
                    key=key,
                    name=key,
                    role="source",
                    metadata={
                        "git_checkout": str(roots[key]),
                    },
                )
            )
            define(graph, PROJECT, key)
    for key, owner in zip(ADR_KEYS, REPOSITORIES, strict=True):
        record = ArchitecturalDecisionRecord(
            key=key,
            title=key,
            content=key,
            decision=key,
            rationale="Fixture rationale",
            consequences="Fixture consequences",
            status="accepted",
            date="2026-01-01",
            effective_date="2026-01-01",
            conflict_scope={"topic": "auth", "system": "service"} if coexist else None,
        )
        source.create_adr(record)
        source.architecture.put_entity(ArchitectureADR(key=adr_node(key), memory_key=key, name=key))
        define(source.architecture, owner, adr_node(key))
    if share:
        define(source.architecture, SHARED_PROJECT, adr_node(ADR_KEYS[0]))
    resolution = None
    if coexist:
        resolution = source.resolve_adr_conflict(
            *ADR_KEYS,
            ADRResolutionDecision(
                action="coexist",
                resolved_by="fixture-reviewer",
                resolved_at="2026-02-05T16:00:00Z",
                rationale="Different clients need both authentication strategies.",
            ),
        )
    snapshots = {}
    for key, expected in zip(REPOSITORIES, ADR_KEYS, strict=True):
        root = roots[key]
        snapshot = source.export_knowledge(PROJECT, key, source_revisions[key], root=root)
        assert [record.key for record in snapshot.adrs] == [expected]
        path = root / SNAPSHOT_PATH
        path.parent.mkdir()
        path.write_text(json.dumps(snapshot.portable_json()), encoding="utf-8")
        git(root, "add", SNAPSHOT_PATH)
        revision = commit(root, "Approved snapshot")
        snapshots[key] = (root, revision, read_snapshot(root, revision))
    assert destination.database.collection("memory_documents").count() == 0
    assert destination.database.collection("memory_edges").count() == 0
    return snapshots, resolution


def restore(destination, snapshots, order):
    inputs = [(snapshots[key][2], snapshots[key][1], snapshots[key][0]) for key in order]
    differences, applied = destination.reconcile_knowledge_batch(inputs, apply=True)
    assert applied, differences
    for key in order:
        root, revision, snapshot = snapshots[key]
        assert destination.reconcile_knowledge(snapshot, revision, root=root, apply=False) == (
            [],
            False,
        )


def owners(database, key):
    return {
        edge["source_key"]
        for edge in database.database.collection("architecture_edges").find(
            {
                "target_key": adr_node(key),
                "relationship_type": "defines",
            }
        )
    }


@pytest.mark.parametrize("order", [REPOSITORIES, tuple(reversed(REPOSITORIES))])
def test_two_repository_snapshots_restore_coexistence_into_empty_database(
    databases, tmp_path, order
):
    source, destination = databases
    snapshots, original = prepare(databases, tmp_path, coexist=True)
    restore(destination, snapshots, order)
    for key in ADR_KEYS:
        assert owners(destination, key) == owners(source, key)
        assert destination.find_adr_conflicts(ADRConflictQuery(key=key)) == []
    restored = destination.get_adr_conflict_resolution(*ADR_KEYS)
    assert restored is not None
    assert (restored.resolved_by, restored.resolved_at, restored.rationale) == (
        original.resolved_by,
        original.resolved_at,
        original.rationale,
    )


def test_two_repository_snapshots_preserve_shared_project_ownership(databases, tmp_path):
    source, destination = databases
    snapshots, _ = prepare(databases, tmp_path, share=True)
    restore(destination, snapshots, REPOSITORIES)
    assert owners(source, ADR_KEYS[0]) == {REPOSITORIES[0], SHARED_PROJECT}
    for key in ADR_KEYS:
        assert owners(destination, key) == owners(source, key)
    assert ADR_KEYS[0] in destination.architecture.guidance_scope(SHARED_PROJECT).adr_keys

    edges = destination.database.collection("architecture_edges")
    (shared,) = edges.find({"source_key": SHARED_PROJECT, "target_key": adr_node(ADR_KEYS[0])})
    edges.delete(shared["_key"])
    root, revision, snapshot = snapshots[REPOSITORIES[0]]
    differences, applied = destination.reconcile_knowledge(
        snapshot, revision, root=root, apply=False
    )
    assert not applied
    assert [(item.record, item.status) for item in differences] == [("adr/adr-alpha", "different")]


def test_batch_restore_rolls_back_when_an_owner_is_missing(databases, tmp_path):
    _, destination = databases
    snapshots, _ = prepare(databases, tmp_path, share=True)
    destination.database.collection("architecture_entities").delete(SHARED_PROJECT)
    inputs = [
        (snapshots[key][2], snapshots[key][1], snapshots[key][0]) for key in reversed(REPOSITORIES)
    ]
    with pytest.raises(ArchitectureGraphError, match="invalid repository ownership"):
        destination.reconcile_knowledge_batch(inputs, apply=True)
    assert destination.database.collection("memory_documents").count() == 0
    assert destination.database.collection("memory_edges").count() == 0
    assert all(destination.architecture.read_entity(adr_node(key)) is None for key in ADR_KEYS)
