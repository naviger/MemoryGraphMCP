from __future__ import annotations

from datetime import date
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from app.services.search_service import MemorySearchService

AsOfDate = Annotated[date, Field(description="Date on which the memory must apply, as YYYY-MM-DD.")]


def register_search_tools(mcp: FastMCP, service: MemorySearchService) -> None:
    @mcp.tool
    async def search_memory(query: str, limit: int = 10, tags: list[str] | None = None, as_of: AsOfDate | None = None,
    ):
        """Search the general memory corpus."""
        return await service.search_memory(query=query, limit=limit, tags=tags, as_of=as_of)

    @mcp.tool
    async def search_architecture(query: str, limit: int = 10, as_of: AsOfDate | None = None):
        """Search architecture-oriented memory."""
        return await service.search_architecture(query=query, limit=limit, as_of=as_of)

    @mcp.tool
    async def search_adrs(query: str, limit: int = 10, as_of: AsOfDate | None = None):
        """Search architecture decision records."""
        return await service.search_adrs(query=query, limit=limit, as_of=as_of)

    @mcp.tool
    async def search_design_language(query: str, limit: int = 10, as_of: AsOfDate | None = None):
        """Search design-language memory."""
        return await service.search_design_language(query=query, limit=limit, as_of=as_of)

    @mcp.tool
    async def search_domain_concepts(query: str, limit: int = 10, as_of: AsOfDate | None = None):
        """Search domain-concept memory."""
        return await service.search_domain_concepts(query=query, limit=limit, as_of=as_of)

    @mcp.tool
    async def semantic_search(query: str, limit: int = 10, embedding: list[float] | None = None, as_of: AsOfDate | None = None,
    ):
        """Run vector-backed semantic search when embeddings are available."""
        return await service.semantic_search(query=query, limit=limit, embedding=embedding, as_of=as_of)

    @mcp.tool
    async def hybrid_search(query: str, limit: int = 10, embedding: list[float] | None = None, as_of: AsOfDate | None = None,
    ):
        """Combine lexical and vector search for a broader retrieval result."""
        return await service.hybrid_search(query=query, limit=limit, embedding=embedding, as_of=as_of)

    @mcp.tool
    async def find_related_entities(entity_id: str, depth: int = 2, limit: int = 20):
        """Traverse the knowledge graph around a memory entity."""
        return await service.find_related_entities(entity_id=entity_id, depth=depth, limit=limit)
