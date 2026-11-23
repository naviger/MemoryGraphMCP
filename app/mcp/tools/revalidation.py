from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from app.domain.exceptions import RevalidationError
from app.domain.models import (
    ConfidenceScore,
    EvidenceCitation,
    MemoryKey,
    MemoryRevalidationResult,
)
from app.mcp.tools.auth import require_writer
from app.repositories.protocol import MemoryRepository
from app.services.review_queue import ReviewQueueService


def register_revalidation_tool(mcp: FastMCP, repository: MemoryRepository) -> None:
    service = ReviewQueueService(repository)

    @mcp.tool
    async def revalidate_memory(
        key: MemoryKey,
        evidence: Annotated[
            list[EvidenceCitation],
            Field(
                min_length=1,
                description="Evidence citations such as source URLs or commit references.",
            ),
        ],
        confidence: ConfidenceScore | None = None,
    ) -> MemoryRevalidationResult:
        """Revalidate with cited evidence, optionally updating confidence on a 0–1 scale."""
        reviewer = require_writer()
        try:
            return await service.revalidate(key, reviewer, evidence, confidence)
        except RevalidationError as exc:
            raise ToolError(str(exc)) from exc
