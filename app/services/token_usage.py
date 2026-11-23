"""Model-independent token estimates and durable retrieval measurements."""
import re
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

import anyio
import structlog
from pydantic import BaseModel, TypeAdapter
from starlette.requests import Request

from app.auth.browser_oidc import SESSION_COOKIE
from app.domain.context_usage import (
    RetrievalMethod,
    UsageOutcome,
    UsageReceipt,
    UsageScope,
    UsageSummary,
)

logger = structlog.get_logger(__name__)
_PARTS = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+|[^\W\d_]+", re.UNICODE)
_JSON = TypeAdapter(object)


class UsageStorage(Protocol):
    def record_usage(self, receipt: UsageReceipt, owner: str, session: str | None) -> None: ...

    def read_usage(self, request_id: str, owner: str) -> UsageReceipt | None: ...

    def report_usage(self, request_id: str, owner: str, outcome: UsageOutcome) -> UsageReceipt | None: ...

    def query_usage(
        self, scope: UsageScope, project: str | None, session: str | None,
    ) -> tuple[list[dict], list[UsageReceipt]]: ...


def words(text: str) -> list[str]:
    return [word.casefold() for word in _WORDS.findall(text.replace("_", " "))]


def estimate_tokens(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        text = value
    elif isinstance(value, BaseModel):
        text = value.model_dump_json(exclude_none=True)
    else:
        text = _JSON.dump_json(value, exclude_none=True).decode()
    return sum(max(1, (len(piece.encode()) + 3) // 4) for piece in _PARTS.findall(text))


def document_baseline(documents) -> int | None:
    values = [
        {name: value for name in ("title", "content", "decision", "rationale", "consequences")
         if (value := item.get(name) if isinstance(item, dict) else getattr(item, name, None))}
        for item in documents
    ]
    return estimate_tokens(values) if values else None


def identity(request: Request | None) -> tuple[str, str | None]:
    if request is None:
        return "anonymous", None
    claims = getattr(request.state, "auth_claims", {})
    subject = claims.get("subject")
    issuer = (claims.get("raw") or {}).get("iss", "")
    owner = sha256(f"{issuer}\0{subject}".encode()).hexdigest() if subject else "anonymous"
    session = request.headers.get("mcp-session-id") or request.headers.get("x-memorygraph-session")
    if getattr(request.state, "browser_session", False):
        session = request.cookies.get(SESSION_COOKIE)
    key = sha256(f"{owner}\0{session[:256]}".encode()).hexdigest() if session else None
    return owner, key


class TokenUsageService:
    def __init__(self, storage: UsageStorage):
        self.storage = storage

    async def record(
        self, request: Request | None, method: RetrievalMethod, query: str, context: object, *,
        project: str | None = None, baseline: int | None = None, entities: int = 0, path: str,
    ) -> UsageReceipt:
        owner, session = identity(request)
        query_tokens, context_tokens = estimate_tokens(query), estimate_tokens(context)
        receipt = UsageReceipt(
            request_id=uuid4().hex, timestamp=datetime.now(UTC), method=method, project_key=project,
            query_tokens=query_tokens, context_tokens=context_tokens,
            total_tokens=query_tokens + context_tokens, entities_supplied=entities, retrieval_path=path,
            naive_rag_tokens=query_tokens + baseline if baseline is not None else None,
            savings_tokens=baseline - context_tokens if baseline is not None else None,
        )
        try:
            await anyio.to_thread.run_sync(self.storage.record_usage, receipt, owner, session)
        except Exception as exc:
            logger.warning("token_usage_record_failed", error_type=type(exc).__name__)
        return receipt

    async def read(self, request: Request | None, request_id: str) -> UsageReceipt | None:
        return await anyio.to_thread.run_sync(self.storage.read_usage, request_id, identity(request)[0])

    async def report(self, request: Request | None, request_id: str, outcome: UsageOutcome) -> UsageReceipt | None:
        return await anyio.to_thread.run_sync(self.storage.report_usage, request_id, identity(request)[0], outcome)

    async def summary(self, request: Request | None, scope: UsageScope, project: str | None) -> UsageSummary:
        grouped, recent = await anyio.to_thread.run_sync(
            self.storage.query_usage, scope, project, identity(request)[1],
        )
        methods: dict[str, int] = {}
        trend = {}
        for row in grouped:
            methods[row["method"]] = methods.get(row["method"], 0) + row["requests"]
            day = trend.setdefault(row["day"], {"date": row["day"], "requests": 0, "context_tokens": 0,
                                               "savings_tokens": 0, "measured_savings_requests": 0})
            for name in ("requests", "context_tokens", "savings_tokens", "measured_savings_requests"):
                day[name] += row[name]

        def total(name, subset=grouped):
            return sum(row[name] for row in subset)

        graph = [row for row in grouped if row["method"] in {"Graph", "ContextPackage"}]
        search = [row for row in grouped if row["method"] in {"Lexical", "Semantic", "Hybrid"}]
        requests, outcomes = total("requests"), total("reported_outcomes")
        measured, savings = total("measured_savings_requests"), total("savings_tokens")
        graph_count, search_count = total("reported_outcomes", graph), total("reported_outcomes", search)
        return UsageSummary(
            scope=scope, requests=requests, methods=methods, measured_savings_requests=measured,
            total_query_tokens=total("query_tokens"), total_context_tokens=total("context_tokens"),
            total_reported_response_tokens=total("response_tokens"),
            total_tokens=total("total_tokens"), total_savings=savings if measured else None,
            average_context_tokens=total("context_tokens") / requests if requests else 0,
            average_savings=savings / measured if measured else None,
            reported_outcomes=outcomes, successful_completions=total("successful_completions"),
            average_follow_up_retries=total("retries") / outcomes if outcomes else None,
            retry_reduction_estimate=(
                total("retries", search) / search_count - total("retries", graph) / graph_count
                if graph_count and search_count else None
            ), recent=recent, trend=[trend[day] for day in sorted(trend)[-30:]],
        )
