from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

CommitSHA = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
ChangeView = Literal["pulls", "commits"]


class ChangeRepository(BaseModel):
    key: str
    name: str
    github: str | None = None


class ChangeCI(BaseModel):
    run_id: str
    status: Literal["passed", "failed", "cancelled"]
    url: str


class BranchCleanup(BaseModel):
    branch: str
    deleted_at: AwareDatetime


class ProjectChange(BaseModel):
    repository_key: str
    title: str
    url: str
    author: str | None = None
    date: AwareDatetime
    commit: CommitSHA | None = None
    number: int | None = Field(default=None, gt=0)
    status: Literal["open", "draft", "merged", "closed"] | None = None
    ci: ChangeCI | None = None
    cleanup: BranchCleanup | None = None


class ProjectChanges(BaseModel):
    repositories: list[ChangeRepository]
    changes: list[ProjectChange]
    unavailable_repositories: list[str]
    limit_per_repository: int
