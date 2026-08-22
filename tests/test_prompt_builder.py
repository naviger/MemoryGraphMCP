from __future__ import annotations

from app.services.prompt_builder import (
    build_arangodb_repository_prompt,
    build_fastapi_service_prompt,
    build_react_component_prompt,
)


def test_build_fastapi_service_prompt_includes_goal() -> None:
    prompt = build_fastapi_service_prompt("MemoryService", "Expose search endpoints")

    assert "MemoryService" in prompt
    assert "Expose search endpoints" in prompt
    assert "FastAPI" in prompt


def test_build_react_component_prompt_includes_purpose() -> None:
    prompt = build_react_component_prompt("MemoryCard", "Display memory snippets")

    assert "MemoryCard" in prompt
    assert "Display memory snippets" in prompt
    assert "React component" in prompt


def test_build_arango_repository_prompt_includes_entity() -> None:
    prompt = build_arangodb_repository_prompt("MemoryRepository", "memory document")

    assert "MemoryRepository" in prompt
    assert "memory document" in prompt
    assert "ArangoDB" in prompt
