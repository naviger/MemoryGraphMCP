from __future__ import annotations

from datetime import date
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from app.domain.models import ReviewQueue, ReviewQueueQuery
from app.services.review_queue import ReviewQueueService


def register_review_queue_tool(mcp: FastMCP, service: ReviewQueueService) -> None:
    @mcp.tool
    async def get_review_queue(
        as_of: date | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        validation_offset: Annotated[int, Field(ge=0)] = 0,
        conflict_offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ReviewQueue:
        """List overdue validations and unresolved ADR conflicts for writer review."""
        return await service.get_queue(
            ReviewQueueQuery(
                as_of=as_of,
                limit=limit,
                validation_offset=validation_offset,
                conflict_offset=conflict_offset,
            )
        )
