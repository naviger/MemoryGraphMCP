from __future__ import annotations

from datetime import date
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from app.domain.models import MemoryKey, ReviewQueue, ReviewQueueQuery
from app.services.review_queue import ReviewQueueService


def register_review_queue_tool(mcp: FastMCP, service: ReviewQueueService) -> None:
    @mcp.tool
    async def get_review_queue(
        as_of: date | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        validation_offset: Annotated[int, Field(ge=0)] = 0,
        project_key: MemoryKey | None = None,
        conflict_offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ReviewQueue:
        """List overdue validations, unresolved ADR conflicts, and pending inferred links."""
        keys = await service.document_keys(project_key) if project_key else None
        return await service.get_queue(
            ReviewQueueQuery(
                as_of=as_of,
                project_key=project_key,
                limit=limit,
                validation_offset=validation_offset,
                conflict_offset=conflict_offset,
            ), keys
        )
