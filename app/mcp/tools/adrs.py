from __future__ import annotations

from datetime import date
from typing import Annotated

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from app.domain.exceptions import ADRError
from app.domain.models import (
    ADRConflict,
    ADRConflictListQuery,
    ADRConflictQuery,
    ADRConflictResolution,
    ADRConflictScope,
    ADRCreate,
    ADRResolutionAction,
    ADRResolutionRationale,
    ADRStatus,
    ADRSupersession,
    ADRWriteResult,
    ArchitecturalDecisionRecord,
    MemoryKey,
    ResolvedADRConflict,
    ScopeLabel,
)
from app.mcp.tools.auth import require_writer
from app.services.ingestion_service import MemoryIngestionService
from app.services.review_queue import ReviewQueueService


def register_adr_tools(
    mcp: FastMCP, ingestion: MemoryIngestionService, *, auth_required: bool,
) -> None:
    repository = ingestion.repository
    review = ReviewQueueService(repository)

    @mcp.tool
    async def create_adr(record: ADRCreate) -> ADRWriteResult:
        """Create an ADR. An accepted ADR includes an automatic conflict report for the writer."""
        if auth_required:
            require_writer()
        try:
            return await ingestion.ingest_adr(record)
        except ADRError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def get_adr(key: MemoryKey) -> ArchitecturalDecisionRecord:
        """Read a structured architectural decision record by its key."""
        try:
            return await anyio.to_thread.run_sync(repository.read_adr, key)
        except ADRError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def set_adr_status(key: MemoryKey, status: ADRStatus) -> ADRWriteResult:
        """Accept or reject a proposed ADR. Acceptance includes a conflict report for the writer."""
        if auth_required:
            require_writer()
        try:
            return await anyio.to_thread.run_sync(repository.set_adr_status, key, status)
        except ADRError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def supersede_adr(source_key: MemoryKey, target_key: MemoryKey) -> ADRSupersession:
        """Link an accepted ADR to its accepted replacement and mark the original superseded."""
        if auth_required:
            require_writer()
        try:
            return await anyio.to_thread.run_sync(repository.supersede_adr, source_key, target_key)
        except ADRError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def set_adr_conflict_scope(
        key: MemoryKey, scope: ADRConflictScope | None,
    ) -> ArchitecturalDecisionRecord:
        """Set or clear an ADR's decision topic and system or environment for conflict checks."""
        if auth_required:
            require_writer()
        try:
            return await anyio.to_thread.run_sync(repository.set_adr_conflict_scope, key, scope)
        except ADRError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def find_adr_conflicts(
        key: MemoryKey,
        as_of: date | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[ADRConflict]:
        """Find potentially competing ADRs with overlapping validity and return both records and citations."""
        request = ADRConflictQuery(key=key, as_of=as_of, limit=limit)
        try:
            return await anyio.to_thread.run_sync(repository.find_adr_conflicts, request)
        except ADRError as exc:
            raise ToolError(str(exc)) from exc


    @mcp.tool
    async def list_adr_conflicts(
        system: ScopeLabel | None = None,
        environment: ScopeLabel | None = None,
        as_of: date | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> list[ADRConflict]:
        """List unresolved ADR pairs, optionally filtered by system, environment, or date."""
        request = ADRConflictListQuery(
            system=system, environment=environment, as_of=as_of, limit=limit, offset=offset,
        )
        return await anyio.to_thread.run_sync(repository.list_adr_conflicts, request)

    @mcp.tool
    async def list_resolved_adr_conflicts(
        system: ScopeLabel | None = None,
        environment: ScopeLabel | None = None,
        as_of: date | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> list[ResolvedADRConflict]:
        """List resolved ADR pairs and their audit records. Filter by the original conflict date."""
        request = ADRConflictListQuery(
            system=system, environment=environment, as_of=as_of, limit=limit, offset=offset,
        )
        return await anyio.to_thread.run_sync(repository.list_resolved_adr_conflicts, request)

    @mcp.tool
    async def resolve_adr_conflict(
        source_key: MemoryKey, target_key: MemoryKey,
        action: ADRResolutionAction, rationale: ADRResolutionRationale,
    ) -> ADRConflictResolution:
        """Resolve a flagged conflict. For supersede, the target ADR replaces the source ADR."""
        reviewer = require_writer()
        try:
            return await review.resolve_conflict(source_key, target_key, reviewer, action, rationale)
        except ADRError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def get_adr_conflict_resolution(
        source_key: MemoryKey, target_key: MemoryKey,
    ) -> ADRConflictResolution:
        """Read the recorded resolution for an ADR pair in either order."""
        try:
            return await anyio.to_thread.run_sync(
                repository.get_adr_conflict_resolution, source_key, target_key,
            )
        except ADRError as exc:
            raise ToolError(str(exc)) from exc
