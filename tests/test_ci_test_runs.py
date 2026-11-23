from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.test_runs import build_test_run_router
from app.config.settings import Settings
from scripts.publish_test_run import read_report


def test_ci_report_counts_cases_and_omits_failure_output(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text("""
        <testsuites><testsuite>
          <testcase name="test_ok"/>
          <testcase name="test_skip"><skipped/></testcase>
          <testcase name="test_fail" classname="tests.test_sample" file="tests/test_sample.py">
            <failure message="private details">Private output
E               jwt.exceptions.InvalidAudienceError: private details
E               AuthenticationError: private wrapper details
            </failure>
          </testcase>
          <testcase name="test_error"><error/></testcase>
        </testsuite></testsuites>
    """)
    assert read_report(report) == {
        "passed": 1,
        "failed": 2,
        "skipped": 1,
        "failures": [
            {
                "name": "tests.test_sample.test_fail",
                "kind": "failure",
                "source_path": "tests/test_sample.py",
                "exception_type": "InvalidAudienceError",
            },
            {"name": "test_error", "kind": "error", "source_path": None},
        ],
    }


def test_ci_publication_requires_dedicated_token_and_pins_project_and_repo():
    class Graph:
        def put_test_run(self, run):
            assert (run.project_key, run.repository_key) == ("project", "repo")
            return run

    payload = {
        "run_id": "100:1",
        "commit": "a" * 40,
        "status": "failed",
        "finished_at": "2026-11-22T12:00:00Z",
        "passed": 2,
        "failed": 1,
        "skipped": 0,
        "failures": [],
    }
    path = "/api/ci/test-runs"
    disabled = FastAPI()
    disabled.include_router(
        build_test_run_router(
            Settings(_env_file=None, auth_required=False),
            Graph(),
        )
    )
    assert TestClient(disabled).post(path, json=payload).status_code == 404

    app = FastAPI()
    app.include_router(
        build_test_run_router(
            Settings(
                _env_file=None,
                auth_required=False,
                ci_test_run_token="ci-only",
                ci_project_key="project",
                ci_repository_key="repo",
            ),
            Graph(),
        )
    )
    client = TestClient(app)
    assert client.post(path, json=payload).status_code == 401
    headers = {"Authorization": "Bearer ci-only"}
    cross_project = client.post(path, headers=headers, json={**payload, "project_key": "other"})
    assert cross_project.status_code == 422
    invalid_status = client.post(path, headers=headers, json={**payload, "status": "passed"})
    assert invalid_status.status_code == 422
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["source"] == "github-actions"


async def test_local_pull_backfills_missing_attempt_and_skips_known_runs_and_forks(monkeypatch):
    from hashlib import sha256
    from io import BytesIO
    from zipfile import ZipFile

    import httpx

    from app.domain.test_runs import TestRunInput
    from app.services import github_test_runs

    report = TestRunInput(
        run_id="10:1",
        commit="a" * 40,
        finished_at="2026-11-22T12:00:00Z",
        status="passed",
        passed=4,
        failed=0,
        skipped=1,
        failures=[],
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("test-run.json", report.model_dump_json())
    data = buffer.getvalue()
    run = {
        "id": 10,
        "run_attempt": 1,
        "head_sha": report.commit,
        "head_branch": "main",
        "event": "push",
        "head_repository": {"full_name": "example/project"},
    }
    run["updated_at"] = "2026-11-22T12:00:00Z"
    known = {**run, "id": 12, "updated_at": "2026-11-22T13:00:00Z"}
    fork = {**run, "id": 11, "head_repository": {"full_name": "fork/project"}}
    visited = []

    def respond(request):
        path = request.url.path
        visited.append(path)
        assert request.headers["Authorization"] == "Bearer read-only"
        if path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"sha": "b" * 40}})
        if path.endswith("/workflows/tests.yml/runs"):
            return httpx.Response(200, json={"workflow_runs": [known, fork, run]})
        if path.endswith("/runs/10/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "id": 2,
                            "name": "memorygraph-test-run-1",
                            "expired": False,
                            "size_in_bytes": len(data),
                            "digest": f"sha256:{sha256(data).hexdigest()}",
                        }
                    ]
                },
            )
        assert path.endswith("/artifacts/2/zip")
        return httpx.Response(200, content=data)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        github_test_runs.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(respond), **kwargs),
    )

    class Graph:
        def __init__(self):
            self.stored = None

        def known_test_run_ids(self, repository_key, run_ids):
            assert repository_key == "repository"
            assert run_ids == ["10:1", "12:1"]
            return {"12:1", "10:1"} if self.stored else {"12:1"}

        def put_test_run(self, result):
            self.stored = result

    graph = Graph()
    settings = Settings(
        _env_file=None,
        auth_required=False,
        ci_project_key="project",
        ci_repository_key="repository",
        ci_github_repository="example/project",
        ci_github_read_token="read-only",
    )
    head = github_test_runs.GitHubBranchHead()
    await github_test_runs.pull_test_runs(settings, graph, head)
    await github_test_runs.pull_test_runs(settings, graph, head)
    assert visited.count("/repos/example/project/actions/runs/10/artifacts") == 1
    assert "/repos/example/project/actions/runs/12/artifacts" not in visited
    assert head.current() == "b" * 40
    assert "/repos/example/project/actions/runs/11/artifacts" not in visited
    assert graph.stored.run_id == report.run_id
    assert graph.stored.passed == 4
    assert graph.stored.repository_key == "repository"


def test_recurring_failures_count_distinct_attempts_by_test_and_file():
    from types import SimpleNamespace

    from app.repositories.arango_architecture import ArangoArchitectureRepository

    failure = {"name": "test_shared", "source_path": "tests/test_one.py"}
    different_file = {**failure, "source_path": "tests/test_two.py"}
    rows = [
        {"run_id": "22:1", "finished_at": "2026-11-22T12:00:00Z", "failures": []},
        {
            "run_id": "21:2",
            "finished_at": "2026-11-22T11:00:00Z",
            "failures": [failure, failure, different_file],
        },
        {"run_id": "21:1", "finished_at": "2026-11-22T10:00:00Z", "failures": [failure]},
    ]

    def execute(query, bind_vars):
        assert "event.project_key == @key" in query and "LIMIT @limit" in query
        assert bind_vars["key"] == "project" and bind_vars["limit"] == 20
        return rows

    repository = ArangoArchitectureRepository(
        lambda: SimpleNamespace(
            aql=SimpleNamespace(execute=execute),
        )
    )
    history = repository.recurring_test_failures("project", 20)
    assert history.attempts_examined == 3
    (group,) = history.failures
    assert (group.name, group.source_path) == ("test_shared", "tests/test_one.py")
    assert [attempt.run_id for attempt in group.attempts] == ["21:2", "21:1"]


def test_ci_health_distinguishes_failures_warnings_and_missing_evidence():
    from app.domain.test_runs import TestRun, ci_run_health, project_ci_health

    run = TestRun(
        project_key="p",
        repository_key="r",
        run_id="1:1",
        commit="a" * 40,
        finished_at="2026-11-22T12:00:00Z",
        status="passed",
        passed=3,
        failed=0,
        skipped=0,
        failures=[],
    )
    assert ci_run_health(run) == "unknown"
    assert ci_run_health(run.model_copy(update={"build_status": "failed"})) == "red"
    assert ci_run_health(run.model_copy(update={"status": "failed"})) == "red"
    complete = run.model_copy(update={"build_status": "passed", "warning_count": 0})
    assert ci_run_health(complete) == "green"
    assert ci_run_health(complete.model_copy(update={"warning_count": 2})) == "yellow"
    assert project_ci_health(complete, complete.commit).freshness == "current"
    stale = project_ci_health(complete, "b" * 40)
    assert (stale.health, stale.freshness) == ("yellow", "stale")
    failed = complete.model_copy(update={"status": "failed"})
    assert project_ci_health(failed, "b" * 40).health == "red"
    no_tests = complete.model_copy(update={"status": "cancelled"})
    assert project_ci_health(no_tests, no_tests.commit).freshness == "unverified"
    assert project_ci_health(None, "b" * 40).freshness == "unverified"
    assert project_ci_health(complete, None).freshness == "unknown"


def test_build_warning_summary_omits_raw_output(tmp_path):
    from scripts.publish_test_run import build_warnings

    logs = tmp_path / "build.log"
    logs.write_text(
        "WARNING: arbitrary sensitive log content\nSome chunks are larger than 500 kB\n"
    )
    warnings = build_warnings(logs)
    assert [warning["name"] for warning in warnings] == [
        "Build warning",
        "Vite chunk size warning",
    ]
    assert all("sensitive" not in warning["message"] for warning in warnings)
