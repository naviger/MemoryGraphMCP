from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.domain.effectiveness import (
    DevelopmentOutcome,
    EffectivenessQuery,
    EffectivenessReport,
    OutcomeReceipt,
)
from app.domain.exceptions import ArchitectureGraphError
from app.mcp.tools.auth import require_writer
from app.services.effectiveness import EffectivenessService


def register_effectiveness_tools(mcp: FastMCP, service: EffectivenessService) -> None:
    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_engineering_effectiveness(query: EffectivenessQuery) -> EffectivenessReport:
        """Summarize engineering evidence and cost estimates for a project, repository, and period."""
        try:
            return await service.summary(query)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def report_development_outcome(outcome: DevelopmentOutcome) -> OutcomeReceipt:
        """Record one cited change and its delivery outcomes. Use a stable change ID for retries."""
        writer = require_writer()
        try:
            return await service.report(outcome, writer)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc
