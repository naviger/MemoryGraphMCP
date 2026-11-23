from hashlib import sha256
from importlib import import_module

from fastapi.testclient import TestClient

from app.domain.test_drafts import DraftCheck


def test_worker_applies_only_reviewed_verified_patch_after_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("MGMCP_DRAFT_WORKER_TOKEN", "worker-test-token")
    monkeypatch.setenv("MGMCP_DRAFT_REMOTE", "https://example.invalid/repo.git")
    worker = import_module("scripts.test_draft_worker")
    root = tmp_path.resolve()
    monkeypatch.setattr(worker, "ROOT", root)
    test_dir = root / "tests"
    test_dir.mkdir()
    source = test_dir / "test_example.py"
    original = "def test_example():\n    assert 1 == 2\n"
    replacement = original.replace("1 == 2", "1 == 1")
    source.write_text(original)
    assert worker.git(root, "init").returncode == 0
    assert worker.git(root, "add", str(source.relative_to(root))).returncode == 0
    assert worker.git(
        root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "Original CI source",
    ).returncode == 0
    head = worker.git(root, "rev-parse", "HEAD").stdout.strip()
    source.write_text(replacement)
    diff = worker.git(root, "diff", "--", str(source.relative_to(root))).stdout
    source.write_text(original)

    base = worker.git(root, "branch", "--show-current").stdout.strip()
    remote = root / "remote.git"
    assert worker.git(root, "clone", "--bare", str(root), str(remote)).returncode == 0
    monkeypatch.setattr(worker, "REMOTE", str(remote))
    draft_id = "a" * 32
    check = DraftCheck(command="pytest", passed=True, output="1 passed")
    draft = worker.signed(worker.WorkerDraft(
        draft_id=draft_id, diff=diff,
        baseline=DraftCheck(command="pytest", passed=False, output="1 failed"),
        verification=check, verified=True,
        project="project", path="tests/test_example.py", commit=head,
        name="test_example", issue_type="failure", stage="test",
        preimage=sha256(original.encode()).hexdigest(),
        postimage=sha256(replacement.encode()).hexdigest(),
    ))
    client = TestClient(worker.app)
    headers = {"Authorization": "Bearer " + worker.TOKEN}
    endpoint = "/draft/" + draft_id + "/apply"
    approval = {
        "project": "project", "base": base, "approve": True, "draft": draft.model_dump(mode="json"),
    }
    assert client.post(endpoint, json=approval).status_code == 401
    denied = client.post(endpoint, headers=headers, json={**approval, "approve": False})
    assert denied.status_code == 422
    other = client.post(endpoint, headers=headers, json={**approval, "project": "other"})
    assert other.status_code == 409
    tampered = {**approval, "draft": {**approval["draft"], "diff": diff + "\n"}}
    assert client.post(endpoint, headers=headers, json=tampered).status_code == 409
    unrelated_edit = original + "# existing user edit\n"
    source.write_text(unrelated_edit)

    def check_applied_snapshot(worktree, command):
        assert worktree != root
        assert (worktree / draft.path).read_text() == replacement
        assert "tests/test_example.py::test_example" in command
        return check

    monkeypatch.setattr(worker, "check", lambda *_: draft.baseline)
    assert client.post(endpoint, headers=headers, json=approval).status_code == 422
    assert worker.git(root, "branch", "--list", "fix/*").stdout == ""

    monkeypatch.setattr(worker, "check", check_applied_snapshot)
    prepared = client.post(endpoint, headers=headers, json=approval)
    assert prepared.status_code == 200, prepared.text
    fix = prepared.json()
    assert fix["branch"] == f"fix/test-example-{draft_id[:12]}"
    assert fix["base_commit"] == head and fix["verification"]["passed"]
    assert worker.git(root, "show", f"{fix['commit']}:{draft.path}").stdout == replacement
    assert worker.git(root, "rev-parse", "HEAD").stdout.strip() == head
    assert source.read_text() == unrelated_edit
    assert client.post(endpoint, headers=headers, json=approval).json() == fix
    assert worker.git(root, "ls-remote", str(remote), f"refs/heads/{fix['branch']}").stdout == ""

    push = {"repository": "owner/repo", "approve": True, "draft": approval["draft"], "fix": fix}
    monkeypatch.delenv("MGMCP_CI_GITHUB_PUBLISH_TOKEN", raising=False)
    assert client.post("/push", json=push).status_code == 401
    assert client.post("/push", headers=headers, json=push).status_code == 503

    monkeypatch.setenv("MGMCP_CI_GITHUB_PUBLISH_TOKEN", "publish-test-token")
    monkeypatch.setattr(worker, "REMOTE", "https://github.com/owner/repo.git")
    real_git = worker.git

    def simulate_remote(cwd, *args, **kwargs):
        if args[0] in {"ls-remote", "push"}:
            assert kwargs["github_token"] == "publish-test-token"
            local_args = tuple(str(remote) if arg.startswith("https://") else arg for arg in args)
            return real_git(cwd, *local_args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(worker, "git", simulate_remote)
    ref = f"refs/heads/{fix['branch']}"
    assert real_git(root, "push", str(remote), f"{head}:{ref}").returncode == 0
    assert client.post("/delete-branch", headers=headers, json=push).status_code == 409
    assert real_git(root, "push", "--force", str(remote), f"{fix['commit']}:{ref}").returncode == 0
    assert client.post("/delete-branch", headers=headers, json=push).json() == {"deleted": True}
    assert client.post("/delete-branch", headers=headers, json=push).json() == {"deleted": True}
    assert real_git(root, "rev-parse", ref).stdout.strip() == fix["commit"]
