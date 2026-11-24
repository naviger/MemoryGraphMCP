from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal, get_args
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.auth.jwt import profile_name
from app.auth.permissions import WRITER_REQUIRED, access_role, writer_subject
from app.auth.project_access import current_project_access
from app.domain.architecture import (
    DEFAULT_GRAPH_HOPS,
    FORWARD_RELATIONSHIPS,
    MAX_GRAPH_HOPS,
    ArchitectureEntity,
    ArchitectureMetamodel,
    ArchitectureNeighborhood,
    ArchitectureSearch,
    EntityType,
    Project,
)
from app.domain.architecture_events import TrackerOverview
from app.domain.ask_graph import AskGraphAnswer, AskGraphQuery
from app.domain.context_usage import (
    ContextQuery,
    MeasuredContextPackage,
    RequestID,
    UsageOutcome,
    UsageReceipt,
    UsageScope,
    UsageSummary,
)
from app.domain.exceptions import ADRError, ArchitectureGraphError, RevalidationError
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
from app.domain.project_changes import ChangeView, ProjectChanges
from app.domain.project_guidance import GuidanceApproval, GuidanceDraftInput, GuidanceEditor
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
from app.domain.test_drafts import (
    DraftApproval,
    DraftID,
    FixWorkflow,
    FixWorkflowRequest,
    FixWorkflowView,
    PublishApproval,
    TestDraft,
    TestFixSuggestion,
)
from app.domain.test_runs import (
    LastPassingRun,
    ProjectCIHealth,
    ProjectTestRuns,
    RecurringTestFailures,
    TestFixRequest,
    TestRunDetail,
    TestRunHistory,
    TestRunID,
    ci_run_health,
    project_ci_health,
)
from app.services.ask_graph import AskEvidence, AskGraphService
from app.services.context_package import ContextPackageService
from app.services.project_changes import ProjectChangesService
from app.services.review_details import ReviewDetailService
from app.services.review_queue import ReviewQueueService
from app.services.search_service import MemorySearchService
from app.services.test_fix import (
    DraftWorkerError,
    TestFixBusy,
    TestFixService,
    TestFixUnavailable,
)
from app.services.token_usage import TokenUsageService, document_baseline

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


def _require_guidance_admin(request: Request) -> str:
    if access_role(getattr(request.state, "auth_claims", {})) != "admin":
        raise HTTPException(403, "An administrator must manage global guidance")
    return _require_writer(request)


def build_review_router(
    service: ReviewQueueService,
    details: ReviewDetailService,
    ask: AskGraphService,
    auth_required: bool,
    search: MemorySearchService,
    *,
    ci_github_repository: str = "",
    ci_project_key: str = "",
    tracker_project_key: str | None = None,
    ci_head: Callable[[], str | None] | None = None,
    ci_repository_key: str = "",
    test_fixes: TestFixService | None = None,
    changes: ProjectChangesService | None = None,
    context_packages: ContextPackageService | None = None,
    usage: TokenUsageService | None = None,
) -> APIRouter:
    async def authorize_project_route(request: Request):
        access = current_project_access.get()
        route = request.scope.get("route")
        if access and route and route.path.startswith(f"{REVIEW_API_PREFIX}/projects/{{key}}"):
            access.require(request.path_params["key"], write=request.method not in {"GET", "HEAD"})

    router = APIRouter(prefix=REVIEW_API_PREFIX, tags=["Review"], dependencies=[Depends(authorize_project_route)])

    def health_for_project(key: str, run: TestRunDetail | None) -> ProjectCIHealth:
        comparable = key == ci_project_key and (run is None or run.repository_key == ci_repository_key)
        head_commit = ci_head() if ci_head and comparable else None
        return project_ci_health(run, head_commit)

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

    async def measure(request, response, method, query, payload, **options):
        if usage is None:
            return
        receipt = await usage.record(request, method, query, payload, **options)
        headers = response.headers
        headers["X-MemoryGraph-Request-ID"] = receipt.request_id
        headers["X-MemoryGraph-Retrieval-Method"] = receipt.method
        headers["X-MemoryGraph-Query-Tokens"] = str(receipt.query_tokens)
        headers["X-MemoryGraph-Context-Tokens"] = str(receipt.context_tokens)
        headers["X-MemoryGraph-Total-Tokens"] = str(receipt.total_tokens)
        headers["X-MemoryGraph-Retrieval-Path"] = quote(receipt.retrieval_path)
        if receipt.naive_rag_tokens is not None:
            headers["X-MemoryGraph-Baseline-Tokens"] = str(receipt.naive_rag_tokens)
        if receipt.savings_tokens is not None:
            headers["X-MemoryGraph-Savings-Tokens"] = str(receipt.savings_tokens)

    @router.post("/context-package")
    async def build_context_package(
        query: ContextQuery, request: Request, response: Response,
    ) -> MeasuredContextPackage:
        if context_packages is None or usage is None:
            raise HTTPException(503, "Context packages are unavailable")
        try:
            package, baseline, count = await context_packages.build(query)
        except ArchitectureGraphError as exc:
            raise HTTPException(404, str(exc)) from exc
        receipt = await usage.record(
            request, "ContextPackage", query.question, package, project=query.project_key,
            baseline=baseline, entities=count,
            path="graph and document evidence → ADR conflict lookup → budgeted context",
        )
        response.headers["Cache-Control"] = "no-store"
        return MeasuredContextPackage(**package.model_dump(), usage=receipt)

    @router.get("/token-usage")
    async def token_usage(
        request: Request, response: Response, scope: UsageScope = "session", project_key: MemoryKey | None = None,
    ) -> UsageSummary:
        if usage is None:
            raise HTTPException(503, "Token analytics are unavailable")
        if scope == "project":
            if not project_key:
                raise HTTPException(422, "Provide a project key for project totals")
            if details.architecture is None or not isinstance(
                await anyio.to_thread.run_sync(details.architecture.read_entity, project_key), Project,
            ):
                raise HTTPException(404, "Project not found")
        response.headers["Cache-Control"] = "no-store"
        return await usage.summary(request, scope, project_key)

    @router.get("/token-usage/{request_id}")
    async def retrieval_usage(request_id: RequestID, request: Request, response: Response) -> UsageReceipt:
        if usage is None:
            raise HTTPException(503, "Token analytics are unavailable")
        receipt = await usage.read(request, request_id)
        if receipt is None:
            raise HTTPException(404, "Usage request not found")
        response.headers["Cache-Control"] = "no-store"
        return receipt

    @router.post("/token-usage/{request_id}/outcome")
    async def report_usage(
        request_id: RequestID, outcome: UsageOutcome, request: Request, response: Response,
    ) -> UsageReceipt:
        if usage is None:
            raise HTTPException(503, "Token analytics are unavailable")
        receipt = await usage.report(request, request_id, outcome)
        if receipt is None:
            raise HTTPException(404, "Usage request not found")
        response.headers["Cache-Control"] = "no-store"
        return receipt

    @router.post("/ask")
    async def ask_graph(query: AskGraphQuery, response: Response, request: Request) -> AskGraphAnswer:
        response.headers["Cache-Control"] = "no-store"
        evidence = AskEvidence()
        result = await ask.answer(query, await resolve_project(query.project_key), evidence=evidence)
        await measure(
            request, response, "Lexical" if evidence.lexical else "Graph", query.question,
            result, project=query.project_key, baseline=evidence.baseline,
            entities=len({(citation.key, citation.destination) for finding in result.findings
                          for citation in finding.citations}), path=evidence.path,
        )
        return result

    @router.post("/search")
    async def search_memory(query: ReviewSearchQuery, response: Response, request: Request) -> ReviewSearchResults:
        response.headers["Cache-Control"] = "no-store"
        retrieve = search.semantic_search if query.mode == "semantic" else search.hybrid_search
        if query.project_key:
            project = await details.get_project(query.project_key, "", query.as_of)
            if project is None:
                raise HTTPException(status_code=404, detail="Project not found")
            if not project.documents:
                result = ReviewSearchResults(matches=[], fallback_used=False)
                await measure(request, response, query.mode.title(), query.query, [],
                              project=query.project_key, path="project scope → no candidate documents")
                return result
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
        result = ReviewSearchResults(
            matches=matches, fallback_used=any(hit.fallback_used for hit in hits),
            embedding=diagnostics,
        )

        actual = "Lexical" if hits and all(hit.retrieval_mode == "lexical" for hit in hits) else query.mode.title()
        await measure(request, response, actual, query.query, matches,
                      project=query.project_key, baseline=document_baseline(hit.document for hit in hits),
                      entities=len(matches), path=f"{query.mode} → {actual.lower()} excerpts")
        return result

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

    @router.get("/projects/{key}/guidance")
    async def get_guidance_editor(
        key: MemoryKey, request: Request, response: Response,
        before_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> GuidanceEditor:
        claims = getattr(request.state, "auth_claims", {})
        access = current_project_access.get()
        project_writer = access is None or access.unrestricted or access.grants.get(key) == "write"
        result = await details.get_guidance_editor(
            key, project_writer and writer_subject(claims) is not None, access_role(claims) == "admin", before_version,
        )
        if result is None:
            raise HTTPException(404, "Project not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.post("/projects/{key}/guidance/draft")
    async def save_guidance_draft(
        key: MemoryKey, draft: GuidanceDraftInput, request: Request, response: Response,
    ) -> GuidanceEditor:
        actor = _require_writer(request)
        if await details.get_guidance_editor(key, False, False, None) is None:
            raise HTTPException(404, "Project not found")
        try:
            await details.change_guidance(key, draft.revision, actor, text=draft.text)
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await get_guidance_editor(key, request, response)

    @router.post("/projects/{key}/guidance/approve")
    async def approve_guidance(
        key: MemoryKey, approval: GuidanceApproval, request: Request, response: Response,
    ) -> GuidanceEditor:
        claims = getattr(request.state, "auth_claims", {})
        actor = _require_writer(request)
        if access_role(claims) != "admin":
            raise HTTPException(403, "An administrator must approve project guidance")
        if await details.get_guidance_editor(key, False, False, None) is None:
            raise HTTPException(404, "Project not found")
        try:
            await details.change_guidance(key, approval.revision, actor)
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await get_guidance_editor(key, request, response)

    @router.get("/guidance/global")
    async def get_global_guidance_editor(
        request: Request, response: Response,
        before_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> GuidanceEditor:
        _require_guidance_admin(request)
        result = await details.get_guidance_editor(None, True, True, before_version)
        if result is None:
            raise HTTPException(503, "Global guidance is unavailable")
        response.headers["Cache-Control"] = "no-store"
        return result

    async def change_global_guidance(
        request: Request, response: Response, revision: int, *, text: str | None = None,
    ) -> GuidanceEditor:
        actor = _require_guidance_admin(request)
        try:
            await details.change_guidance(None, revision, actor, text=text)
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await get_global_guidance_editor(request, response)

    @router.post("/guidance/global/draft")
    async def save_global_guidance_draft(
        draft: GuidanceDraftInput, request: Request, response: Response,
    ) -> GuidanceEditor:
        return await change_global_guidance(request, response, draft.revision, text=draft.text)

    @router.post("/guidance/global/approve")
    async def approve_global_guidance(
        approval: GuidanceApproval, request: Request, response: Response,
    ) -> GuidanceEditor:
        return await change_global_guidance(request, response, approval.revision)

    @router.get("/projects/{key}/tracker")
    async def get_tracker_overview(key: MemoryKey, response: Response) -> TrackerOverview:
        result = await details.get_tracker_overview(key, tracker_project_key)
        if result is None:
            raise HTTPException(status_code=404, detail="Project not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/projects/{key}/changes")
    async def get_project_changes(
        key: MemoryKey, response: Response, view: ChangeView = "pulls",
        repository_key: MemoryKey | None = None,
    ) -> ProjectChanges:
        if changes is None:
            raise HTTPException(503, "Project change history is unavailable")
        try:
            result = await changes.history(key, view, repository_key)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "Project not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/projects/{key}/testing")
    async def get_project_testing(
        key: MemoryKey, request: Request, response: Response, run_id: TestRunID | None = None,
    ) -> ProjectTestRuns:
        exists, run = await details.get_project_testing(key, run_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Project not found")
        if run_id and run is None:
            raise HTTPException(status_code=404, detail="Test run not found for this project")
        response.headers["Cache-Control"] = "no-store"
        def github_attempt(item: TestRunDetail | None) -> str | None:
            if not item or not ci_github_repository or item.repository_key != ci_repository_key:
                return None
            identifier, attempt = item.run_id.split(":")
            return f"https://github.com/{ci_github_repository}/actions/runs/{identifier}/attempts/{attempt}"

        passing = None
        if run and not run_id:
            passing = run if run.status == "passed" else await details.latest_passing_test_run(key)
        signed_in = bool(getattr(request.state, "auth_claims", {}).get("subject"))
        writer = bool(writer_subject(getattr(request.state, "auth_claims", {})))
        can_draft = bool(
            signed_in and run and test_fixes and test_fixes.available(run)
            and test_fixes.drafts_available()
        )
        return ProjectTestRuns(
            latest_run=run, run_url=github_attempt(run),
            can_suggest_fix=bool(signed_in and run and test_fixes and test_fixes.available(run)),
            can_review_fix=signed_in,
            can_draft_fix=can_draft,
            can_apply_draft=can_draft and writer,
            can_complete_fix=writer,
            can_publish_fix=bool(
                writer and test_fixes and test_fixes.settings.ci_github_publish_token
                and test_fixes.settings.ci_github_publish_token.get_secret_value()
            ),
            last_passing_run=LastPassingRun(
                run_id=passing.run_id, finished_at=passing.finished_at,
                run_url=github_attempt(passing),
            ) if passing else None,
            health=ci_run_health(run) if run_id else health_for_project(key, run).health,
        )

    @router.get("/projects/{key}/testing/health")
    async def get_project_ci_health(key: MemoryKey, response: Response) -> ProjectCIHealth:
        exists, run = await details.get_project_testing(key)
        if not exists:
            raise HTTPException(404, "Project not found")
        response.headers["Cache-Control"] = "no-store"
        return health_for_project(key, run)

    @router.get("/projects/{key}/testing/history")
    async def get_test_run_history(
        key: MemoryKey,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
        cursor: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
    ) -> TestRunHistory:
        history = await details.get_test_run_history(key, limit, cursor)
        if history is None:
            raise HTTPException(404, "Project not found")
        response.headers["Cache-Control"] = "no-store"
        return history

    @router.get("/projects/{key}/testing/recurring-failures")
    async def get_recurring_test_failures(key: MemoryKey, response: Response) -> RecurringTestFailures:
        result = await details.get_recurring_test_failures(key)
        if result is None:
            raise HTTPException(404, "Project not found")
        response.headers["Cache-Control"] = "no-store"
        return result

    async def resolve_test_issue(key: str, payload: TestFixRequest):
        exists, run = await details.get_project_testing(key, payload.run_id)
        if not exists or not run:
            raise HTTPException(404, "No test run found for this project")
        issues = run.warnings if payload.issue_type == "warning" else run.failures
        if payload.failure_index >= len(issues):
            raise HTTPException(404, "CI issue not found")
        return run, issues[payload.failure_index]

    def require_signed_in(request: Request) -> None:
        if not getattr(request.state, "auth_claims", {}).get("subject"):
            raise HTTPException(403, "Sign in to review CI fixes")

    async def save_workflow(workflow: FixWorkflow, **updates) -> FixWorkflow:
        try:
            return await details.save_fix_workflow(workflow, **updates)
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc

    def workflow_view(workflow: FixWorkflow) -> FixWorkflowView:
        return FixWorkflowView.model_validate(workflow, from_attributes=True)

    @router.get("/projects/{key}/testing/fix-workflow")
    async def get_fix_workflow(
        key: MemoryKey, issue: Annotated[TestFixRequest, Query()],
        request: Request, response: Response,
    ) -> FixWorkflowView:
        require_signed_in(request)
        await resolve_test_issue(key, issue)
        response.headers["Cache-Control"] = "no-store"
        return workflow_view(await details.read_fix_workflow(key, issue))

    @router.post("/projects/{key}/testing/suggest-fix")
    async def suggest_test_fix(
        key: MemoryKey, payload: FixWorkflowRequest, request: Request, response: Response,
    ) -> TestFixSuggestion:
        require_signed_in(request)
        run, issue = await resolve_test_issue(key, payload)
        workflow = await details.read_fix_workflow(key, payload)
        response.headers["Cache-Control"] = "no-store"
        if workflow.suggestion and not payload.rerun:
            return workflow.suggestion
        if workflow.applying_at or workflow.applied_at:
            raise HTTPException(409, "The reviewed patch is being applied or has already been applied.")
        if not test_fixes or not test_fixes.available(run):
            raise HTTPException(503, "Fix suggestions are unavailable for this repository")
        try:
            suggestion = await test_fixes.suggest(run, issue)
            await save_workflow(workflow, suggestion=suggestion, suggested_at=datetime.now(UTC))
            return suggestion
        except TestFixBusy as exc:
            raise HTTPException(429, "A suggestion is already being generated. Try again soon.") from exc
        except TestFixUnavailable as exc:
            raise HTTPException(502, "Could not generate a fix suggestion. Please try again.") from exc

    @router.post("/projects/{key}/testing/draft-fix")
    async def draft_test_fix(
        key: MemoryKey, payload: FixWorkflowRequest, request: Request, response: Response,
    ) -> TestDraft:
        require_signed_in(request)
        run, issue = await resolve_test_issue(key, payload)
        workflow = await details.read_fix_workflow(key, payload)
        response.headers["Cache-Control"] = "no-store"
        if workflow.draft and not payload.rerun:
            return workflow.draft
        if workflow.applying_at or workflow.applied_at:
            raise HTTPException(409, "This draft has already been approved for application.")
        if not workflow.suggestion:
            raise HTTPException(409, "Generate a suggestion before drafting a patch.")
        if not test_fixes or not test_fixes.available(run) or not test_fixes.drafts_available():
            raise HTTPException(503, "The local draft worker is unavailable.")
        try:
            draft = await test_fixes.draft(run, issue, workflow.suggestion.proposed_fix)
            now = datetime.now(UTC)
            await save_workflow(workflow, draft=draft, drafted_at=now, verified_at=now)
            return draft
        except DraftWorkerError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except TestFixBusy as exc:
            raise HTTPException(429, "A fix request is already running.") from exc
        except TestFixUnavailable as exc:
            raise HTTPException(502, "Could not generate a patch.") from exc

    @router.post("/projects/{key}/testing/verify-fix")
    async def rerun_test_draft(
        key: MemoryKey, payload: TestFixRequest, request: Request, response: Response,
    ) -> FixWorkflowView:
        require_signed_in(request)
        await resolve_test_issue(key, payload)
        workflow = await details.read_fix_workflow(key, payload)
        if not workflow.draft or workflow.applying_at or workflow.applied_at:
            raise HTTPException(409, "Pre-verification requires a saved draft that has not been applied.")
        if not test_fixes or not test_fixes.drafts_available():
            raise HTTPException(503, "The local draft worker is unavailable.")
        try:
            updated = await test_fixes.verify_draft(workflow.draft)
            response.headers["Cache-Control"] = "no-store"
            return workflow_view(await save_workflow(
                workflow, draft=updated, verified_at=datetime.now(UTC),
            ))
        except DraftWorkerError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.post("/projects/{key}/testing/drafts/{draft_id}/apply")
    async def approve_test_draft(
        key: MemoryKey, draft_id: DraftID, approval: DraftApproval, request: Request,
    ) -> dict[str, bool]:
        actor = _require_writer(request)
        await resolve_test_issue(key, approval)
        workflow = await details.read_fix_workflow(key, approval)
        draft = workflow.draft
        if not draft or draft.draft_id != draft_id or not draft.verified:
            raise HTTPException(409, "The reviewed draft does not match the saved verified patch.")
        if workflow.fix_branch:
            return {"applied": True}
        now = datetime.now(UTC)
        if workflow.applying_at and now - workflow.applying_at < timedelta(minutes=3):
            raise HTTPException(409, "This patch is already being applied.")
        if not test_fixes or not test_fixes.drafts_available():
            raise HTTPException(503, "The local draft worker is unavailable.")
        claimed = await save_workflow(workflow, applying_at=now)
        try:
            fix = await test_fixes.apply(key, draft)
        except DraftWorkerError as exc:
            await save_workflow(claimed, applying_at=None)
            raise HTTPException(exc.status, str(exc)) from exc
        await save_workflow(
            claimed, applying_at=None, applied_at=datetime.now(UTC), applied_by=actor,
            fix_branch=fix, post_verification=fix.verification, post_verified_at=datetime.now(UTC),
        )
        return {"applied": True}

    @router.post("/projects/{key}/testing/post-verify-fix")
    async def post_verify_test_draft(
        key: MemoryKey, payload: TestFixRequest, request: Request, response: Response,
    ) -> FixWorkflowView:
        _require_writer(request)
        await resolve_test_issue(key, payload)
        workflow = await details.read_fix_workflow(key, payload)
        if not workflow.draft or not workflow.applied_at or workflow.completed_at or workflow.fix_branch:
            raise HTTPException(409, "Post-verification requires an applied, open patch.")
        if not test_fixes or not test_fixes.drafts_available():
            raise HTTPException(503, "The local draft worker is unavailable.")
        try:
            result = await test_fixes.post_verify_draft(workflow.draft)
        except DraftWorkerError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return workflow_view(await save_workflow(
            workflow, post_verification=result, post_verified_at=datetime.now(UTC),
        ))

    @router.post("/projects/{key}/testing/complete-fix")
    async def complete_test_fix(
        key: MemoryKey, payload: TestFixRequest, request: Request, response: Response,
    ) -> FixWorkflowView:
        actor = _require_writer(request)
        await resolve_test_issue(key, payload)
        workflow = await details.read_fix_workflow(key, payload)
        response.headers["Cache-Control"] = "no-store"
        if workflow.completed_at:
            return workflow_view(workflow)
        if workflow.fix_branch or not workflow.applied_at or not workflow.post_verification or not workflow.post_verification.passed:
            raise HTTPException(409, "Post-verification must pass before completing the fix.")
        return workflow_view(await save_workflow(
            workflow, completed_at=datetime.now(UTC), completed_by=actor,
        ))

    @router.post("/projects/{key}/testing/publish-fix")
    async def publish_test_fix(
        key: MemoryKey, approval: PublishApproval, request: Request, response: Response,
    ) -> FixWorkflowView:
        actor = _require_writer(request)
        run, _ = await resolve_test_issue(key, approval)
        workflow = await details.read_fix_workflow(key, approval)
        fix = workflow.fix_branch
        if not workflow.draft or not fix or not fix.verification.passed or fix.commit != approval.commit:
            raise HTTPException(409, "Approval must match the verified fix commit.")
        response.headers["Cache-Control"] = "no-store"
        if workflow.publication:
            return workflow_view(workflow)
        if not test_fixes:
            raise HTTPException(503, "GitHub publishing is unavailable.")
        now = datetime.now(UTC)
        if workflow.publishing_at and now - workflow.publishing_at < timedelta(minutes=5):
            raise HTTPException(409, "This fix is already being published.")
        claimed = await save_workflow(workflow, publishing_at=now)
        try:
            publication = await test_fixes.publish(fix, workflow.draft, run)
        except DraftWorkerError as exc:
            await save_workflow(claimed, publishing_at=None)
            raise HTTPException(exc.status, str(exc)) from exc
        return workflow_view(await save_workflow(
            claimed, publishing_at=None, publication=publication,
            published_at=datetime.now(UTC), published_by=actor,
        ))

    @router.post("/projects/{key}/testing/refresh-fix")
    async def refresh_test_fix(
        key: MemoryKey, payload: TestFixRequest, request: Request, response: Response,
    ) -> FixWorkflowView:
        require_signed_in(request)
        original, issue = await resolve_test_issue(key, payload)
        workflow = await details.read_fix_workflow(key, payload)
        fix, publication = workflow.fix_branch, workflow.publication
        if not test_fixes or not publication or not fix:
            raise HTTPException(409, "A published fix is required.")
        if publication.state != "merged":
            try:
                publication = await test_fixes.refresh_publication(fix, publication.number)
            except DraftWorkerError as exc:
                raise HTTPException(exc.status, str(exc)) from exc
        confirming = workflow.confirming_run_id
        ci_run_id = workflow.merge_ci_run_id
        ci_status = workflow.merge_ci_status
        if not confirming and publication.merge_commit and publication.merged_at:
            run = await details.get_test_run_for_commit(
                key, original.repository_key, publication.merge_commit,
            )
            if run and run.finished_at >= publication.merged_at:
                ci_run_id = run.run_id
                if run.status == "failed" or run.build_status == "failed":
                    ci_status = "failed"
                else:
                    ci_status = "inconclusive"
                    if run.status == "passed":
                        if payload.issue_type == "warning":
                            if run.warning_count == len(run.warnings) and not any(
                                warning.name == issue.name and warning.stage == issue.stage
                                for warning in run.warnings
                            ):
                                confirming = run.run_id
                        elif run.passed > 0 and run.skipped == 0:
                            confirming = run.run_id
                    if confirming:
                        ci_status = "confirmed"
        response.headers["Cache-Control"] = "no-store"
        updates = {
            "publication": publication, "confirming_run_id": confirming,
            "merge_ci_run_id": ci_run_id, "merge_ci_status": ci_status,
        }
        if all(getattr(workflow, field) == value for field, value in updates.items()):
            return workflow_view(workflow)
        return workflow_view(await save_workflow(workflow, **updates))

    @router.post("/projects/{key}/testing/finish-fix/{action}")
    async def finish_published_fix(
        key: MemoryKey, action: Literal["merge", "delete-branch"],
        approval: PublishApproval, request: Request, response: Response,
    ) -> FixWorkflowView:
        actor = _require_writer(request)
        run, _ = await resolve_test_issue(key, approval)
        workflow = await details.read_fix_workflow(key, approval)
        fix, publication = workflow.fix_branch, workflow.publication
        if not fix or not publication or not workflow.draft or approval.commit != fix.commit:
            raise HTTPException(409, "Approval must match the published fix commit.")
        if (
            not test_fixes or run.repository_key != test_fixes.settings.ci_repository_key
            or not test_fixes.settings.ci_github_publish_token
            or not test_fixes.settings.ci_github_publish_token.get_secret_value()
        ):
            raise HTTPException(503, "GitHub writing is unavailable for this repository.")
        response.headers["Cache-Control"] = "no-store"
        try:
            if action == "merge":
                if publication.state == "merged":
                    return workflow_view(workflow)
                merged = await test_fixes.merge_publication(fix, publication.number)
                return workflow_view(await save_workflow(
                    workflow, publication=merged, merged_by=actor,
                ))
            if publication.state != "merged" or not workflow.confirming_run_id:
                raise HTTPException(409, "The fix must be merged and confirmed by CI.")
            if workflow.branch_deleted_at:
                return workflow_view(workflow)
            await test_fixes.delete_remote_branch(fix, workflow.draft)
            return workflow_view(await save_workflow(
                workflow, branch_deleted_at=datetime.now(UTC), branch_deleted_by=actor,
            ))
        except DraftWorkerError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.get("/graph/metamodel")
    async def get_graph_metamodel(response: Response) -> ArchitectureMetamodel:
        response.headers["Cache-Control"] = "no-store"
        return ArchitectureMetamodel(
            entity_types=list(get_args(EntityType)), relationships=FORWARD_RELATIONSHIPS
        )

    @router.get("/graph/entities")
    async def search_graph_entities(
        query: Annotated[ArchitectureSearch, Query()], response: Response, request: Request
    ) -> list[ArchitectureEntity]:
        response.headers["Cache-Control"] = "no-store"
        result = await details.search_graph(query)
        await measure(request, response, "Graph", query.query, result,
                      entities=len(result), path="architecture entity search")
        return result

    @router.get("/graph/entities/{key}")
    async def get_graph_neighborhood(
        key: MemoryKey,
        response: Response, request: Request,
        entity_type: EntityType | None = None,
        hops: Annotated[int, Query(ge=1, le=MAX_GRAPH_HOPS)] = DEFAULT_GRAPH_HOPS,
        entity_types: Annotated[list[EntityType] | None, Query(max_length=18)] = None,
        project_key: MemoryKey | None = None,
    ) -> ArchitectureNeighborhood:
        result = await details.get_neighborhood(
            key, entity_types or ([entity_type] if entity_type else []), hops
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Architecture entity not found")
        response.headers["Cache-Control"] = "no-store"
        await measure(request, response, "Graph", key, result, project=project_key,
                      entities=len({result.center.key, *(path.entity.key for path in result.paths)}),
                      path="graph neighborhood traversal")
        return result

    @router.get("/records/{key}/similar")
    async def get_similar_records(
        key: MemoryKey, response: Response, request: Request, limit: Annotated[int, Query(ge=1, le=20)] = 6
    ) -> SimilarRecords:
        result = await details.similar_records(key, limit)
        if result is None:
            raise HTTPException(status_code=404, detail="Memory record not found")
        response.headers["Cache-Control"] = "no-store"
        await measure(request, response, "Semantic", key, result, entities=len(result.matches),
                      path="stored-vector similar records")
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
