from __future__ import annotations

from textwrap import dedent


class StaticKnowledgeBase:
    def architecture_markdown(self) -> str:
        return dedent(
            """
            # Architecture

            MemoryGraphMCP follows Clean Architecture:

            - MCP layer adapts MCP requests to application use cases
            - Application layer orchestrates memory retrieval and generation flows
            - Domain layer defines memory documents, search requests, and graph entities
            - Persistence layer implements ArangoDB-backed document, traversal, and vector queries

            Business logic belongs in services. MCP handlers only validate input and delegate.
            """
        ).strip()

    def design_language_markdown(self) -> str:
        return dedent(
            """
            # Design Language

            The project uses deliberate, explicit language around memory retrieval:

            - "memory" means a persisted engineering artifact stored in ArangoDB
            - "architecture" means system-level documentation and decision support material
            - "adr" means an architecture decision record
            - "design language" means implementation tone, naming rules, and UI conventions
            - "domain concept" means a bounded term from the engineering vocabulary
            """
        ).strip()

    def security_markdown(self) -> str:
        return dedent(
            """
            # Security

            - All persistent data lives in ArangoDB
            - MCP HTTP traffic is protected with JWT bearer tokens
            - Authentication is enforced at the transport boundary
            - No secrets should be stored in local files or committed to the repository
            - OpenTelemetry and structured logs should avoid leaking sensitive payloads
            """
        ).strip()

    def domain_model_markdown(self) -> str:
        return dedent(
            """
            # Domain Model

            Core persisted entities:

            - MemoryDocument: a stored unit of engineering knowledge
            - MemorySearchRequest: retrieval filters and search mode
            - MemorySearchHit: a ranked document match
            - RelatedEntity: a graph-related vertex discovered through traversal
            - GraphTraversalRequest: traversal parameters for related-entity discovery
            """
        ).strip()
