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
from app.domain.context_usage import ContextQuery, UsageOutcome
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import ArchitecturalDecisionRecord
from app.mcp.tools.context_usage import register_context_usage
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

    graph = SimpleNamespace(
        read_entity=nodes.get, project_scope=lambda key: SimpleNamespace(entities=[
            nodes["assets"], nodes["adr"], nodes["test-suite"],
        ]), search=lambda request: list(nodes.values()), trace=trace,
        find_memory_entities=lambda key: [nodes["adr"]] if key == "record" else [],
    )
    return ContextPackageService(graph, SimpleNamespace(read_document=lambda key: record if key == "record" else None))


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
    with pytest.raises(ArchitectureGraphError, match="selected scope"):
        await context_service.build(query.model_copy(update={"service": "out-of-project"}))


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
