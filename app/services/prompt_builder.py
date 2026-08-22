from __future__ import annotations

from textwrap import dedent


def build_fastapi_service_prompt(service_name: str, endpoint_goal: str, domain_notes: str | None = None) -> str:
    notes = f"\n\nDomain notes:\n{domain_notes.strip()}" if domain_notes else ""
    return dedent(
        f"""
        Generate a production-ready FastAPI service named {service_name}.

        Requirements:
        - Use Python 3.13+
        - Follow Clean Architecture
        - Separate routers, schemas, services, and persistence concerns
        - Include typed Pydantic v2 models
        - Add structured logging, validation, and health endpoints
        - Keep the implementation concise but production-safe

        Goal:
        {endpoint_goal}{notes}
        """
    ).strip()


def build_react_component_prompt(component_name: str, purpose: str, style_notes: str | None = None) -> str:
    notes = f"\n\nStyle notes:\n{style_notes.strip()}" if style_notes else ""
    return dedent(
        f"""
        Generate a modern React component named {component_name}.

        Requirements:
        - Prefer typed props and small composable helpers
        - Use accessible markup and clear loading/error states
        - Keep styling intentional and visually distinct
        - Avoid unnecessary abstraction

        Purpose:
        {purpose}{notes}
        """
    ).strip()


def build_arangodb_repository_prompt(repository_name: str, entity_name: str, storage_notes: str | None = None) -> str:
    notes = f"\n\nStorage notes:\n{storage_notes.strip()}" if storage_notes else ""
    return dedent(
        f"""
        Generate an ArangoDB repository named {repository_name} for the {entity_name} domain.

        Requirements:
        - Use python-arango
        - Support document retrieval, graph traversal, and vector search
        - Keep query construction isolated from application logic
        - Include explicit bind variables and typed return models

        Target entity:
        {entity_name}{notes}
        """
    ).strip()
