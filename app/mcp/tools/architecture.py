from __future__ import annotations

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from app.domain.architecture import (
    ArchitectureEntity,
    ArchitectureIndexResult,
    ArchitectureLink,
    ArchitecturePath,
    ArchitectureRelationship,
    ArchitectureSearch,
    ArchitectureTrace,
    Constraint,
    ConstraintInput,
    ConversationSummary,
    ConversationSummaryInput,
    EntityName,
    Goal,
    Observation,
    ObservationInput,
    ProjectGoalInput,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import MemoryKey
from app.domain.project_guidance import ProjectGuidance
from app.mcp.tools.auth import require_writer
from app.services.architecture_service import ArchitectureService


def register_architecture_tools(
    mcp: FastMCP,
    service: ArchitectureService,
    *,
    auth_required: bool,
) -> None:
    repository = service.repository

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_project_guidance(project_key: MemoryKey) -> ProjectGuidance:
        """Read published project guidance, current accepted ADRs, and active constraints.

        Use the explicit Project key. Follow repository instructions and use these records as
        evidence for project decisions. Read the cited ADR or graph entity for more detail.
        """
        try:
            return await service.get_project_guidance(project_key)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def add_observation(observation: ObservationInput) -> Observation:
        """Record an agent observation with evidence, confidence, and related graph entities."""
        recorded_by = require_writer() if auth_required else None
        try:
            return await service.add_observation(observation, recorded_by)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def save_architectural_constraint(constraint: ConstraintInput) -> Constraint:
        """Save an architecture rule and its constrained entities and validating test suites."""
        if auth_required:
            require_writer()
        try:
            return await service.save_architectural_constraint(constraint)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def save_project_goal(goal: ProjectGoalInput) -> Goal:
        """Save a project goal and its complete set of supporting ADR, service, and task links."""
        if auth_required:
            require_writer()
        try:
            return await service.save_project_goal(goal)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def add_conversation_summary(
        conversation: ConversationSummaryInput,
    ) -> ConversationSummary:
        """Record a concise conversation summary and link the ADRs, risks, and tasks it surfaced."""
        recorded_by = require_writer() if auth_required else None
        try:
            return await service.add_conversation_summary(conversation, recorded_by)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def index_python_architecture(
        repository_name: EntityName,
        files: dict[str, str],
    ) -> ArchitectureIndexResult:
        """Index a complete Python source snapshot supplied as relative paths and source text."""
        if auth_required:
            require_writer()
        try:
            return await service.index_python(repository_name, files)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def put_architecture_entity(entity: ArchitectureEntity) -> ArchitectureEntity:
        """Create or update a first-class architecture entity by key and type."""
        if auth_required:
            require_writer()
        try:
            return await anyio.to_thread.run_sync(repository.put_entity, entity)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def import_architecture_memory(key: MemoryKey) -> ArchitectureEntity:
        """Add an existing ADR or domain concept to the graph using its memory key."""
        if auth_required:
            require_writer()
        try:
            return await service.import_memory(key)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def link_architecture_entities(link: ArchitectureLink) -> ArchitectureRelationship:
        """Link two graph entities with a typed relationship, confidence, and cited source."""
        if auth_required:
            require_writer()
        try:
            return await anyio.to_thread.run_sync(repository.put_link, link)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def search_architecture_entities(request: ArchitectureSearch) -> list[ArchitectureEntity]:
        """Find graph entities by name, qualified name, source path, or linked memory key."""
        return await anyio.to_thread.run_sync(repository.search, request)

    @mcp.tool
    async def trace_architecture(request: ArchitectureTrace) -> list[ArchitecturePath]:
        """Follow typed graph relationships in either direction and return paths with provenance."""
        try:
            return await anyio.to_thread.run_sync(repository.trace, request)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc
