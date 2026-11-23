from __future__ import annotations

from datetime import date
from typing import Annotated, get_args

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.auth.jwt import profile_name
from app.auth.permissions import WRITER_REQUIRED, access_role, writer_subject
from app.domain.architecture import (
    DEFAULT_GRAPH_HOPS,
    FORWARD_RELATIONSHIPS,
    MAX_GRAPH_HOPS,
    ArchitectureEntity,
    ArchitectureMetamodel,
    ArchitectureNeighborhood,
    ArchitectureSearch,
    EntityType,
)
from app.domain.ask_graph import AskGraphAnswer, AskGraphQuery
from app.domain.exceptions import ADRError, RevalidationError
from app.domain.models import (
    ADRConflictResolution,
    ADRResolutionAction,
    ADRResolutionRationale,
    ConfidenceScore,
    EvidenceCitation,
    MemoryKey,
    MemoryRevalidationResult,
    ReviewQueue,
    ReviewQueueQuery,
)
from app.domain.review_details import (
    ProjectOverview,
    ProjectSummary,
    ReviewEntityDetail,
    ReviewSearchMatch,
    ReviewSearchQuery,
    ReviewSearchResults,
    SearchEmbeddingDiagnostics,
    SimilarRecords,
)
from app.services.ask_graph import AskGraphService
from app.services.review_details import ReviewDetailService
from app.services.review_queue import ReviewQueueService
from app.services.search_service import MemorySearchService

REVIEW_API_PREFIX = "/api/review"


class ReviewSession(BaseModel):
    subject: str | None = None
    display_name: str | None = None
    can_write: bool
    auth_required: bool
    role: str
    browser_session: bool


class RevalidationInput(BaseModel):
    evidence: list[EvidenceCitation] = Field(min_length=1)
    confidence: ConfidenceScore | None = None


class ConflictResolutionInput(BaseModel):
    source_key: MemoryKey
    target_key: MemoryKey
    action: ADRResolutionAction
    rationale: ADRResolutionRationale


def _require_writer(request: Request) -> str:
    subject = writer_subject(getattr(request.state, "auth_claims", {}))
    if subject is None:
        raise HTTPException(status_code=403, detail=WRITER_REQUIRED)
    return subject


def build_review_router(
    service: ReviewQueueService,
    details: ReviewDetailService,
    ask: AskGraphService,
    auth_required: bool,
    search: MemorySearchService,
) -> APIRouter:
    router = APIRouter(prefix=REVIEW_API_PREFIX, tags=["Review"])

    async def resolve_project(key: str | None) -> ProjectOverview | None:
        if key is None:
            return None
        project = await details.get_project(key, "", None)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    @router.get("/session")
    async def session(request: Request, response: Response) -> ReviewSession:
        claims = getattr(request.state, "auth_claims", {})
        response.headers["Cache-Control"] = "no-store"
        return ReviewSession(
            subject=claims.get("subject"),
            display_name=claims.get("display_name") or profile_name(claims.get("raw", {})),
            can_write=writer_subject(claims) is not None,
            auth_required=auth_required,
            role=access_role(claims),
            browser_session=getattr(request.state, "browser_session", False),
        )

    @router.get("/queue")
    async def get_queue(
        query: Annotated[ReviewQueueQuery, Query()], response: Response
    ) -> ReviewQueue:
        response.headers["Cache-Control"] = "no-store"
        project = await resolve_project(query.project_key)
        return await service.get_queue(
            query, [doc.key for doc in project.documents] if project is not None else None
        )

    @router.post("/ask")
    async def ask_graph(query: AskGraphQuery, response: Response) -> AskGraphAnswer:
        response.headers["Cache-Control"] = "no-store"
        return await ask.answer(query, await resolve_project(query.project_key))

    @router.post("/search")
    async def search_memory(query: ReviewSearchQuery, response: Response) -> ReviewSearchResults:
        response.headers["Cache-Control"] = "no-store"
        retrieve = search.semantic_search if query.mode == "semantic" else search.hybrid_search
        if query.project_key:
            project = await details.get_project(query.project_key, "", query.as_of)
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found")
            if not project.documents:
                return ReviewSearchResults(matches=[], fallback_used=False)
            hits = await retrieve(
                query.query, limit=query.limit, as_of=query.as_of, kinds=query.kinds,
                document_keys=[document.key for document in project.documents],
            )
        else:
            hits = await retrieve(
                query.query, limit=query.limit, as_of=query.as_of, kinds=query.kinds
            )
        matches = []
        for hit in hits:
            document = hit.document
            if document.key is None:
                continue
            content = " ".join(document.content.split())
            excerpt = content if len(content) <= 320 else content[:317].rstrip() + "…"
            matches.append(ReviewSearchMatch(
                key=document.key, title=document.title, excerpt=excerpt, kind=document.kind,
                source=document.source, source_id=document.source_id,
                effective_date=document.effective_date, expiration_date=document.expiration_date,
            ))
        profile = next(
            (hit.metadata for hit in hits if hit.retrieval_mode in ("semantic", "hybrid")
             and hit.metadata.get("embedding_provider")),
            None,
        )
        diagnostics = None if profile is None else SearchEmbeddingDiagnostics(
            provider=profile.get("embedding_provider"), model=profile.get("embedding_model"),
            dimension=profile.get("embedding_dimension"), version=profile.get("embedding_version"),
        )
        return ReviewSearchResults(
            matches=matches, fallback_used=any(hit.fallback_used for hit in hits),
            embedding=diagnostics,
        )

    @router.get("/projects/{key}")
    async def get_project(
        key: MemoryKey, response: Response,
        query: Annotated[str, Query(max_length=500)] = "",
        as_of: date | None = None,
    ) -> ProjectOverview:
        result = await details.get_project(key, query, as_of)
        if result is None:
            raise HTTPException(status_code=404, detail="Project not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/projects/{key}/summary")
    async def get_project_summary(key: MemoryKey, response: Response) -> ProjectSummary:
        summary = await details.get_project_summary(key)
        if summary is None:
            raise HTTPException(status_code=404, detail="Project not found")
        response.headers["Cache-Control"] = "no-store"
        return summary

    @router.get("/graph/metamodel")
    async def get_graph_metamodel(response: Response) -> ArchitectureMetamodel:
        response.headers["Cache-Control"] = "no-store"
        return ArchitectureMetamodel(
            entity_types=list(get_args(EntityType)), relationships=FORWARD_RELATIONSHIPS
        )

    @router.get("/graph/entities")
    async def search_graph_entities(
        query: Annotated[ArchitectureSearch, Query()], response: Response
    ) -> list[ArchitectureEntity]:
        response.headers["Cache-Control"] = "no-store"
        return await details.search_graph(query)

    @router.get("/graph/entities/{key}")
    async def get_graph_neighborhood(
        key: MemoryKey,
        response: Response,
        entity_type: EntityType | None = None,
        hops: Annotated[int, Query(ge=1, le=MAX_GRAPH_HOPS)] = DEFAULT_GRAPH_HOPS,
        entity_types: Annotated[list[EntityType] | None, Query(max_length=18)] = None,
    ) -> ArchitectureNeighborhood:
        result = await details.get_neighborhood(
            key, entity_types or ([entity_type] if entity_type else []), hops
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Architecture entity not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/records/{key}/similar")
    async def get_similar_records(
        key: MemoryKey, response: Response, limit: Annotated[int, Query(ge=1, le=20)] = 6
    ) -> SimilarRecords:
        result = await details.similar_records(key, limit)
        if result is None:
            raise HTTPException(status_code=404, detail="Memory record not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/records/{key}")
    async def get_record(key: MemoryKey, response: Response) -> ReviewEntityDetail:
        result = await details.get_record(key)
        if result is None:
            raise HTTPException(status_code=404, detail="Memory record not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/entities/{key}")
    async def get_entity(key: MemoryKey, response: Response) -> ReviewEntityDetail:
        result = await details.get_entity(key)
        if result is None:
            raise HTTPException(status_code=404, detail="Architecture entity not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.post("/records/{key}/revalidate")
    async def revalidate(
        key: MemoryKey,
        body: RevalidationInput,
        request: Request,
    ) -> MemoryRevalidationResult:
        reviewer = _require_writer(request)
        try:
            return await service.revalidate(key, reviewer, body.evidence, body.confidence)
        except RevalidationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/conflicts/resolve")
    async def resolve(body: ConflictResolutionInput, request: Request) -> ADRConflictResolution:
        reviewer = _require_writer(request)
        try:
            return await service.resolve_conflict(
                body.source_key,
                body.target_key,
                reviewer,
                body.action,
                body.rationale,
            )
        except ADRError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
