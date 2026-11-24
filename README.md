# MemoryGraphMCP

MemoryGraphMCP is a stateless Model Context Protocol server for secure long-term engineering memory stored in ArangoDB.

It exposes MCP resources, tools, and prompts for retrieval and code generation workflows while keeping all persistent state in ArangoDB.

## What it provides

- MCP resources for architecture, design language, security, and domain model guidance
- Search tools for lexical, semantic, hybrid, and graph-based retrieval
- Prompts for generating FastAPI services, React components, and ArangoDB repositories
- FastAPI hosting with MCP mounted at `/mcp`
- JWT bearer authentication for HTTP access
- A project-scoped architecture graph linking decisions, constraints, goals, tasks, source code, and tests
- A containerized React UI for project navigation, evidence review, search, and guidance publishing
- Local GitHub Actions import, read-only source control history, and an approval-driven CI fix workflow
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

Projects and source repositories are separate nodes. A project can span multiple
repositories. The Python AST indexer records files, modules, classes, functions,
methods, service and repository classes, FastAPI routes, and tests. It infers
code relationships where supported by the source. Use explicit relationships such
as `implements` and `validated_by` to connect decisions to their implementation
and test evidence. Edges record direction, source, confidence, and timestamps.
See the [architecture metamodel](docs/architecture-metamodel.md) and run
`python -m scripts.index_architecture --help` for indexing options.

Use `add_observation` to attach a sourced, timestamped summary and confidence score
to graph entities. Use `save_architectural_constraint` to record a rule, its scope,
authority, enforcement status, governed entities, and validating tests.
`save_project_goal` links objectives to projects, ADRs, services, and tasks.
`add_conversation_summary` stores concise summaries with links to decisions,
risks, and tasks surfaced by the conversation.

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

### Review web client

Project Overview provides a task-first workspace with state filters, Goals, Risks, and Constraints.
See [recoverable capture and task execution](docs/recoverable-capture-and-task-execution.md) for lag,
replay, backfill, local drafting, and agent setup.

The React client is packaged in the `review-ui` container. Docker Compose starts it with the stack. Set `MGMCP_REVIEW_PORT` to change the host port. [Open the local review UI](http://127.0.0.1:3200).

For an existing stack, run `docker compose up -d --build memorygraphmcp review-ui` to create the UI service and load changes. The `docker compose restart` command restarts containers that already exist.

Use **Open record** on a validation or **Open ADR** on either side of a conflict to see the record's validity window, confidence history, and citations. The detail view also shows linked ADRs, services, source files, and tests. Architecture links come from the graph, so an existing ADR needs a graph entry and explicit links to show its related code and tests. Supersession links from the ADR lifecycle appear directly.

Sign in through OIDC or connect with a bearer token accepted by the configured
issuer and audience. Readers can inspect the queue and its evidence. Writers
and admins can cite evidence and optionally update confidence during
revalidation, or resolve an ADR conflict with a rationale. The container
serves the UI and proxies the review API on the same origin.

- **Home** lists projects with CI health, created and last edited dates, open
  tasks, proposed ADRs, overdue reviews, unresolved conflicts, and the last
  successful code index. Counts open the relevant project tab. When project
  dates are missing, the table uses the earliest and latest known records.
- **Projects** uses the project from the header throughout the UI. Overview
  shows counts and open work. The Code tab groups classes and top-level
  functions by repository and file, with methods under their class. Searchable
  functions and methods appear under their containing file. The inspector
  previews decisions, code, and search results, links to full records, and
  opens Graph centered on the selected item via **Explore relationships**.
  Returning from a record restores search filters, expanded files, and selection.
- **Graph** centers the view on a selected node. Filter by node type or
  collapsible category and adjust the hop count from its default of four up
  to ten. Selecting a node or edge opens a right-hand panel. Edge details include
  direction, source, confidence, and both connected nodes.
- **Search Memory** supports semantic and hybrid retrieval with project, as-of
  date, and document type filters. Results show excerpts, sources, validity
  dates, and record links. Open **Advanced options** for search mode and
  embedding diagnostics.
- **Record details** show citations, validity dates, validation and confidence
  history, and linked architecture entities. **Embedding diagnostics** shows
  provider, model, version, dimensions, and compatibility with the active
  search profile. Open **Similar records** on an eligible record or graph node
  to load semantic matches on demand.
- **Help** is at the bottom of the sidebar. It explains the main views,
  guidance publishing, and the graph metamodel, including the supported node
  types and forward and inverse relationships.

For UI development, run `npm ci` and `npm run dev` in `review-ui/` while MemoryGraphMCP is running. Vite proxies the review API to `http://127.0.0.1:3100` by default. Set `REVIEW_API_URL` if the API uses a different address.

**Ask the Graph** accepts a plain-language question and an optional as-of date. It answers project and repository counts from the corresponding graph node types. For questions about engineering decisions, it synthesizes a direct answer from matching decisions and notes, groups corroborating citations per claim, and can explain a decision using its stored rationale. Citations open the underlying detail pages. It shows unresolved ADR conflicts and gaps such as missing effective dates or dated validation evidence. Code and test links come from the current graph and carry a note about their historical coverage. The view uses stored evidence and does not require a generative model or a new provider.

### Browser sign-in and local Keycloak

MemoryGraphMCP supports OpenID Connect Authorization Code Flow with S256 PKCE. The review UI uses an HTTP-only session cookie. MCP clients continue using API-audience bearer access tokens. The application verifies the token issuer, audience, signature, and expiration. Readers browse and search, writers can make review changes, and admins inherit writer access. Role assignment is managed at the identity provider.

For local testing, copy `.env.example` to `.env`, set `ARANGO_ROOT_PASSWORD`, and set three distinct passwords before starting Keycloak for the first time:

- `MGMCP_LOCAL_READER_PASSWORD` for `mgmcp-reader`
- `MGMCP_LOCAL_WRITER_PASSWORD` for `mgmcp-writer`
- `MGMCP_LOCAL_ADMIN_PASSWORD` for `mgmcp-admin`

Start the local profile:

```sh
docker compose -f docker-compose.yml -f docker-compose.local-auth.yml \
  --profile local-auth up -d --build
```

Open `http://127.0.0.1:3200` and select **Sign in**. Keycloak listens on `127.0.0.1:8180` by default. Its local realm is imported on first startup and persisted in the Keycloak volume. The local realm uses a public PKCE client and the distinct `memorygraphmcp-api` access-token audience. Use the same Compose files on subsequent restarts to retain the local authentication settings.

For a single API instance, set `MGMCP_OIDC_BROWSER_SESSION_DB=/var/lib/memorygraphmcp/sessions/browser.sqlite` and supply a stable Fernet key as `MGMCP_OIDC_BROWSER_SESSION_KEY`. The Compose session volume preserves encrypted sessions across API container restarts. Keep the key in private deployment configuration and reuse it on restart. Rotating the key invalidates existing stored sessions. The store is intended for a single API process.


The `mgmcp-admin` account has the application admin role. Keycloak's administration console signs in to the separate `master` realm. Set `MGMCP_LOCAL_KEYCLOAK_ADMIN_USERNAME` and `MGMCP_LOCAL_KEYCLOAK_ADMIN_PASSWORD` before first startup to bootstrap a temporary Keycloak administrator. For an existing local database, use the same Compose configuration and stop Keycloak before recovery:

```sh
docker compose --profile local-auth stop keycloak
docker compose --profile local-auth run --rm --no-deps \
  --entrypoint /opt/keycloak/bin/kc.sh keycloak bootstrap-admin user \
  --username mgmcp-keycloak-admin --password:env KC_BOOTSTRAP_ADMIN_PASSWORD \
  --no-prompt --db=dev-file
docker compose --profile local-auth up -d --no-deps keycloak
```

Use the temporary account at `http://127.0.0.1:8180/admin/`. Keycloak marks bootstrap users as temporary. Once a permanent administrator is available, remove the bootstrap account.

For a shared deployment, run the base Compose file with authentication enabled and configure these provider settings:

| Setting | Purpose |
| --- | --- |
| `MGMCP_OIDC_ISSUER` | Exact OIDC issuer |
| `MGMCP_OIDC_JWKS_URI` | Provider signing keys URL |
| `MGMCP_OIDC_AUDIENCE` | Audience of the JWT access token for this API |
| `MGMCP_OIDC_BROWSER_ORIGIN` | Public HTTPS origin of the review UI |
| `MGMCP_OIDC_BROWSER_CLIENT_ID` | OIDC browser client |
| `MGMCP_OIDC_BROWSER_CLIENT_SECRET` | Secret for a confidential client, if applicable |
| `MGMCP_OIDC_BROWSER_SESSION_DB` | Optional path to a private SQLite session file on a persistent volume |
| `MGMCP_OIDC_BROWSER_SESSION_KEY` | Stable Fernet encryption key for the browser session file |
| `MGMCP_OIDC_BROWSER_TOKEN_AUTH` | `client_secret_basic` or `client_secret_post` |
| `MGMCP_OIDC_BROWSER_SCOPES` | Additional access-token scopes, when required |
| `MGMCP_OIDC_BROWSER_AUDIENCE` | Optional audience parameter for providers that require one |
| `MGMCP_OIDC_ROLE_CLAIM` | Top-level access-token claim containing application role names, default `roles` |

Register `<UI origin>/api/auth/callback` and `<UI origin>/` as the exact login and logout redirect URIs. The provider must issue RS256-signed JWTs. Keycloak's realm roles are also supported. If the provider advertises an end-session endpoint, sign-out ends both the local session and the provider session. Browser sessions live in the API process and expire with the provider tokens or after a service restart. Run one API worker for browser sign-in until shared session storage is configured.

### CI test runs

The project **Testing** tab opens the latest imported GitHub Actions attempt by default. Use **Run history** to open an earlier attempt and see its commit, completion time, totals, and failures. **Load older runs** pages through imported history. **Last passing run** shows when the most recent successful attempt finished and links to that attempt, including when the latest run failed. **Recurring failures** groups by test name and source file across the most recent 20 imported attempts. Open a group to inspect the attempts that reported it. Failed tests use their repository-relative JUnit file path to find the indexed test suite and its current ADR and constraint validation links. CI publishes test names, paths, and exception types when available. Open the linked GitHub Actions attempt for full logs.

The workflow runs pytest on pushes and pull requests. Default-branch pushes upload a bounded JSON summary as a GitHub Actions artifact, including when pytest fails. To enable local import, set `MGMCP_CI_GITHUB_REPOSITORY` to `owner/repo` and set `MGMCP_CI_PROJECT_KEY` and `MGMCP_CI_REPOSITORY_KEY` to the existing project and linked source repository. The API polls GitHub from inside the local deployment. Set `MGMCP_CI_GITHUB_BRANCH` if the default branch is different from `main`. For private repositories, set `MGMCP_CI_GITHUB_READ_TOKEN` locally to a credential with Actions read access. GitHub needs no inbound access to the API. The importer retries missing artifacts within its ten-run window and skips attempts already stored.

In the Testing tab, authenticated users can choose **Suggest fix** on an imported GitHub failure when local inference is configured. Set `MGMCP_TEST_FIX_OLLAMA_URL` to the Ollama URL reachable by the API container and install `qwen3-coder:30b` on that Ollama instance. Override the model with `MGMCP_TEST_FIX_OLLAMA_MODEL` if needed. The application supplies the failure summary, source from the tested commit, and linked ADR or constraint evidence. Each suggestion includes a likely cause, proposed edit, and verification step. The feature leaves the source tree and CI untouched.

Testing also shows the recorded build outcome and warning count for each attempt. Expand a warning to see its diagnostic and request a fix suggestion. The CI workflow builds and installs the Python package, and builds the frontend when the checked-out commit includes its package lockfile. Older artifacts can have unrecorded build and warning fields.

Project **CI health** uses the latest imported attempt. A build or test failure is red, a completed passing build and test run with warnings is yellow, and a completed passing run with zero warnings is green. Missing or incomplete evidence is unknown. Home and the project page show whether the latest imported result tested the configured GitHub branch head. A different commit is stale and turns a non-failing health result yellow; an existing failure remains red.


After **Suggest fix** returns, signed-in users can choose **Draft this fix** when the local worker is configured. Applying a verified patch requires writer or admin access. The reviewed suggestion is passed to the local model as evidence. It drafts a bounded edit to a Python test or `pyproject.toml` in a detached worktree at the CI commit. It runs a focused pytest check or the Python package build before and after the edit in a network-disabled Docker container. Draft results include the diff and both check outputs. Testing saves the issue, suggestion, draft, and verification results. After pre-verification, **Apply to fix branch** requires explicit writer or admin approval. The local worker applies the reviewed diff to the configured remote target branch in isolation, runs the check, and saves the passing commit on a local branch such as `fix/test-verify-hs256-token-a1b2c3d4e5f6`. The active checkout stays untouched. A second approval sends the exact verified commit to GitHub and opens a pull request. **Merge pull request** requires a third approval and submits the exact PR head SHA to GitHub. GitHub closes the pull request when the merge succeeds. **Verify merged fix** checks for an imported passing run on the exact merge commit and advances when it arrives. **Delete branch** asks for approval to delete the remote fix branch with a lease on the approved SHA. Warning fixes require complete warning evidence and absence of the original warning. Frontend checks require a separate verification environment.

To enable drafting on a local checkout, build the check image with `docker build -f Dockerfile.draft-check -t memorygraphmcp-draft-check:local .`. In the ignored `.env`, set `MGMCP_DRAFT_REMOTE` to the trusted Git repository URL, `MGMCP_DRAFT_WORKER_TOKEN` to a generated secret, and `MGMCP_DRAFT_WORKER_URL` to the worker's address as seen by the API container (for Docker Desktop on macOS, `http://host.docker.internal:3211`). Run `sh scripts/start_draft_worker.sh` from a local terminal and recreate the API container. The worker listens on host loopback.


To enable **Publish pull request**, set `MGMCP_CI_GITHUB_PUBLISH_TOKEN` in the
ignored local `.env` and restart both the API container and the local worker.
Use a repository-scoped GitHub credential with Contents and Pull requests
write permissions. Set `MGMCP_DRAFT_REMOTE` to the HTTPS GitHub URL for
`MGMCP_CI_GITHUB_REPOSITORY`. The CI importer and PR status reader continue to
use `MGMCP_CI_GITHUB_READ_TOKEN`. For private repositories, give that token
Actions, Contents, and Pull requests read access. Use a protected target branch with required
pull requests, approvals and CI status checks. Allow squash merges, and keep the publishing credential subject to the target branch rules. Each saved CI patch gets its own fix branch. A single patch can address related symptoms that share a root cause.

### Project Source Control

The Source Control tab follows the project selected in the header and shows recent GitHub pull requests and default-branch commits for
the project's linked source repositories. CI links use the exact commit from
locally imported runs. Branch cleanup uses the saved Testing record. The existing
CI repository is available automatically. To enable more repositories, set
`MGMCP_GITHUB_CHANGE_REPOSITORIES` to comma-separated
`graph-repository-key=owner/name` pairs in the local environment. The existing
GitHub read credential needs Pull requests and Contents read access for any
private repositories included in the view.


### Project tracker events

To enable tracker event ingestion, set `MGMCP_GRAPH_EVENT_PRODUCER_SUBJECT` to the
verified token subject of the tracker writer and `MGMCP_GRAPH_EVENT_PROJECT_KEY`
to the key of its existing Project. Both settings require authentication.
The configured producer can submit `task.upserted` and `task.completed` using
`ingest_graph_event` and retrieve receipts with `get_graph_event`.

Each event includes `project_key`, `task_key`, `event_id`, and a timezone-aware
`occurred_at`. An upsert includes `name` and may include `description` and
`status`. New tasks, their ownership links, and receipts are written together.
Existing tasks must link directly and exclusively to that project through a
`defines`, `related_to`, or `references` edge. Replaying the same payload returns
its original receipt. Reusing its ID with a different payload is rejected.
Events at or before the last applied time are recorded without changing the task.

Project Overview shows tracker configuration status and the last ten receipts
in processing order. It links to tasks still associated with the project and
labels stale events. Readers can inspect activity through the Review UI.


### Global and Project Guidance

`AGENTS.md` is the entry point for coding agents. It directs them to call
`get_project_guidance(project_key)` before starting architecture work or
recorded follow-ups. Find the project key using `search_architecture_entities`.

| Editor | Scope | Who can draft | Who can publish |
| --- | --- | --- | --- |
| Sidebar **Global Guidance** | All projects in this deployment | Admin | Admin |
| **Projects → Guidance** | Selected project | Writer or admin | Admin |

Save a draft, then choose **Approve and publish** and confirm the saved revision.
Each publication records the author, approving admin, timestamps, and version.
The editors show the current publication and earlier versions.

The read-only MCP tool returns the two published scopes separately.
`global_guidance` contains deployment-wide instructions and `published_guidance`
contains project instructions. Each field is null until its first publication.
Drafts stay in the editors. Current accepted ADRs and active constraints,
including validating test keys, are returned alongside both guidance fields.

### Codex development handoff

1. Open the task in **Projects → Overview → Tasks** and bring its link and
   acceptance criteria into an interactive Codex session in the source
   repository. Follow `AGENTS.md` and call `get_project_guidance` for the task’s
   project. Open the cited ADRs, constraints, or tests that apply.
2. Implement on a focused branch. Run the relevant checks and use Codex to
   review the exact diff or commit. Get explicit approval before publishing.
3. Return to the same task. Choose **Completed** in the **State** dropdown and record the reviewed commit,
   checks and results, unresolved findings, and any follow-up. Link relevant
   ADRs or evidence through the graph. Set the task to **Completed** when the
   result is verified. For unfinished work, keep the task open and record
   the blocker or follow-up in a linked observation.

For example, a completed outcome could read “Reviewed commit `abc1234`.
The changed-app pytest suite passed 24 tests. Diff review found no blockers.
Publishing is pending approval.” The project Help topic **Develop with Codex**
describes the same handoff. The task panel shows past agent executions in the read-only **History** panel.

### Approved knowledge snapshots

For snapshots that reference each other's ADRs, restore them in one transaction:

```sh
python -m scripts.knowledge_snapshot import-batch --project PROJECT \
  --snapshot /path/to/repo-a COMMIT_A REPOSITORY_A \
  --snapshot /path/to/repo-b COMMIT_B REPOSITORY_B
```

Use `check-batch` for a read-only preview. Set up the project and linked repository records
before restore. Shared ownership links require their destination project records to exist.
The restore validates every Git snapshot before writing, installs all ADRs before resolving
cross-repository coexistence, and rolls back if any snapshot cannot be verified.

From the repository root, use `python -m scripts.knowledge_snapshot` with
`--project PROJECT_KEY --repository SOURCE_REPOSITORY_KEY`. This maintenance
command uses the configured ArangoDB credentials. Set the linked Repository
node's `metadata.git_checkout` to the absolute path of its trusted local Git
checkout. The command accepts that repository and its linked worktrees. For
`check` and `import`, the snapshot's source commit must be an ancestor of, or
equal to, the commit containing the snapshot. Review the snapshot before
committing it.

- `export` writes JSON to stdout for review and placement at
  `knowledge/approved.json`. It includes accepted ADRs owned by the selected
  repository, their
  documented coexistence resolutions, and current published project guidance.
  Export requires a stored review deadline for every included ADR. Backfill
  missing deadlines before exporting. Coexistence keeps its audit history when
  an ADR's scope changes. Only a resolution with the same pair, scope, and
  overlap suppresses a current conflict. Legacy resolutions match the pair,
  system, environment, and overlap recorded at the time.
  The snapshot identifies the owning project, linked source repository, and
  Git commit selected at export. Repository-owned ADRs carry ownership data
  used to restore their defining links and direct project links and detect
  ownership drift. A direct project link alone leaves an ADR in project
  guidance without assigning it to a repository snapshot.
- `check` reads that path from a local commit selected by `--ref` (default
  `HEAD`). It exits with status 1 on drift and reports records present only in
  Git, records present only in the database, differing records with both
  values, and occupied keys that would prevent a safe import.
- `import` reads the same committed snapshot. If the only differences are
  new records from Git, it validates coexistence resolutions against the ADR
  pair and checks newly imported ADRs for unresolved conflicts. It verifies the resulting
  snapshot before committing the database transaction, and journals the
  imported record keys and both Git revisions. Re-importing matching content
  succeeds without writes. For other differences, it exits with status 1 and
  leaves the database intact for review. An unresolved conflict also aborts
  the import and reports both decisions and their available evidence.

Snapshots cover current approved project knowledge. The database retains
guidance drafts, earlier publications, global guidance, supersession links,
and search embeddings.

### Automated knowledge capture

The capture worker creates project-linked observations from commits, pull
requests, ADRs, CI attempts, task updates, architecture indexes, constraints,
and existing conversation summaries. **Projects → Review → Suggested
relationships** shows tentative links with confidence and evidence. Capture
links each observation to the project and directly matched entities.
Suggestions do not create confirmed architecture edges.

Set `MGMCP_CAPTURE_PROJECT_KEY` and `MGMCP_CAPTURE_REPOSITORY_KEY` for an
existing project and its linked source repository. For Compose, set
`MGMCP_CAPTURE_HOST_SPOOL` to an absolute local directory and add
`-f docker-compose.capture.yml` alongside your existing Compose files. From
each participating Git checkout, install the post-commit hook once:

```sh
python /path/to/MemoryGraphMCP/scripts/install_capture_hook.py /path/to/capture-spool
```

The hook queues the commit SHA, changed files, and a bounded diff. It does
not need GitHub or MCP credentials. The worker retries unprocessed files
and removes an envelope after capture. Keep the spool private because
unprocessed envelopes contain source diffs. Pass `--index-branch main` when
installing the hook in an authoritative checkout if local commits on that
branch should also refresh the complete Python architecture index. Use
one capture worker and one authoritative checkout per indexed repository.

Set `MGMCP_CAPTURE_GITHUB_REPOSITORY=owner/repo` to also capture recent
GitHub commits and pull requests. Private repositories use the existing
read-only GitHub credential. GitHub polling uses a bounded recent history
and follows the configured CI branch for commits. To refresh the full Python
index at that branch's GitHub head, set `MGMCP_CAPTURE_INDEX_GITHUB=true`.
Choose one authoritative index source per repository. Python indexing can
capture literal `ConstraintInput` declarations with a key, name, rule, scope,
and authority as proposed evidence. Active architectural constraints are
managed through the constraint tool. CI and tracker events
come from the graph journal. ADRs, constraints, indexes, standalone tasks,
and existing conversation summaries are observed as project-scoped snapshots.

Writers can call `review_change` with the project key, diff, optional test
results, optional rationale, and linked entities. The tool records what changed, any supplied
rationale, and related ADRs, goals, and constraints. It uses stored evidence
and static heuristics, so missing rationale stays unknown.

### Graph context packages and token usage

Call `build_context_package` with a question and optionally a project, entity,
ADR, goal, or service key. An ADR anchor can use its graph key or stored record
key. The tool uses graph anchors and direct relationships to collect compact
decisions, active constraints, services, tests, and citations. Use `as_of` to
filter decisions by date and `max_context_tokens` to limit the estimated size.
Graph links describe the currently indexed architecture. Pass
`include_full_adrs: true` to request full ADR text when it fits the same budget.
Send the context fields to your LLM and retain the `usage` receipt separately.
Ask the Graph uses the graph evidence first and falls back to lexical evidence
when it finds no applicable linked decisions.

**Token usage** in the sidebar shows recent retrievals, daily history, and
session, project, and lifetime summaries. An MCP retrieval returns its receipt
in result metadata or, for context packages, in the structured result.
Review API searches and graph requests expose estimates and a request ID in
`X-MemoryGraph-*` headers. Use `get_retrieval_usage` for an individual receipt
and `get_token_usage` for rollups. For bearer clients without a server session,
send a consistent `X-MemoryGraph-Session` value to group requests.

The estimator is model-independent. The counterfactual uses full text for the
same retrieved documents or candidate ADRs where available, with the same
question and reported response tokens. A missing baseline produces no savings
estimate. Use `report_retrieval_outcome` to add completion, follow-up retries,
and optional provider-counted response tokens to your request. The dashboard
compares average reported retries for graph and document retrieval when both
groups have outcomes. It does not infer success or claim a causal reduction.
The journal stores metrics, not query text, retrieved text, or model output.

### Engineering effectiveness

Open **Effectiveness** in the sidebar. Compare projects, linked source
repositories, or inclusive UTC date ranges side by side. Inventory and health
are current snapshots. Regenerating a historical activity report uses today's
inventory and links, with the selected dates applied to activity. The dashboard
shows score denominators, evidence counts, and calculation methods.

Writers can call `report_development_outcome` (or POST to
`/api/review/effectiveness/outcomes`) with a project and source repository,
stable change ID, author category (`agent` or `human`), time, and citation.
Replaying the same report returns its receipt. Outcome reports can include a
task finish and start time, first-pass result, retry count, violations of linked
constraints, and separate rework, review, or onboarding estimates. Each effort
estimate requires hours, confidence from 0 to 1, and evidence. Keep categories
non-overlapping. Actor category is reported explicitly. GitHub account names
are insufficient evidence for attribution. If an agent report uses the exact
commit SHA, missing check outcomes can use imported CI attempts for that commit.

Analytics read the existing tracker, observation, and retrieval journals when
those features have produced data. Project-wide retrieval events without a
repository key contribute only to project totals. No token or effort estimate
is extrapolated across missing samples. For model cost avoidance, enter a USD
price per million input tokens. The result estimates input cost at that price,
and negative net savings indicate extra tokens or cost.

The **Token usage** and **Effectiveness** tabs in Projects stay scoped to the
open project. The sidebar dashboards support wider views and comparisons
across projects. Charts summarize daily token activity, weekly delivery,
graph health, and comparisons. Expand the detailed measurements to inspect
the underlying numbers and methodology. Insights are calculated directly
from those numbers and describe missing evidence and negative savings.

### Help center

Open **Help** at the bottom of the sidebar to search or browse task guides.
Each guide has its own URL and links to related topics. Use the circled
question mark beside a screen or project tab heading for a short explanation,
then choose **More** to open the matching guide. The graph metamodel guide
shows the node and relationship types supported by the running API.
