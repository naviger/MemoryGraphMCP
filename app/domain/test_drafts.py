from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from app.domain.models import MemoryKey
from app.domain.test_runs import TestFixRequest

DraftID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class DraftEdit(BaseModel):
    original: str = Field(min_length=1, max_length=4000)
    replacement: str = Field(min_length=1, max_length=5000)


class DraftCheck(BaseModel):
    command: str
    passed: bool
    output: str


class ProposedTestFix(BaseModel):
    cause: str = Field(min_length=1, max_length=1200)
    proposed_fix: str = Field(min_length=1, max_length=2500)
    verification: str = Field(min_length=1, max_length=1200)


class TestFixSuggestion(ProposedTestFix):
    model: str
    source_paths: list[str]
    evidence_gaps: list[str]


class TestDraft(BaseModel):
    draft_id: DraftID
    diff: str
    baseline: DraftCheck
    verification: DraftCheck
    verified: bool


class WorkerDraft(TestDraft):
    project: MemoryKey
    commit: str
    path: str
    name: str
    issue_type: Literal["failure", "warning"]
    stage: Literal["build", "test"]
    preimage: str
    postimage: str
    signature: str = ""


class DraftApproval(TestFixRequest):
    approve: Literal[True]


class FixWorkflowRequest(TestFixRequest):
    rerun: bool = False


class PostVerification(BaseModel):
    commit: str
    check: DraftCheck
    passed: bool


class FixBranch(BaseModel):
    branch: Annotated[str, StringConstraints(
        pattern=r"^fix/(?:ci-[0-9a-f]{32}|(?:test|warning)-[a-z0-9-]{1,48}-[0-9a-f]{12})$",
    )]
    base_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    verification: PostVerification


class FixPublication(BaseModel):
    number: int = Field(gt=0)
    url: str
    state: Literal["open", "closed", "merged"]
    merged_at: AwareDatetime | None = None
    merge_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")] | None = None


class PublishApproval(DraftApproval):
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class FixWorkflow(BaseModel):
    project_key: MemoryKey
    issue: TestFixRequest
    revision: int = 0
    publishing_at: AwareDatetime | None = None
    suggestion: TestFixSuggestion | None = None
    suggested_at: AwareDatetime | None = None
    draft: WorkerDraft | None = None
    drafted_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    applying_at: AwareDatetime | None = None
    applied_at: AwareDatetime | None = None
    applied_by: str | None = None
    post_verification: PostVerification | None = None
    post_verified_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    completed_by: str | None = None
    fix_branch: FixBranch | None = None
    publication: FixPublication | None = None
    published_at: AwareDatetime | None = None
    published_by: str | None = None
    confirming_run_id: str | None = None
    merge_ci_run_id: str | None = None
    merge_ci_status: Literal["failed", "inconclusive", "confirmed"] | None = None
    merged_by: str | None = None
    branch_deleted_at: AwareDatetime | None = None
    branch_deleted_by: str | None = None


class FixWorkflowView(BaseModel):
    suggestion: TestFixSuggestion | None = None
    suggested_at: AwareDatetime | None = None
    draft: TestDraft | None = None
    drafted_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    applied_at: AwareDatetime | None = None
    applied_by: str | None = None
    post_verification: PostVerification | None = None
    post_verified_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    completed_by: str | None = None
    fix_branch: FixBranch | None = None
    publication: FixPublication | None = None
    published_at: AwareDatetime | None = None
    published_by: str | None = None
    confirming_run_id: str | None = None
    merge_ci_run_id: str | None = None
    merge_ci_status: Literal["failed", "inconclusive", "confirmed"] | None = None
    merged_by: str | None = None
    branch_deleted_at: AwareDatetime | None = None
    branch_deleted_by: str | None = None
