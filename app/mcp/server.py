from __future__ import annotations

from fastmcp import FastMCP

from app.config.settings import Settings
from app.mcp.prompts.templates import register_prompts
from app.mcp.resources.catalog import register_resources
from app.mcp.tools.search import register_search_tools
from app.services.knowledge_base import StaticKnowledgeBase
from app.services.search_service import MemorySearchService


def build_mcp_server(
    settings: Settings,
    service: MemorySearchService,
    knowledge_base: StaticKnowledgeBase,
) -> FastMCP:
    server = FastMCP(
        settings.app_name,
        instructions=(
            "Secure engineering memory retrieval for MCP clients. "
            "Use the provided resources for architecture, design language, security, and domain model guidance."
        ),
        version=settings.app_version,
    )
    register_resources(server, knowledge_base)
    register_search_tools(server, service)
    register_prompts(server)
    return server
