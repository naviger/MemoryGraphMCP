from __future__ import annotations

from fastmcp import FastMCP

from app.config.settings import Settings
from app.mcp.prompts.templates import register_prompts
from app.mcp.resources.catalog import register_resources
from app.mcp.tools.adrs import register_adr_tools
from app.mcp.tools.architecture import register_architecture_tools
from app.mcp.tools.revalidation import register_revalidation_tool
from app.mcp.tools.review_queue import register_review_queue_tool
from app.mcp.tools.search import register_search_tools
from app.services.architecture_service import ArchitectureService
from app.services.ingestion_service import MemoryIngestionService
from app.services.knowledge_base import StaticKnowledgeBase
from app.services.review_queue import ReviewQueueService
from app.services.search_service import MemorySearchService


def build_mcp_server(
    settings: Settings,
    service: MemorySearchService,
    knowledge_base: StaticKnowledgeBase,
    architecture_service: ArchitectureService | None = None,
    ingestion_service: MemoryIngestionService | None = None,
) -> FastMCP:
    server = FastMCP(
        settings.app_name,
        instructions=(
            "Secure engineering memory retrieval for MCP clients. "
            "Use the provided resources for architecture, security, and domain guidance. "
            + (
                "Resolve the project key with search_architecture_entities, then call "
                "get_project_guidance for its current ADRs and constraints. "
                "Follow the repository's own instructions."
                if architecture_service is not None else ""
            )
            + (
                "Use as_of for time-scoped search and cite relevant records. "
                "Check ADR conflicts before treating a decision as authoritative. "
                "Report uncertainty when records are undated, stale, or conflicting. "
                if ingestion_service is not None else ""
            )
        ),
        version=settings.app_version,
    )
    register_resources(server, knowledge_base)
    register_search_tools(server, service)
    if architecture_service is not None:
        register_architecture_tools(
            server, architecture_service, auth_required=settings.auth_required,
        )
    if ingestion_service is not None:
        register_adr_tools(server, ingestion_service, auth_required=settings.auth_required)
        register_revalidation_tool(server, ingestion_service.repository)
        register_review_queue_tool(server, ReviewQueueService(ingestion_service.repository))
    register_prompts(server)
    return server
