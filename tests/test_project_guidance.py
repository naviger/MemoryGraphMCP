from datetime import UTC, datetime

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from app.config.settings import Settings
from app.domain.architecture import Constraint, Project
from app.domain.project_guidance import GuidanceDecision, GuidanceScope, GuidanceState
from app.mcp.server import build_mcp_server
from app.services.architecture_service import ArchitectureService
from app.services.knowledge_base import StaticKnowledgeBase


async def test_project_guidance_is_read_only_and_uses_project_sources():
    class Graph:
        def read_entity(self, key):
            return Project(key=key, name="Example") if key == "example" else None

        def read_guidance_state(self, key):
            assert key == "example"
            return GuidanceState.model_validate({
                "draft": {"text": "Unapproved draft", "edited_by": "writer",
                          "edited_at": "2026-11-22T10:00:00Z"},
                "published": {"text": "Published instructions", "version": 1,
                              "edited_by": "writer", "approved_by": "admin",
                              "edited_at": "2026-11-22T09:00:00Z",
                              "published_at": "2026-11-22T09:30:00Z"},
            })

        def guidance_scope(self, key):
            assert key == "example"
            return GuidanceScope(entity_keys=["example", "service"], adr_keys=["adr"])

        def guidance_constraints(self, keys):
            assert keys == ["example", "service"]
            return [
                Constraint(
                    key="rule", name="Service boundaries", rule="Use service interfaces",
                    enforcement_status="enforced", test_suite_keys=["test-service"],
                ),
                Constraint(
                    key="old-rule", name="Former rule", rule="Retired",
                    enforcement_status="retired",
                ),
            ]

    class Memory:
        def read_guidance_decisions(self, keys, as_of):
            assert keys == ["adr"]
            assert as_of == datetime.now(UTC).date()
            return [GuidanceDecision(
                record_key="adr", title="Service boundary", decision="Use interfaces",
                rationale="Testability", evidence=["tests/test_service.py"],
            )]

    memory = Memory()
    server = build_mcp_server(
        Settings(_env_file=None, auth_required=False), None, StaticKnowledgeBase(),
        architecture_service=ArchitectureService(Graph(), memory),
    )
    async with Client(server) as client:
        tool = next(tool for tool in await client.list_tools()
                    if tool.name == "get_project_guidance")
        assert tool.annotations.readOnlyHint is True
        result = (await client.call_tool(
            tool.name, {"project_key": "example"},
        )).structured_content
        assert result["published_guidance"]["text"] == "Published instructions"
        assert "Unapproved draft" not in str(result)
        assert [adr["record_key"] for adr in result["decisions"]] == ["adr"]
        assert result["decisions"][0]["evidence"] == ["tests/test_service.py"]
        assert [rule["entity_key"] for rule in result["constraints"]] == ["rule"]
        assert result["constraints"][0]["validating_test_keys"] == ["test-service"]
        with pytest.raises(ToolError, match="Project not found"):
            await client.call_tool(tool.name, {"project_key": "missing"})
