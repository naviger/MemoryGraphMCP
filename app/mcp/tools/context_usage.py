from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated
from uuid import uuid4

from pydantic import Field

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware

from app.auth.project_access import current_project_access
from app.domain.context_usage import (
    CheckedContextAnswer,
    CheckedExcerptAnswer,
    ContextClaim,
    ContextClaimCheck,
    ContextPackage,
    ContextQuery,
    ContextExcerpt,
    MeasuredContextPackage,
    PreparedContextExcerpts,
    RequestID,
    UsageOutcome,
    UsageReceipt,
    UsageScope,
    UsageSummary,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import MemoryKey
from app.services.context_package import ContextPackageService, context_excerpts, finalize_excerpt_answer
from app.services.token_usage import TokenUsageService, document_baseline, identity

_GRAPH = {"find_related_entities", "search_architecture_entities", "trace_architecture", "get_project_guidance"}
_LEXICAL = {"search_memory", "search_architecture", "search_adrs", "search_design_language", "search_domain_concepts"}
_METHODS = {**dict.fromkeys(_GRAPH, "Graph"), **dict.fromkeys(_LEXICAL, "Lexical"),
            "semantic_search": "Semantic", "hybrid_search": "Hybrid"}


def request_or_none():
    try:
        return get_http_request()
    except RuntimeError:
        return None


_CONTEXT_TTL_SECONDS = 180
_CONTEXT_CACHE_LIMIT = 128
ContextPrincipal = tuple[str, bool | None, tuple[tuple[str, str], ...]]


def _context_principal(project: str) -> ContextPrincipal:
    access = current_project_access.get()
    owner, _ = identity(request_or_none())
    if access is None:
        return owner, None, ()
    access.require(project)
    return owner, access.unrestricted, tuple(sorted(access.grants.items()))


@dataclass(frozen=True)
class PreparedContext:
    query: ContextQuery
    principal: ContextPrincipal
    package: ContextPackage
    expires_at: float


class RetrievalUsageMiddleware(Middleware):
    def __init__(self, usage: TokenUsageService):
        self.usage = usage

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        method = _METHODS.get(name)
        result = await call_next(context)
        if method is None or result.is_error:
            return result
        args = context.message.arguments or {}
        nested = args.get("request") or {}
        query = args.get("query") or nested.get("query") or args.get("entity_id") or nested.get("key") or args.get("project_key") or ""
        payload = result.structured_content
        context_body = payload if payload is not None else [
            item.text for item in result.content if item.type == "text"
        ]
        rows = payload.get("result") if isinstance(payload, dict) else None
        rows = rows if isinstance(rows, list) else []
        baseline = document_baseline(
            [item["document"] for item in rows if isinstance(item, dict) and "document" in item],
        ) if method != "Graph" else None
        modes = {item.get("retrieval_mode") for item in rows if isinstance(item, dict)}
        requested = method
        if modes == {"lexical"}:
            method = "Lexical"
        path = name if requested == method else f"{name} → lexical fallback"
        receipt = await self.usage.record(
            request_or_none(), method, str(query), context_body, project=args.get("project_key"),
            baseline=baseline, entities=len(rows), path=path,
        )
        result.meta = {**(result.meta or {}), "memorygraph/token_usage": receipt.model_dump(mode="json")}
        return result


def register_context_usage(mcp: FastMCP, service: ContextPackageService, usage: TokenUsageService):
    mcp.add_middleware(RetrievalUsageMiddleware(usage))
    prepared: OrderedDict[str, PreparedContext] = OrderedDict()

    @mcp.tool
    async def build_context_package(query: ContextQuery) -> MeasuredContextPackage:
        """Blend graph and document evidence with dated ADR conflicts and budget exclusions."""
        try:
            package, baseline, count = await service.build(query)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc
        receipt = await usage.record(
            request_or_none(), "ContextPackage", query.question, package, project=query.project_key,
            baseline=baseline, entities=count,
            path="graph and document evidence → ADR conflict lookup → budgeted context",
        )
        return MeasuredContextPackage(**package.model_dump(), usage=receipt)

    @mcp.tool
    async def validate_context_citations(
        query: ContextQuery, claims: Annotated[list[ContextClaim], Field(min_length=1, max_length=20)],
    ) -> list[ContextClaimCheck]:
        """Rebuild scoped context and check exact excerpt sentences and destination:key citations."""
        try:
            return await service.check_claims(query, claims)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def finalize_context_answer(
        query: ContextQuery, claims: Annotated[list[ContextClaim], Field(max_length=20)],
    ) -> CheckedContextAnswer:
        """Before returning an assisted answer, separate verified findings from flagged claims.

        Use citation_id from build_context_package and quote exact excerpt sentences.
        Flagged claims are unverified and must not be presented as findings.
        """
        try:
            return await service.finalize_answer(query, claims)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def list_context_excerpts(query: ContextQuery) -> list[ContextExcerpt]:
        """List selectable exact excerpts and their canonical graph citations for this query."""
        try:
            return await service.list_excerpts(query)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def prepare_context_excerpts(query: ContextQuery) -> PreparedContextExcerpts:
        """Prepare scoped excerpts and a short-lived context ID for finalization."""
        if query.project_key is None:
            raise ToolError("Provide a project key to reuse excerpt context")
        try:
            principal = _context_principal(query.project_key)
            package, _, _ = await service.build(query)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc
        now = monotonic()
        while prepared and (next(iter(prepared.values())).expires_at <= now
                            or len(prepared) >= _CONTEXT_CACHE_LIMIT):
            prepared.popitem(last=False)
        context_id = uuid4().hex
        prepared[context_id] = PreparedContext(query, principal, package, now + _CONTEXT_TTL_SECONDS)
        return PreparedContextExcerpts(context_id=context_id, excerpts=context_excerpts(package))

    @mcp.tool
    async def finalize_context_excerpts(
        query: ContextQuery,
        excerpt_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=128)]], Field(max_length=20)],
        context_id: RequestID | None = None,
    ) -> CheckedExcerptAnswer:
        """Resolve and check selected excerpts. Pass the prepared context ID to reuse the scoped package."""
        try:
            if context_id is None:
                return await service.finalize_excerpts(query, excerpt_ids)
            if query.project_key is None:
                raise ToolError("Provide a project key to reuse excerpt context")
            principal = _context_principal(query.project_key)
            snapshot = prepared.get(context_id)
            if snapshot is not None and snapshot.expires_at <= monotonic():
                prepared.pop(context_id)
                snapshot = None
            if (snapshot is None or snapshot.principal != principal or snapshot.query != query
                    or query.as_of is None and snapshot.package.as_of != datetime.now(UTC).date()):
                raise ToolError("Prepared context is unavailable for this query. Prepare excerpts again.")
            return finalize_excerpt_answer(snapshot.package, excerpt_ids)
        except ArchitectureGraphError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def get_token_usage(scope: UsageScope = "session", project_key: MemoryKey | None = None) -> UsageSummary:
        """Read estimated token usage, savings, and client-reported outcomes."""
        if scope == "project" and project_key is None:
            raise ToolError("Provide a project key for project totals")
        return await usage.summary(request_or_none(), scope, project_key)

    @mcp.tool
    async def get_retrieval_usage(request_id: RequestID) -> UsageReceipt:
        """Read a retrieval request receipt and its reported outcome."""
        receipt = await usage.read(request_or_none(), request_id)
        if receipt is None:
            raise ToolError("Usage request not found")
        return receipt

    @mcp.tool
    async def report_retrieval_outcome(request_id: RequestID, outcome: UsageOutcome) -> UsageReceipt:
        """Record completion, number of follow-up retries, and optionally provider-counted output tokens."""
        receipt = await usage.report(request_or_none(), request_id, outcome)
        if receipt is None:
            raise ToolError("Usage request not found")
        return receipt
