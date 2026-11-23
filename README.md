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

## Semantic Retrieval

Semantic search is server-driven. Clients can still pass embeddings, but they no longer need to.

Supported embedding modes:

- `ollama`: calls an Ollama endpoint at `MGMCP_EMBEDDING_BASE_URL`
- `deterministic`: local fallback provider for offline or failure cases

If `MGMCP_EMBEDDING_STRICT_MODE=false`, the server falls back to deterministic embeddings when the primary provider fails and logs the fallback explicitly.

Server-generated queries compare vectors from the same provider, model, version, and dimension. Retrieval uses lexical search when no compatible vectors are available. Clients supplying their own vectors are responsible for matching the corpus's embedding model.

Re-embed an existing corpus after changing provider, model, version, or dimension, or to add model metadata to legacy embeddings:

```bash
python -m scripts.reembed_memory --documents --vectors
```

Validate that semantic mode is active by checking the startup logs for `embedding_provider_selected` and by running `semantic_search` without supplying an embedding array.

## Migration Guide

To enable semantic mode on an existing deployment:

1. Set `MGMCP_EMBEDDING_PROVIDER=ollama` or leave the deterministic fallback as the default.
2. Set `MGMCP_EMBEDDING_MODEL`, `MGMCP_EMBEDDING_DIMENSION`, and `MGMCP_EMBEDDING_BASE_URL`.
3. Run `python -m scripts.reembed_memory --documents --vectors`.
4. Call `semantic_search` or `hybrid_search` without an embedding array and confirm the returned metadata includes `retrieval_mode`, `embedding_provider_used`, and `fallback_used`.

## Architectural decision records

An ADR has a unique key, a title, a status, a decision, a rationale, consequences, and an ISO date (`YYYY-MM-DD`). The statuses are `proposed`, `accepted`, `superseded`, and `rejected`.

- Use `create_adr(record)` to store an ADR. The status defaults to `proposed`.
- Use `set_adr_status(key, status)` to accept or reject a proposed ADR.
- Use `supersede_adr(source_key, target_key)` to replace an accepted ADR with another accepted ADR. The operation creates a directed `superseded_by` edge and marks the original `superseded` in the same transaction. Retrying the same link is safe.
- Use `get_adr(key)` to read a structured record, `search_adrs(query)` to search ADRs, or `find_related_entities(entity_id)` with the original ADR's returned `id` to follow its replacement.

With authentication enabled, ADR writes require a `writer` or `admin` role in the token's top-level `roles` list or Keycloak's `realm_access.roles` list. Existing free-text memories tagged with `kind: adr` remain available through `search_adrs`.

## Temporal memory

Memory documents and structured ADRs accept four optional fields:

| Field | Meaning |
| --- | --- |
| `effective_date` | First day the record applies, inclusive |
| `expiration_date` | First day the record stops applying, exclusive |
| `last_validated` | Most recent day the claim was checked against evidence |
| `confidence` | Confidence in the claim on a scale from 0 to 1 |

Dates use `YYYY-MM-DD`. Missing bounds are open, and missing validation or confidence indicates unknown. An expiration date must be later than the effective date.

Use `as_of` on lexical, semantic, or hybrid search to find records that applied on a particular day. For example, an agent answering “What is the current authentication strategy?” can call `search_adrs` with `query: "authentication"` and `as_of` set to today's date. Dated searches include undated legacy records. They exclude structured proposals and rejections. A superseded ADR can appear in historical results when it has an expiration date. When an accepted replacement has an explicit effective date, supersession uses that date to close the original ADR's window unless it already ends earlier.

Omitting `as_of` searches across the corpus, including expired records. Validation date and confidence are returned as evidence signals and do not affect eligibility or ranking.

### Revalidation

Call `revalidate_memory(key, evidence, confidence=None)` with one or more citations, such as a source URL or a commit and file reference. The tool requires a verified `writer` or `admin` token. It records the token's subject, the server's UTC timestamp, and the citations in the record's validation history. It also sets `last_validated` and `next_review_due`. Include `confidence` (0–1) when the cited evidence warrants a new score. The history records both `previous_confidence` and `new_confidence`. Omitting the score keeps the existing confidence, and scores remain unknown if none has been recorded.

Tag an ADR with `security` for a 30-day review interval. Other records use 90 days. New ADRs have an initial review deadline based on their last validation date, when supplied, or their decision date. Existing records get a deadline when revalidated. Review deadlines are independent of the effective date and expiration date. Revalidation preserves both dates and the ADR status.

### ADR conflict detection

Set `conflict_scope` on new ADRs, or use `set_adr_conflict_scope` to label an existing structured ADR. Provide a shared decision topic and at least one scope label. For example:

```json
{
  "topic": "authentication strategy",
  "system": "customer api",
  "environment": "production"
}
```

Call `find_adr_conflicts(key, as_of)` on a retrieved ADR. It reports potentially conflicting decisions within the same topic and namespace. Records must share a system or environment label. If both records specify a system or environment, different values for that field make their scopes disjoint. Labels ignore case and surrounding whitespace.

Decisions must overlap in time. An expiration date is exclusive, so a replacement that starts when its predecessor expires does not conflict with it. Omitting `as_of` checks historical and current overlaps. Missing date bounds remain open. Only accepted ADRs and superseded ADRs with a recorded end date participate.

Each result includes both ADRs, the overlap dates, their original source references when available, and their revalidation history with evidence citations. Different decision text flags a **potential** conflict. The agent should use the decisions and citations to determine whether their implications differ.

Creating an accepted ADR or accepting a proposal checks its entire validity window in the same transaction. Both tools return a `conflict_check` report with up to 20 pairs. Each pair includes both ADRs, the overlap dates, and their citations. Acceptance proceeds so the writer can decide how to resolve the findings. A report with `missing_scope` means the ADR needs scope labels before it can be compared. If `has_more` is true, use `find_adr_conflicts` with a larger limit.

### Listing unresolved ADR conflicts

Use `list_adr_conflicts` to review the entire corpus without starting from an ADR key. Optional `system` and `environment` filters match the pair's scope labels, ignoring case and surrounding whitespace. Combine them to narrow the list. Use `as_of` (`YYYY-MM-DD`) for conflicts that overlap on that date, or omit it to include historical and future overlaps.

Each unresolved pair appears once, with both records, their overlap dates, and citations from both records' validation histories. Results are ordered by most recent overlap start, then by ADR key. Use `limit` (default 20, maximum 100) and `offset` to page through the results.

### Resolving ADR conflicts

A writer or admin can call `resolve_adr_conflict(source_key, target_key, action, rationale)` on a flagged pair. Use `supersede` when the target ADR replaces the source. The resolution and the supersession edge are committed together, and an explicit replacement effective date closes the source's validity window under the normal supersession rules. Use `coexist` to keep both ADRs and document why their decisions can apply together.

The receipt contains both keys, the original overlap, the action, the rationale, the verified resolver's identity, and the server's UTC timestamp. Resolved pairs no longer appear in automatic or manual conflict reports. Use `get_adr_conflict_resolution` with the two keys in either order to retrieve the receipt. Retrying the same resolution returns the original receipt.

### Reviewing resolved ADR conflicts

Use `list_resolved_adr_conflicts` with optional `system`, `environment`, `as_of`, `limit`, and `offset`. Here `as_of` filters the original overlap, including the first day and excluding the end date. Each result shows both ADRs, the original overlap, the resolution action, who resolved it, when, and the rationale. Results use the same overlap-first ordering as the unresolved list.

New resolutions retain the scope labels as recorded at resolution time. Earlier resolutions use the current ADR scope labels for filtering.

### Review queue

Call `get_review_queue` for two lists: `overdue_validations` and `unresolved_adr_conflicts`. Overdue validations include applicable records with a scheduled review date earlier than the review date. They show the deadline, last validation and cited evidence when available. Conflict results include both ADRs, their overlap, and cited evidence for both records.

By default, the queue uses the current UTC date for overdue reviews and includes unresolved conflicts across all overlap dates. Pass `as_of` to evaluate due dates and limit conflicts to that date. Use `limit` for each section, and `validation_offset` and `conflict_offset` to page them independently. Writers can act on entries with `revalidate_memory` or `resolve_adr_conflict`.
