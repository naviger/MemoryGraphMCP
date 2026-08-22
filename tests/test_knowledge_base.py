from __future__ import annotations

from app.services.knowledge_base import StaticKnowledgeBase


def test_architecture_resource_mentions_clean_architecture() -> None:
    knowledge_base = StaticKnowledgeBase()

    architecture = knowledge_base.architecture_markdown()
    security = knowledge_base.security_markdown()

    assert "Clean Architecture" in architecture
    assert "JWT bearer tokens" in security
