from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal, get_args

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
from app.domain.architecture_events import TrackerOverview
from app.domain.ask_graph import AskGraphAnswer, AskGraphQuery
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
from app.services.ask_graph import AskGraphService
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
    *,
    ci_github_repository: str = "",
    ci_project_key: str = "",
    tracker_project_key: str | None = None,
    ci_head: Callable[[], str | None] | None = None,
    ci_repository_key: str = "",
    test_fixes: TestFixService | None = None,
    changes: ProjectChangesService | None = None,
) -> APIRouter:
    router = APIRouter(prefix=REVIEW_API_PREFIX, tags=["Review"])

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
