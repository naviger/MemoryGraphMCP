from __future__ import annotations

from fastmcp import FastMCP

from app.services.prompt_builder import (
    build_arangodb_repository_prompt,
    build_fastapi_service_prompt,
    build_react_component_prompt,
)


def register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt
    def generate_fastapi_service(service_name: str, endpoint_goal: str, domain_notes: str | None = None) -> str:
        """Generate a FastAPI service implementation brief."""
        return build_fastapi_service_prompt(service_name, endpoint_goal, domain_notes)

    @mcp.prompt
    def generate_react_component(component_name: str, purpose: str, style_notes: str | None = None) -> str:
        """Generate a React component implementation brief."""
        return build_react_component_prompt(component_name, purpose, style_notes)

    @mcp.prompt
    def generate_arangodb_repository(
        repository_name: str,
        entity_name: str,
        storage_notes: str | None = None,
    ) -> str:
        """Generate an ArangoDB repository implementation brief."""
        return build_arangodb_repository_prompt(repository_name, entity_name, storage_notes)
