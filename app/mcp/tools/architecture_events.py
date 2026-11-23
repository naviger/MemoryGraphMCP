from __future__ import annotations

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.domain.architecture import EntityName
from app.domain.architecture_events import GraphEvent, GraphEventReceipt
from app.domain.exceptions import ArchitectureGraphError
from app.mcp.tools.auth import require_writer
from app.repositories.architecture_protocol import ArchitectureRepository


def register_event_tools(
    mcp: FastMCP, repository: ArchitectureRepository, trusted_producer: str, project_key: str,
) -> None:
    def authorize_producer() -> str:
        source = require_writer()
        if source != trusted_producer:
            raise ToolError("This writer is not the configured graph event producer")
        return source

    @mcp.tool
    async def ingest_graph_event(event: GraphEvent) -> GraphEventReceipt:
        """Apply a project-tracker event once and record its source and affected entities."""
        source = authorize_producer()
        if event.project_key != project_key:
            raise ToolError("Event project does not match the configured tracker project")
        try:
            return await anyio.to_thread.run_sync(
                repository.ingest_event, source, project_key, event,
            )
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_graph_event(event_id: EntityName) -> GraphEventReceipt:
        """Read the stored receipt for an event from the configured producer."""
        source = authorize_producer()
        try:
            return await anyio.to_thread.run_sync(
                repository.get_event, source, project_key, event_id,
            )
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc
