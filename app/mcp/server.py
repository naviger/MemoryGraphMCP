from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from app.auth.project_access import ProjectAccessDenied, current_project_access
from app.config.settings import Settings
from app.mcp.prompts.templates import register_prompts
from app.mcp.resources.catalog import register_resources
from app.mcp.tools.adrs import register_adr_tools
from app.mcp.tools.architecture import register_architecture_tools
from app.mcp.tools.architecture_events import register_event_tools
from app.mcp.tools.context_usage import register_context_usage
from app.mcp.tools.effectiveness import register_effectiveness_tools
from app.mcp.tools.revalidation import register_revalidation_tool
from app.mcp.tools.review_queue import register_review_queue_tool
from app.mcp.tools.search import register_search_tools
from app.services.architecture_service import ArchitectureService
from app.services.context_package import ContextPackageService
from app.services.effectiveness import EffectivenessService
from app.services.ingestion_service import MemoryIngestionService
from app.services.knowledge_base import StaticKnowledgeBase
from app.services.review_queue import ReviewQueueService
from app.services.search_service import MemorySearchService
from app.services.token_usage import TokenUsageService


class ProjectAccessMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        if current_project_access.get() is None:
            raise ToolError("Project access is required")
        try:
            return await call_next(context)
        except ProjectAccessDenied as exc:
            raise ToolError(str(exc)) from exc


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
                "Use build_context_package for compact, question-specific graph evidence. "
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
    if settings.project_authorization_required:
        server.add_middleware(ProjectAccessMiddleware())
    register_resources(server, knowledge_base)
    register_search_tools(server, service)
    if architecture_service is not None:
        register_architecture_tools(
            server, architecture_service, auth_required=settings.auth_required,
        )
        if hasattr(architecture_service.repository, "record_usage"):
            register_context_usage(
                server, ContextPackageService(architecture_service.repository, architecture_service.memory),
                TokenUsageService(architecture_service.repository),
            )
        if hasattr(architecture_service.repository, "effectiveness_graph"):
            register_effectiveness_tools(server, EffectivenessService(
                architecture_service.repository, architecture_service.memory,
            ))
    if (
        architecture_service is not None and settings.auth_required
        and settings.graph_event_producer_subject and settings.graph_event_project_key
    ):
        register_event_tools(
            server, architecture_service.repository,
            settings.graph_event_producer_subject, settings.graph_event_project_key,
        )
    if ingestion_service is not None:
        register_adr_tools(server, ingestion_service, auth_required=settings.auth_required)
        register_revalidation_tool(server, ingestion_service.repository)
        register_review_queue_tool(server, ReviewQueueService(ingestion_service.repository))
    register_prompts(server)
    return server
