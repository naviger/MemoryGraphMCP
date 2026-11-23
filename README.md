# MemoryGraphMCP

MemoryGraphMCP is a stateless Model Context Protocol server for secure long-term engineering memory stored in ArangoDB.

It exposes MCP resources, tools, and prompts for retrieval and code generation workflows while keeping all persistent state in ArangoDB.

## What it provides

- MCP resources for architecture, design language, security, and domain model guidance
- Search tools for lexical, semantic, hybrid, and graph-based retrieval
- Prompts for generating FastAPI services, React components, and ArangoDB repositories
- FastAPI hosting with MCP mounted at `/mcp`
- JWT bearer authentication for HTTP access
- OpenTelemetry and structlog instrumentation hooks

## Architecture

The code follows Clean Architecture:

- MCP layer: request/response adapters and component registration
- Application layer: orchestration and use cases
- Domain layer: models, contracts, and policies
- Persistence layer: ArangoDB repository implementations

Business logic stays in services, not in MCP tool handlers.

## Development

The project targets Python 3.13+ and is designed to run with `uv`.

```bash
uv sync --extra dev
uv run pytest
uv run memorygraphmcp
```

## Configuration

Set environment variables with the `MGMCP_` prefix.

Common settings:

- `MGMCP_ARANGO_URL`
- `MGMCP_ARANGO_DATABASE`
- `MGMCP_ARANGO_USERNAME`
- `MGMCP_ARANGO_PASSWORD`
- `MGMCP_ARANGO_ROOT_PASSWORD`
- `MGMCP_JWT_SECRET_KEY`
- `MGMCP_JWT_PUBLIC_KEY`
- `MGMCP_OTEL_EXPORTER_OTLP_ENDPOINT`

If you prefer the existing local `.env` file, the settings loader also accepts the Arango variables already present there:

- `ARANGO_HOST`
- `ARANGO_DATABASE`
- `ARANGO_USERNAME`
- `ARANGO_PASSWORD`

## ArangoDB Enterprise UI setup

Create the storage objects in ArangoDB Enterprise UI before starting the server:

1. Open the database `architecture`.
2. Create a document collection named `memory_documents`.
3. Create an edge collection named `memory_edges`.
4. Create a document collection named `memory_vectors` for vector payloads and embeddings.
5. Create an ArangoSearch view named `memory_search` and link it to `memory_documents`.
6. Create a second ArangoSearch view named `memory_vectors_view` if you want a separate vector-friendly search surface.
7. In the document collection, add the fields you plan to search and traverse, including `title`, `content`, `kind`, `tags`, `source`, and any embedding/vector fields your ingestion pipeline writes.
8. In the edge collection, ensure each edge document has `_from`, `_to`, and `relation` so graph traversal tools can follow relationships.

If your deployment uses a separate security model, keep the `architect` user limited to the `architecture` database and grant only the collection and view permissions required by the server.

## Deployment

A Dockerfile is included for containerized deployment. The mounted FastAPI app serves the MCP server at `/mcp` and a health endpoint at `/healthz`.

## MCP resources

- `memory://architecture`
- `memory://design-language`
- `memory://security`
- `memory://domain-model`

## MCP tools

- `search_memory`
- `search_architecture`
- `search_adrs`
- `search_design_language`
- `search_domain_concepts`
- `semantic_search`
- `hybrid_search`
- `find_related_entities`

## MCP prompts

- `generate_fastapi_service`
- `generate_react_component`
- `generate_arangodb_repository`


## Architecture graph and project guidance

Use the [architecture metamodel](docs/architecture-metamodel.md) for the graph
schema, code indexer, and the read-only MCP tool `get_project_guidance`.
