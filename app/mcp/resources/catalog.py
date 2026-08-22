from __future__ import annotations

from fastmcp import FastMCP

from app.services.knowledge_base import StaticKnowledgeBase


def register_resources(mcp: FastMCP, knowledge_base: StaticKnowledgeBase) -> None:
    @mcp.resource("memory://architecture")
    def architecture() -> str:
        return knowledge_base.architecture_markdown()

    @mcp.resource("memory://design-language")
    def design_language() -> str:
        return knowledge_base.design_language_markdown()

    @mcp.resource("memory://security")
    def security() -> str:
        return knowledge_base.security_markdown()

    @mcp.resource("memory://domain-model")
    def domain_model() -> str:
        return knowledge_base.domain_model_markdown()
