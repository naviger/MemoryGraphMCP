import httpx
import pytest

from app.config.settings import Settings
from app.domain.architecture import Project, Repository
from app.domain.project_changes import BranchCleanup
from app.domain.test_runs import TestRun
from app.services import project_changes

_MERGE = "a" * 40
_HEAD = "b" * 40
_DATE = "2026-11-22T12:00:00Z"


async def test_changes_use_read_only_github_and_match_exact_repository_and_commit(monkeypatch):
    class Graph:
        def read_entity(self, key):
            return Project(key=key, name="Example") if key == "project" else None

        def project_source_repositories(self, key):
            return [
                Repository(key="primary", name="Primary", role="source"),
                Repository(key="secondary", name="Secondary", role="source"),
            ]

        def change_test_runs(self, project, repository, commits):
            assert (project, repository, commits) == ("project", "primary", [_MERGE])
            return {_MERGE: TestRun(
                project_key=project, repository_key=repository, commit=_MERGE,
                run_id="72:1", finished_at=_DATE, status="passed",
                passed=4, failed=0, skipped=0, failures=[],
            )}

        def change_branch_cleanups(self, project, urls):
            assert project == "project" and urls == ["https://github.com/example/primary/pull/2"]
            return {urls[0]: BranchCleanup(
                branch="fix/test-example-aaaaaaaaaaaa", deleted_at=_DATE,
            )}

    requests = []

    def respond(request):
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer read-only-token"
        requests.append(request.url.path)
        if request.url.path.endswith("/pulls"):
            assert request.url.params["state"] == "all"
            return httpx.Response(200, json=[{
                "number": 2, "title": "Fix test", "merged_at": _DATE,
                "merge_commit_sha": _MERGE, "head": {"sha": _HEAD}, "state": "closed",
            }])
        return httpx.Response(200, json=[{
            "sha": _MERGE, "commit": {"message": "Fix test", "author": {"date": _DATE}},
        }])

    client = httpx.AsyncClient
    monkeypatch.setattr(project_changes.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    service = project_changes.ProjectChangesService(Settings(
        _env_file=None, auth_required=False, ci_project_key="project", ci_repository_key="primary",
        ci_github_repository="example/primary", ci_github_read_token="read-only-token",
        ci_github_publish_token="different-token",
        github_change_repositories="secondary=example/secondary",
    ), Graph())

    history = await service.history("project", "pulls", "primary")
    assert [repo.key for repo in history.repositories] == ["primary", "secondary"]
    pr = history.changes[0]
    assert (pr.number, pr.status, pr.commit, pr.ci.status) == (2, "merged", _MERGE, "passed")
    assert pr.ci.url.endswith("/72/attempts/1") and pr.cleanup.branch.startswith("fix/")
    commits = await service.history("project", "commits", "primary")
    assert commits.changes[0].url.endswith(f"/commit/{_MERGE}")
    assert commits.changes[0].cleanup is None
    assert requests == ["/repos/example/primary/pulls", "/repos/example/primary/commits"]
    with pytest.raises(LookupError):
        await service.history("project", "pulls", "another-project")
