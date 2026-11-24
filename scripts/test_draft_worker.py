"""Trusted local bridge for sandboxed CI draft checks and approved Git patches."""

import base64
import hmac
import os
import re
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.domain.test_drafts import (
    DraftCheck,
    DraftEdit,
    DraftID,
    FixBranch,
    PostVerification,
    WorkerDraft,
)
from scripts.isolated_checks import run_isolated_check

ROOT = Path(os.environ.get("MGMCP_DRAFT_WORKTREE", ".")).resolve()
TOKEN = os.environ["MGMCP_DRAFT_WORKER_TOKEN"]
REMOTE = os.environ["MGMCP_DRAFT_REMOTE"]
IMAGE = os.environ.get("MGMCP_DRAFT_CHECK_IMAGE", "memorygraphmcp-draft-check:local")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_LOCK = threading.Lock()


class Issue(BaseModel):
    project: str
    commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    path: str
    issue_type: Literal["failure", "warning"]
    name: str
    stage: Literal["build", "test"]


class DraftRequest(Issue):
    edit: DraftEdit


class ApplyRequest(BaseModel):
    project: str
    approve: Literal[True]
    draft: WorkerDraft


def authenticate(authorization: str | None) -> None:
    if not authorization or not hmac.compare_digest(
        authorization.encode(), f"Bearer {TOKEN}".encode()
    ):
        raise HTTPException(401, "Invalid local worker credential")


def git(
    cwd: Path, *args: str, data: str | None = None, github_token: str | None = None,
) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if github_token:
        encoded = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraHeader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {encoded}",
            GIT_CONFIG_KEY_1="http.followRedirects",
            GIT_CONFIG_VALUE_1="false",
        )
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", *args],
        cwd=cwd,
        env=env,
        input=data,
        text=True,
        capture_output=True,
        timeout=35,
    )


def target_path(root: Path, value: str) -> Path:
    if not (
        value == "pyproject.toml" or re.fullmatch(r"tests/(?:[\w.-]+/)*[\w.-]+\.py", value)
    ) or any(part in {".", ".."} for part in value.split("/")):
        raise HTTPException(422, "Drafts are limited to Python tests and pyproject.toml.")
    target = root / value
    if not target.is_file() or target.resolve() != target:
        raise HTTPException(409, "The reviewed path is unavailable or is a symbolic link.")
    return target


def command_for(draft: Issue | WorkerDraft) -> list[str]:
    if draft.stage == "build":
        return [
            "python",
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            "/tmp/wheels",
            "/src",
        ]
    if not draft.path.startswith("tests/"):
        # Warnings from a test dependency may only identify its configuration file.
        return ["python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache", "tests"]
    selection = draft.path
    if draft.issue_type == "failure":
        prefix = draft.path.removesuffix(".py").replace("/", ".") + "."
        name = draft.name.removeprefix(prefix)
        names = name.split("[", 1)[0].split(".")
        if not all(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", part) for part in names):
            raise HTTPException(422, "The CI report has no supported Python test selector.")
        selection += "::" + "::".join(names)
    return ["python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache", selection]


def check(worktree: Path, command: list[str]) -> DraftCheck:
    return DraftCheck.model_validate(run_isolated_check(worktree, IMAGE, command))


def warning_found(issue: Issue | WorkerDraft, output: str) -> bool:
    if issue.name == "Build warning":
        return re.search(r"(?im)^\s*(?:warning:|[a-z]+warning:)", output) is not None
    return issue.name + ":" in output


def signed(draft: WorkerDraft) -> WorkerDraft:
    data = draft.model_dump_json(exclude={"signature"}).encode()
    return draft.model_copy(
        update={"signature": hmac.new(TOKEN.encode(), data, sha256).hexdigest()}
    )


def validate_signature(draft: WorkerDraft) -> None:
    if not hmac.compare_digest(draft.signature, signed(draft).signature):
        raise HTTPException(409, "The draft verification signature is invalid.")


@contextmanager
def isolated_worktree(commit: str, source: str | None = None):
    with tempfile.TemporaryDirectory(prefix="mgmcp-draft-", dir="/tmp") as scratch:
        root = Path(scratch).resolve()
        root.chmod(0o755)
        worktree = root / "worktree"
        for args in (
            ("init", "--bare"),
            ("fetch", "--no-tags", "--depth=1", source or REMOTE, commit),
            ("worktree", "add", "--detach", str(worktree), commit),
        ):
            if git(root, *args).returncode:
                raise HTTPException(502, "Could not create a worktree at the tested commit.")
        yield worktree


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def verified_result(issue: Issue | WorkerDraft, before: DraftCheck, after: DraftCheck) -> bool:
    if issue.issue_type == "warning":
        return (
            before.passed
            and warning_found(issue, before.output)
            and after.passed
            and not warning_found(issue, after.output)
        )
    return bool(
        not before.passed
        and re.search(r"\b[1-9]\d* failed\b", before.output)
        and after.passed
        and re.search(r"\b[1-9]\d* passed\b", after.output)
    )


@contextmanager
def exclusive_work():
    if not _LOCK.acquire(blocking=False):
        raise HTTPException(429, "The local check worker is busy.")
    try:
        yield
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "A local Git or sandbox operation timed out.") from exc
    finally:
        _LOCK.release()


@app.post("/draft", response_model=WorkerDraft)
def create_draft(request: DraftRequest, authorization: str | None = Header(None)) -> WorkerDraft:
    authenticate(authorization)
    with exclusive_work(), isolated_worktree(request.commit) as worktree:
        path = target_path(worktree, request.path)
        original = path.read_text(encoding="utf-8")
        preimage = digest(path)
        if original.count(request.edit.original) != 1 or (
            request.edit.original == request.edit.replacement
        ):
            raise HTTPException(422, "The model edit did not match the tested source uniquely.")
        command = command_for(request)
        baseline = check(worktree, command)
        path.write_text(
            original.replace(request.edit.original, request.edit.replacement, 1),
            encoding="utf-8",
        )
        diff = git(worktree, "diff", "--no-ext-diff", "--no-textconv", "--", request.path)
        if diff.returncode or not diff.stdout or len(diff.stdout) > 30_000:
            raise HTTPException(422, "The draft did not produce a bounded source diff.")
        verification = check(worktree, command)
        return signed(
            WorkerDraft(
                **request.model_dump(exclude={"edit"}),
                draft_id=uuid4().hex,
                diff=diff.stdout,
                baseline=baseline,
                verification=verification,
                verified=verified_result(request, baseline, verification),
                preimage=preimage,
                postimage=digest(path),
            )
        )


@app.post("/verify", response_model=WorkerDraft)
def verify_draft(draft: WorkerDraft, authorization: str | None = Header(None)) -> WorkerDraft:
    authenticate(authorization)
    validate_signature(draft)
    with exclusive_work(), isolated_worktree(draft.commit) as worktree:
        path = target_path(worktree, draft.path)
        if digest(path) != draft.preimage:
            raise HTTPException(409, "The tested source no longer matches the saved draft.")
        command = command_for(draft)
        baseline = check(worktree, command)
        if (
            git(worktree, "apply", "--", data=draft.diff).returncode
            or digest(path) != draft.postimage
        ):
            raise HTTPException(409, "The saved patch no longer applies to the tested commit.")
        verification = check(worktree, command)
        return signed(
            draft.model_copy(
                update={
                    "baseline": baseline,
                    "verification": verification,
                    "verified": verified_result(draft, baseline, verification),
                }
            )
        )


class PrepareRequest(ApplyRequest):
    base: str


class PushRequest(BaseModel):
    approve: Literal[True]
    repository: Annotated[str, Field(pattern=r"^[\w.-]+/[\w.-]+$")]
    draft: WorkerDraft
    fix: FixBranch


def git_result(root: Path, *args: str) -> str:
    result = git(root, *args)
    if result.returncode:
        raise HTTPException(409, "Could not read or update the approved fix branch.")
    return result.stdout.strip()


def fix_branch_name(draft: WorkerDraft) -> str:
    category = "warning" if draft.issue_type == "warning" else "test"
    name = draft.name.split("[", 1)[0].rsplit(".", 1)[-1].removeprefix("test_")
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].rstrip("-")
    return f"fix/{category}-{slug or 'issue'}-{draft.draft_id[:12]}"


@app.post("/draft/{draft_id}/apply", response_model=FixBranch)
def apply_draft(
    draft_id: DraftID, request: PrepareRequest, authorization: str | None = Header(None),
) -> FixBranch:
    authenticate(authorization)
    draft = request.draft
    validate_signature(draft)
    if draft.draft_id != draft_id or draft.project != request.project or not draft.verified:
        raise HTTPException(409, "The approved draft does not match a verified project patch.")
    branch = fix_branch_name(draft)
    base_ref = "refs/heads/" + request.base
    with exclusive_work():
        legacy = "fix/ci-" + draft_id
        if git(ROOT, "rev-parse", "--verify", "refs/heads/" + legacy).returncode == 0:
            branch = legacy
        ref = "refs/heads/" + branch
        existing = git(ROOT, "rev-parse", "--verify", ref)
        if existing.returncode == 0:
            commit = existing.stdout.strip()
            base = git_result(ROOT, "rev-parse", commit + "^")
            source = str(ROOT)
        else:
            git_result(ROOT, "check-ref-format", base_ref)
            listing = git_result(ROOT, "ls-remote", "--exit-code", REMOTE, base_ref)
            base = listing.split()[0]
            commit = None
            source = None
        with isolated_worktree(base, source) as worktree:
            path = target_path(worktree, draft.path)
            if digest(path) != draft.preimage or git(
                worktree, "apply", "--", data=draft.diff,
            ).returncode or digest(path) != draft.postimage:
                raise HTTPException(409, "The approved patch no longer matches the target branch.")
            git_result(worktree, "add", "--", draft.path)
            tree = git_result(worktree, "write-tree")
            if commit and git_result(ROOT, "rev-parse", commit + "^{tree}") != tree:
                raise HTTPException(409, "The existing fix branch contains a different patch.")
            result = check(worktree, command_for(draft))
            if not verified_result(draft, draft.baseline, result):
                raise HTTPException(
                    422, "The patch did not pass the check against the target branch.",
                )
            if git_result(worktree, "write-tree") != tree:
                raise HTTPException(409, "The staged patch changed during verification.")
            if not commit:
                commit = git_result(
                    worktree, "-c", "user.name=MemoryGraphMCP",
                    "-c", "user.email=memorygraphmcp@localhost",
                    "commit-tree", tree, "-p", base, "-m", f"Fix CI issue {draft_id}",
                )
                git_result(
                    ROOT, "fetch", "--no-tags", "--no-write-fetch-head", str(worktree), commit,
                )
                git_result(ROOT, "update-ref", ref, commit, "0" * 40)
        return FixBranch(
            branch=branch, base_commit=base, commit=commit,
            verification=PostVerification(commit=commit, check=result, passed=True),
        )


def publishing_remote(request: PushRequest, authorization: str | None) -> tuple[str, str]:
    authenticate(authorization)
    validate_signature(request.draft)
    if request.fix.branch not in {
        fix_branch_name(request.draft), "fix/ci-" + request.draft.draft_id,
    }:
        raise HTTPException(409, "The branch does not match the approved draft.")
    token = os.environ.get("MGMCP_CI_GITHUB_PUBLISH_TOKEN")
    if not token:
        raise HTTPException(503, "GitHub write credentials are unavailable on the local worker.")
    remote = "https://github.com/" + request.repository + ".git"
    if REMOTE.removesuffix(".git").casefold() != remote.removesuffix(".git").casefold():
        raise HTTPException(409, "The publish repository does not match the trusted remote.")
    return remote, token


@app.post("/push")
def push_fix(request: PushRequest, authorization: str | None = Header(None)) -> dict:
    remote, token = publishing_remote(request, authorization)
    fix = request.fix
    ref = "refs/heads/" + fix.branch
    with exclusive_work(), tempfile.TemporaryDirectory(prefix="mgmcp-push-") as scratch:
        if git_result(
            ROOT, "rev-parse", "--verify", ref,
        ) != fix.commit:
            raise HTTPException(409, "The local branch changed after approval.")
        clean = Path(scratch)
        git_result(clean, "init", "--bare")
        git_result(clean, "fetch", "--no-tags", str(ROOT), fix.commit)
        push = git(
            clean, "push", f"--force-with-lease={ref}:",
            remote, f"{fix.commit}:{ref}", github_token=token,
        )
        if push.returncode:
            listing = git(clean, "ls-remote", remote, ref, github_token=token)
            if listing.returncode or listing.stdout.split()[:1] != [fix.commit]:
                raise HTTPException(502, "Could not publish the approved fix branch.")
    return {"pushed": True}


@app.post("/delete-branch")
def delete_fix_branch(request: PushRequest, authorization: str | None = Header(None)) -> dict:
    remote, token = publishing_remote(request, authorization)
    fix = request.fix
    ref = "refs/heads/" + fix.branch
    with exclusive_work(), tempfile.TemporaryDirectory(prefix="mgmcp-delete-") as scratch:
        clean = Path(scratch)
        git_result(clean, "init", "--bare")
        listing = git(clean, "ls-remote", remote, ref, github_token=token)
        if listing.returncode:
            raise HTTPException(502, "Could not read the remote fix branch.")
        head = listing.stdout.split()[:1]
        if not head:
            return {"deleted": True}
        if head != [fix.commit]:
            raise HTTPException(409, "The remote branch changed after approval.")
        deletion = git(
            clean, "push", f"--force-with-lease={ref}:{fix.commit}",
            remote, f":{ref}", github_token=token,
        )
        if deletion.returncode:
            listing = git(clean, "ls-remote", remote, ref, github_token=token)
            if listing.returncode or listing.stdout.strip():
                raise HTTPException(409, "GitHub declined branch deletion or the branch changed.")
    return {"deleted": True}


@app.post("/post-verify", response_model=PostVerification)
def post_verify_draft(
    draft: WorkerDraft, authorization: str | None = Header(None),
) -> PostVerification:
    authenticate(authorization)
    validate_signature(draft)
    if not draft.verified:
        raise HTTPException(409, "The draft must pass pre-verification.")
    with exclusive_work():
        applied = target_path(ROOT, draft.path).read_bytes()
        if sha256(applied).hexdigest() != draft.postimage:
            raise HTTPException(409, "The applied file no longer matches the reviewed patch.")
        head = git(ROOT, "rev-parse", "HEAD")
        if head.returncode:
            raise HTTPException(409, "The local checkout has no readable HEAD commit.")
        commit = head.stdout.strip()
        with isolated_worktree(commit, str(ROOT)) as worktree:
            target_path(worktree, draft.path).write_bytes(applied)
            result = check(worktree, command_for(draft))
        return PostVerification(
            commit=commit,
            check=result,
            passed=verified_result(draft, draft.baseline, result),
        )
