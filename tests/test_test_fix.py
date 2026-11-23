import base64
import json

import httpx

from app.config.settings import Settings
from app.domain.architecture import Constraint
from app.domain.models import MemoryDocument
from app.domain.test_runs import TestRunDetail
from app.services import test_fix


async def test_fix_suggestion_uses_pinned_source_and_linked_architecture(monkeypatch):
    commit = "a" * 40
    source = "from app.auth.jwt import JWTVerifier\ndef test_auth():\n    JWTVerifier()"
    received = []

    def respond(request):
        if request.url.host == "api.github.com":
            assert request.url.params["ref"] == commit
            content = (
                source
                if request.url.path.endswith("/tests/test_auth.py")
                else "class JWTVerifier: pass"
            )
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "size": len(content),
                    "content": base64.b64encode(content.encode()).decode(),
                },
            )
        assert request.url.path == "/api/chat"
        assert "Authorization" not in request.headers
        prompt = json.loads(request.content)
        assert prompt["model"] == "qwen3-coder:30b"
        received.append(json.loads(prompt["messages"][1]["content"]))
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "cause": "Missing audience",
                            "proposed_fix": "Set the test audience",
                            "verification": "Run the focused pytest case",
                        }
                    )
                }
            },
        )

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        test_fix.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )

    class Architecture:
        def read_entity(self, key):
            assert key == "rule"
            return Constraint(key=key, name="Audience required", rule="Verify API audience")

    class Memory:
        def read_document(self, key):
            assert key == "adr"
            return MemoryDocument(key=key, title="OIDC", content="Use distinct API audiences")

    service = test_fix.TestFixService(
        Settings(
            _env_file=None,
            auth_required=False,
            ci_github_repository="example/repo",
            ci_repository_key="repo",
            test_fix_ollama_url="http://localhost:11434",
            ci_github_read_token="local-only",
        ),
        Architecture(),
        Memory(),
    )
    run = TestRunDetail(
        run_id="123:1",
        commit=commit,
        finished_at="2026-11-22T11:00:00Z",
        status="failed",
        passed=0,
        failed=1,
        skipped=0,
        project_key="project",
        repository_key="repo",
        failures=[
            {
                "name": "tests.test_auth.test_auth",
                "kind": "failure",
                "source_path": "tests/test_auth.py",
                "exception_type": "InvalidAudienceError",
                "validations": [
                    {"key": "rule", "name": "Audience required", "entity_type": "Constraint"},
                    {"key": "decision", "name": "OIDC", "entity_type": "ADR", "memory_key": "adr"},
                ],
            }
        ],
    )
    result = await service.suggest(run, run.failures[0])
    assert result.proposed_fix == "Set the test audience"
    assert result.source_paths == ["tests/test_auth.py", "app/auth/jwt.py"]
    assert received[0]["failure"]["exception_type"] == "InvalidAudienceError"
    assert [item["content"] for item in received[0]["linked_architecture"]] == [
        "Verify API audience",
        "Use distinct API audiences",
    ]


async def test_publish_recovers_existing_pr_and_refresh_uses_read_credentials(monkeypatch):
    from app.domain.test_drafts import FixBranch, WorkerDraft

    draft_id = "1" * 32
    base = "a" * 40
    commit = "b" * 40
    branch = f"fix/ci-{draft_id}"
    check = {"command": "pytest tests/test_example.py", "passed": True, "output": "1 passed"}
    fix = FixBranch(
        branch=branch, base_commit=base, commit=commit,
        verification={"commit": commit, "check": check, "passed": True},
    )
    draft = WorkerDraft(
        draft_id=draft_id, project="project", path="tests/test_example.py",
        name="test_example", issue_type="failure", stage="test", commit=base,
        diff="diff", baseline=check, verification=check, verified=True,
        preimage="original", postimage="patched",
    )
    run = TestRunDetail(
        run_id="123:1", commit=base, project_key="project", repository_key="repo",
        finished_at="2026-11-22T11:00:00Z", status="failed",
        passed=0, failed=1, skipped=0, failures=[],
    )
    pull = {
        "number": 7, "state": "open", "merged": False,
        "head": {"ref": branch, "sha": commit, "repo": {"full_name": "example/repo"}},
        "base": {"ref": "main", "repo": {"full_name": "example/repo"}},
    }
    opened = []
    credentials = []

    def respond(request):
        if request.url.path == "/repos/example/repo/pulls":
            assert request.headers["Authorization"] == "Bearer publish-credential"
            if request.method == "POST":
                body = json.loads(request.content)
                assert (body["head"], body["base"]) == (branch, "main")
                opened.append(pull)
                return httpx.Response(201, json=pull)
            assert request.url.params["head"] == "example:" + branch
            return httpx.Response(200, json=opened)
        credentials.append(request.headers["Authorization"])
        if request.url.path == "/repos/example/repo/pulls/7/merge":
            assert request.method == "PUT"
            assert json.loads(request.content) == {"sha": commit, "merge_method": "squash"}
            pull.update(
                merged=True, state="closed", merge_commit_sha="c" * 40,
                merged_at="2026-11-22T12:00:00Z",
            )
            return httpx.Response(200, json={"merged": True})
        assert request.url.path == "/repos/example/repo/pulls/7"
        return httpx.Response(200, json=pull)

    service = test_fix.TestFixService(
        Settings(
            _env_file=None, auth_required=False,
            ci_github_repository="example/repo", ci_repository_key="repo",
            ci_github_read_token="read-credential", ci_github_publish_token="publish-credential",
        ),
        None, None,
    )
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        test_fix.httpx, "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(respond), **kwargs),
    )

    async def worker(path, payload):
        assert path == "/push"
        assert payload["approve"] is True
        assert payload["fix"]["commit"] == commit
        return {"pushed": True}

    monkeypatch.setattr(service, "_worker", worker)
    publication = await service.publish(fix, draft, run)
    assert publication.state == "open"
    assert publication.url == "https://github.com/example/repo/pull/7"
    assert await service.publish(fix, draft, run) == publication
    assert len(opened) == 1
    merged = await service.merge_publication(fix, publication.number)
    assert all(credential == "Bearer publish-credential" for credential in credentials)
    assert await service.refresh_publication(fix, publication.number) == merged
    assert credentials[-1] == "Bearer read-credential"
    assert merged.state == "merged"
    assert merged.merge_commit == "c" * 40
