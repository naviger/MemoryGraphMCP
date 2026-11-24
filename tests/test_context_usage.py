from datetime import date
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from app.domain.architecture import (
    ArchitectureADR,
    ArchitecturePath,
    ArchitectureRelationship,
    Project,
    Service,
    TestSuite,
)
from app.domain.ask_graph import AskGraphQuery
from app.domain.context_usage import ContextQuery, UsageOutcome
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import (
    ADRConflict,
    ADRConflictRecord,
    ADRConflictScope,
    ArchitecturalDecisionRecord,
    MemoryDocument,
)
from app.domain.review_details import ProjectEntity, ProjectScope
from app.mcp.tools.context_usage import register_context_usage
from app.services.ask_graph import AskGraphService
from app.services.context_package import ContextPackageService
from app.services.token_usage import TokenUsageService, estimate_tokens


@pytest.fixture
def context_service():
    nodes = {
        "project": Project(key="project", name="Project"),
        "assets": Service(key="assets", name="AssetService"),
        "adr": ArchitectureADR(key="adr", name="Asset identity", memory_key="record"),
        "test-suite": TestSuite(key="test-suite", name="Asset tests", source_path="tests/test_asset.py"),
        "out-of-project": Service(key="out-of-project", name="Outside"),
    }
    record = ArchitecturalDecisionRecord(
        key="record", title="Asset identity", status="accepted",
        content="Private appendix sentence. " * 100, decision="Use canonical asset IDs.",
        rationale="Avoid duplicate identifiers.", consequences="Consumers must migrate.",
        date="2026-01-01", effective_date="2026-02-01",
    )

    def trace(request):
        if request.key != "assets":
            return []
        return [
            ArchitecturePath(
                entity=nodes[key], nodes=[nodes["assets"], nodes[key]],
                relationships=[ArchitectureRelationship(
                    source_key="assets", target_key=key, relationship_type=relationship,
                    source="indexed source", created_at="2026-02-10T00:00:00Z",
                    updated_at="2026-02-10T00:00:00Z",
                )],
            )
            for key, relationship in (("adr", "implements"), ("test-suite", "validated_by"),
                                      ("out-of-project", "uses"))
        ]

    scope = ProjectScope(entities=[
        ProjectEntity(
            key=node.key, name=node.name, entity_type=node.entity_type,
            memory_key=getattr(node, "memory_key", None), reason="Project link",
        )
        for node in (nodes["assets"], nodes["adr"], nodes["test-suite"])
    ], dependencies=[], repository_names=[])
    graph = SimpleNamespace(
        read_entity=nodes.get, project_scope=lambda key: scope,
        search=lambda request: list(nodes.values()), trace=trace,
        find_memory_entities=lambda key: [nodes["adr"]] if key == "record" else [],
    )
    return ContextPackageService(graph, SimpleNamespace(
        read_document=lambda key: record if key == "record" else None,
        project_documents=lambda *args: [record],
        search_question_documents=lambda *args, **kwargs: [],
    ))


@pytest.mark.asyncio
async def test_context_package_links_scope_date_and_hard_estimate_budget(context_service):
    query = ContextQuery(question="How does AssetService work?", project_key="project",
                         as_of=date(2026, 2, 10), max_context_tokens=400)
    package, baseline, count = await context_service.build(query)
    assert 0 < estimate_tokens(package) <= query.max_context_tokens
    assert baseline > estimate_tokens(package)
    assert count == 3
    assert package.relevant_adrs[0].text == "Use canonical asset IDs."
    assert package.relevant_tests[0].key == "test-suite"
    assert [node.key for node in package.relevant_services] == ["assets"]
    assert any("indexed source" in evidence.citation for evidence in package.evidence)
    assert "Private appendix" not in package.model_dump_json()

    old = await context_service.build(query.model_copy(update={"as_of": date(2026, 1, 1)}))
    assert old.package.relevant_adrs == []
    expanded = await context_service.build(query.model_copy(update={
        "include_full_adrs": True, "max_context_tokens": 4000, "adr": "record",
    }))
    assert "Private appendix" in expanded.package.relevant_adrs[0].text
    tight = await context_service.build(query.model_copy(update={
        "include_full_adrs": True, "max_context_tokens": 256,
    }))
    assert estimate_tokens(tight.package) <= 256
    assert tight.package.budget_excluded["relevant_adrs"] == 1
    with pytest.raises(ArchitectureGraphError, match="selected scope"):
        await context_service.build(query.model_copy(update={"service": "out-of-project"}))


@pytest.mark.asyncio
async def test_blends_project_documents_and_dated_conflict_counterparts(context_service):
    memory = context_service.memory
    linked = memory.read_document("record")
    linked.conflict_scope = ADRConflictScope(topic="identity", system="assets")
    unlinked = linked.model_copy(update={
        "key": "unlinked", "title": "Storage names", "conflict_scope": None,
        "decision": "Use storage names in exported asset inventory reports. " * 4,
    })
    start, end = date(2026, 3, 1), date(2026, 4, 1)
    counterpart = linked.model_copy(update={
        "key": "counterpart", "title": "External identifiers", "decision": "Use external IDs.",
        "effective_date": start, "expiration_date": end, "source_id": "evidence:external-ids",
    })
    documents = {record.key: record for record in (linked, unlinked, counterpart)}
    memory.project_documents = lambda *args: list(documents.values())
    memory.read_document = documents.get

    def search(terms, as_of, limit, *, document_keys, adrs_only):
        assert terms == "asset work" and adrs_only
        assert set(document_keys) == documents.keys()
        return [linked, unlinked]

    def find_conflicts(request, *, allowed_document_keys):
        assert request.key == linked.key
        assert set(allowed_document_keys) == documents.keys()
        if not start <= request.as_of < end:
            return []
        return [ADRConflict(
            original=ADRConflictRecord.model_validate(linked.model_dump()),
            competing=ADRConflictRecord.model_validate(counterpart.model_dump()),
            overlap_start=start, overlap_end=end,
        )]

    memory.search_question_documents = search
    memory.find_adr_conflicts = find_conflicts
    query = ContextQuery(question="How does AssetService work?", project_key="project", as_of=start)
    package, _, _ = await context_service.build(query)
    assert [item.key for item in package.relevant_adrs] == ["record", "counterpart", "unlinked"]
    assert package.conflicts[0].overlap_end == end
    assert any(item.citation == "evidence:external-ids" for item in package.evidence)
    assert package.budget_excluded == {}

    all_adrs = package.model_copy(update={
        "relevant_services": [], "relevant_tests": [],
        "evidence": [item for item in package.evidence if item.key in documents],
        "budget_excluded": {"relevant_services": 1, "relevant_tests": 1},
    })
    pair_only = all_adrs.model_copy(update={
        "relevant_adrs": all_adrs.relevant_adrs[:2],
        "evidence": [item for item in all_adrs.evidence if item.key != unlinked.key],
        "budget_excluded": {**all_adrs.budget_excluded, "relevant_adrs": 1},
    })
    without_conflict = all_adrs.model_copy(update={
        "conflicts": [], "budget_excluded": {**all_adrs.budget_excluded, "conflicts": 1},
    })
    budget = max(256, estimate_tokens(pair_only), estimate_tokens(without_conflict))
    assert budget < estimate_tokens(all_adrs)
    constrained, _, _ = await context_service.build(query.model_copy(update={
        "max_context_tokens": budget,
    }))
    assert constrained.conflicts == package.conflicts
    assert constrained.relevant_adrs == pair_only.relevant_adrs
    assert constrained.budget_excluded["relevant_adrs"] == 1
    assert estimate_tokens(constrained) <= budget

    why, _, _ = await context_service.build(query.model_copy(update={
        "question": "Why does AssetService work?",
    }))
    assert why.conflicts == package.conflicts
    excerpts = {item.key: item.text for item in why.relevant_adrs}
    for record in (linked, counterpart):
        assert record.decision in excerpts[record.key]
        assert record.rationale in excerpts[record.key]
    assert excerpts[unlinked.key] == unlinked.rationale

    shared_opening = "When reconciling imported records, apply the same ownership rules. " * 6
    linked.decision = shared_opening + "Use canonical asset IDs."
    counterpart.decision = shared_opening + "Use external IDs."
    why_long, _, _ = await context_service.build(query.model_copy(update={
        "question": "Why does AssetService work?",
    }))
    conflict = why_long.conflicts[0]
    assert conflict.original_key == linked.key
    assert conflict.competing_key == counterpart.key
    assert "Use canonical asset IDs." in conflict.original_decision_excerpt
    assert "Use external IDs." in conflict.competing_decision_excerpt
    assert {linked.key, counterpart.key} <= {item.key for item in why_long.evidence}

    for question in ("How does AssetService work?", "Why does AssetService work?"):
        linked.decision = ("Asset ownership. " + shared_opening * 12
                           + "Imported identifiers. " + shared_opening * 12
                           + "Use canonical asset IDs.")
        counterpart.decision = ("Asset ownership: " + shared_opening * 12
                                + "Imported identifiers: " + shared_opening * 12
                                + "Use external IDs.")
        divergent, _, _ = await context_service.build(query.model_copy(update={"question": question}))
        conflict = divergent.conflicts[0]
        assert "Use canonical asset IDs." in conflict.original_decision_excerpt
        assert "Use external IDs." in conflict.competing_decision_excerpt
        assert {linked.key, counterpart.key} <= {item.key for item in divergent.evidence}
        assert estimate_tokens(divergent) <= query.max_context_tokens

    linked.decision = "Use canonical asset IDs."
    counterpart.decision = "Use external IDs."

    for day in (date(2026, 2, 28), end):
        dated, _, _ = await context_service.build(query.model_copy(update={"as_of": day}))
        assert dated.conflicts == []
        assert [item.key for item in dated.relevant_adrs] == ["record", "unlinked"]

    tight_query = query.model_copy(update={"max_context_tokens": 256, "include_full_adrs": True})
    tight, _, _ = await context_service.build(tight_query)
    assert estimate_tokens(tight) <= tight_query.max_context_tokens
    assert tight.budget_excluded["relevant_adrs"] == 3
    assert tight.budget_excluded["conflicts"] == 1
    assert tight.relevant_adrs == tight.conflicts == []


@pytest.mark.asyncio
async def test_ask_graph_keeps_document_findings_when_graph_matches(context_service):
    memory = context_service.memory
    linked = memory.read_document("record")
    note = MemoryDocument(key="note", title="Asset note", content="Asset IDs persist across moves.")

    def search(*args, **kwargs):
        return [linked] if kwargs.get("adrs_only") else [linked, note]

    memory.search_question_documents = search
    answer = await AskGraphService(memory, SimpleNamespace(), context_service).answer(
        AskGraphQuery(question="How does AssetService work?", as_of=date(2026, 2, 10)),
    )
    findings = [(item.text, [cite.key for cite in item.citations]) for item in answer.findings]
    assert findings == [
        ("Use canonical asset IDs.", ["record"]),
        ("Asset IDs persist across moves.", ["note"]),
    ]


@pytest.mark.asyncio
async def test_mcp_context_returns_usage_and_records_outcome(context_service):
    saved = {}

    class Storage:
        def record_usage(self, receipt, owner, session):
            saved[receipt.request_id] = receipt

        def read_usage(self, request_id, owner):
            return saved.get(request_id)

        def report_usage(self, request_id, owner, outcome):
            record = saved[request_id]
            assert outcome == UsageOutcome(completed=True, follow_up_retries=1, response_tokens=12)
            record.outcome = outcome
            return record

    server = FastMCP("context-test")
    register_context_usage(server, context_service, TokenUsageService(Storage()))
    async with Client(server) as client:
        result = await client.call_tool("build_context_package", {"query": {
            "question": "How does AssetService work?", "service": "assets",
        }})
        receipt = result.structured_content["usage"]
        assert receipt["method"] == "ContextPackage" and receipt["entities_supplied"] >= 3
        assert receipt["response_tokens"] is None
        request_id = receipt["request_id"]
        outcome = await client.call_tool("report_retrieval_outcome", {"request_id": request_id, "outcome": {
            "completed": True, "follow_up_retries": 1, "response_tokens": 12,
        }})
        assert outcome.structured_content["outcome"]["follow_up_retries"] == 1
