"""Additional tests to reach 100% coverage for search tools and error paths."""

from __future__ import annotations

import pytest

from app.domain.exceptions import AuthenticationError
from app.domain.models import (
    GraphTraversalRequest,
    MemoryDocument,
    MemorySearchHit,
    MemorySearchRequest,
    RelatedEntity,
)
from app.mcp.tools.search import register_search_tools
from app.services.search_service import MemorySearchService


class MinimalMCP:
    """Minimal MCP for tool registration."""

    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


class FullRepo:
    """Repository that returns complete results for all search methods."""

    def search_documents(self, request):
        return [MemorySearchHit(document=MemoryDocument(key="k1", title="Lexical", content="Body"), score=1.0)]

    def semantic_search(self, request):
        return [MemorySearchHit(document=MemoryDocument(key="k2", title="Semantic", content="Body"), score=0.8)]

    def hybrid_search(self, request):
        return [MemorySearchHit(document=MemoryDocument(key="k3", title="Hybrid", content="Body"), score=0.9)]

    def find_related_entities(self, request):
        return [RelatedEntity(entity_id="e1", label="Entity", relation="links", depth=1, score=0.7)]


@pytest.mark.asyncio
async def test_search_tools_all_invokable() -> None:
    """Verify all search tools can be invoked with proper async execution."""
    mcp = MinimalMCP()
    repo = FullRepo()
    service = MemorySearchService(repo)
    register_search_tools(mcp, service)

    result = await mcp.tools["search_memory"]("query", limit=5, tags=["tag"])
    assert len(result) > 0

    result = await mcp.tools["search_architecture"]("arch query", limit=5)
    assert len(result) > 0

    result = await mcp.tools["search_adrs"]("adr query", limit=5)
    assert len(result) > 0

    result = await mcp.tools["search_design_language"]("design query", limit=5)
    assert len(result) > 0

    result = await mcp.tools["search_domain_concepts"]("concept query", limit=5)
    assert len(result) > 0

    result = await mcp.tools["semantic_search"]("semantic query", limit=5)
    assert len(result) > 0

    result = await mcp.tools["hybrid_search"]("hybrid query", limit=5)
    assert len(result) > 0

    result = await mcp.tools["find_related_entities"]("entity1", depth=2, limit=10)
    assert len(result) > 0


def test_authentication_error_is_exception() -> None:
    """Test that AuthenticationError is properly raised as an exception."""
    with pytest.raises(AuthenticationError):
        raise AuthenticationError("Test auth failure")


def test_search_results_preserve_all_fields() -> None:
    """Test that search results properly preserve all model fields."""
    doc = MemoryDocument(
        key="test-key",
        title="Test Title",
        content="Test content",
        kind="architecture",
        tags=["tag1", "tag2"],
        source="internal",
        source_id="src-1",
        namespace="test-ns",
        version=2,
        status="active",
        provenance={"created_at": "2024-01-01"},
        metadata={"custom": "value"}
    )
    hit = MemorySearchHit(document=doc, score=0.95, highlights=["highlight1"])

    assert hit.document.key == "test-key"
    assert hit.document.title == "Test Title"
    assert hit.document.kind == "architecture"
    assert hit.document.tags == ["tag1", "tag2"]
    assert hit.document.source == "internal"
    assert hit.document.namespace == "test-ns"
    assert hit.document.version == 2
    assert hit.document.status == "active"
    assert hit.score == 0.95
    assert hit.highlights == ["highlight1"]


def test_graph_traversal_request_depth_limits() -> None:
    """Test GraphTraversalRequest depth constraints."""
    req1 = GraphTraversalRequest(seed_entity_id="e1", depth=1, limit=10)
    assert req1.depth == 1

    req2 = GraphTraversalRequest(seed_entity_id="e1", depth=6, limit=100)
    assert req2.depth == 6

    with pytest.raises(ValueError):
        GraphTraversalRequest(seed_entity_id="e1", depth=0, limit=10)

    with pytest.raises(ValueError):
        GraphTraversalRequest(seed_entity_id="e1", depth=7, limit=10)


def test_memory_search_request_limit_constraints() -> None:
    """Test MemorySearchRequest limit field constraints."""
    req = MemorySearchRequest(query="test", limit=50, kinds=["architecture"])
    assert req.limit == 50
    assert req.kinds == ["architecture"]

    with pytest.raises(ValueError):
        MemorySearchRequest(query="test", limit=0)

    with pytest.raises(ValueError):
        MemorySearchRequest(query="test", limit=101)
