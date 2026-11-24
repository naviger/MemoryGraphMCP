"""Authenticated local Codex adapter for one operator-approved project."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import threading
from contextlib import closing, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__:
    from .isolated_checks import run_isolated_check
else:
    from isolated_checks import run_isolated_check

MODES = {
    "PLAN_ONLY": ("plan",),
    "IMPLEMENT": ("execute",),
    "REVIEW": ("review",),
    "FULL_LIFECYCLE": ("plan", "execute", "review"),
}
TEST_PATH = re.compile(r"tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_-]+\.py")

APPROVED_MODE = "APPLY_APPROVED"
APPROVED_PHASES = ("apply", "review_commit")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "tests", "approved"],
    "properties": {
        "summary": {"type": "string"},
        "tests": {"type": "array", "items": {"type": "string"}},
        "approved": {"type": "boolean"},
    },
}


PHASE_TIMEOUT_SECONDS = 510


class BridgeError(Exception):
    def __init__(self, code, message, usage=None):
        super().__init__(message)
        self.code = code
        self.usage = usage


def reported_usage(events, rates=None):
    counters = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    found = False
    cache_known = True
    for line in (events or "").splitlines():
        try:
            event = json.loads(line)
            if event.get("type") not in {"turn.completed", "turn.failed"}:
                continue
            usage = event["usage"]
            incoming, outgoing = usage["input_tokens"], usage["output_tokens"]
            cached = usage.get("cached_input_tokens")
            if (
                type(incoming) is not int
                or type(outgoing) is not int
                or min(incoming, outgoing) < 0
            ):
                continue
            if cached is not None and (type(cached) is not int or not 0 <= cached <= incoming):
                continue
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        found = True
        cache_known = cache_known and cached is not None
        counters["input_tokens"] += incoming
        counters["output_tokens"] += outgoing
        counters["cached_input_tokens"] += cached or 0
    if not found:
        return None
    cost = None
    if rates and cache_known:
        cached = counters["cached_input_tokens"]
        cost = (
            (counters["input_tokens"] - cached) * rates["input"]
            + cached * rates["cached_input"]
            + counters["output_tokens"] * rates["output"]
        ) / 1_000_000
    return {
        **counters,
        "cached_input_tokens": counters["cached_input_tokens"] if cache_known else None,
        "estimated_cost_usd": cost,
    }


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


class CodexBridge:
    def __init__(
        self, state: Path, repo: Path, project: str, executable: str, *, rates=None, model=None
    ):
        self.state = state.resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.repo = repo.resolve()
        self.project = project
        self.executable = executable
        self.rates = rates
        self.model = model
        self.lock = threading.Lock()
        self.running = {}
        with closing(self.database()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS phase_results (
                    id TEXT NOT NULL, phase TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(id, phase)
                )
            """)
            db.execute("CREATE TABLE IF NOT EXISTS cancellations (id TEXT PRIMARY KEY)")

    def database(self):
        return sqlite3.connect(self.state / "operations.sqlite")

    @staticmethod
    def cancelled(db, identity):
        return bool(
            db.execute(
                "SELECT 1 FROM cancellations WHERE id=?",
                (identity,),
            ).fetchone()
        )

    def cancel(self, identity):
        with self.lock, closing(self.database()) as db, db:
            db.execute("INSERT OR IGNORE INTO cancellations VALUES (?)", (identity,))
            if (process := self.running.get(identity)) and process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
        return {"cancelled": True}

    def run(self, identity, phase, prompt):
        context = json.loads(prompt)
        mode = context.get("mode")
        phases = APPROVED_PHASES if mode == APPROVED_MODE else MODES.get(mode, ())
        if context.get("project_key") != self.project or phase not in phases:
            raise BridgeError(403, "Project or execution mode is unavailable")
        with self.lock, closing(self.database()) as db:
            if self.cancelled(db, identity):
                raise BridgeError(409, "Execution was cancelled")
            row = db.execute(
                "SELECT result FROM phase_results WHERE id=? AND phase=?",
                (identity, phase),
            ).fetchone()
            if row:
                return json.loads(row[0])
            if self.running:
                raise BridgeError(503, "The local bridge already has an active phase")
            for previous in phases[: phases.index(phase)]:
                if not db.execute(
                    "SELECT 1 FROM phase_results WHERE id=? AND phase=?",
                    (identity, previous),
                ).fetchone():
                    raise BridgeError(409, "An earlier phase must finish first")
            self.running[identity] = None
        try:
            result = self.run_phase(identity, phase, mode, prompt)
            with self.lock, closing(self.database()) as db, db:
                if self.cancelled(db, identity):
                    raise BridgeError(409, "Execution was cancelled")
                db.execute(
                    "INSERT INTO phase_results VALUES (?,?,?)",
                    (identity, phase, json.dumps(result)),
                )
            return result
        finally:
            with self.lock:
                self.running.pop(identity, None)

    def prepare_repo(self, directory, source=None):
        checkout = directory / "repo"
        if not checkout.exists():
            # A separate local clone keeps writable Git metadata away from the source checkout.
            git(
                source or self.repo,
                "clone",
                "--no-hardlinks",
                "--no-tags",
                "--single-branch",
                "--",
                ".",
                str(checkout),
            )
            git(checkout, "remote", "remove", "origin")
            (directory / "base").write_bytes(git(checkout, "rev-parse", "HEAD"))
        return checkout

    def verify_contract(self, checkout, contract):
        paths = contract.get("test_paths")
        if (
            not isinstance(paths, list)
            or len(paths) > 8
            or any(not isinstance(path, str) or not TEST_PATH.fullmatch(path) for path in paths)
        ):
            raise BridgeError(400, "Invalid regression test selection")
        if any((checkout / path).resolve() != checkout / path for path in paths):
            raise BridgeError(400, "Regression test paths cannot use symbolic links")
        return self.pytest_check(checkout, paths)

    def pytest_check(self, checkout, paths):
        return run_isolated_check(
            checkout,
            os.environ.get("MGMCP_DRAFT_CHECK_IMAGE", "memorygraphmcp-draft-check:local"),
            [
                "python",
                "-m",
                "pytest",
                "-q",
                "--color=no",
                "-o",
                "cache_dir=/tmp/pytest-cache",
                *paths,
            ],
        )

    def prove_repair(self, checkout, directory, captured, blocked_tree):
        paths = (
            git(
                checkout,
                "diff",
                "--cached",
                "--no-renames",
                "--diff-filter=AM",
                "--name-only",
                "-z",
                blocked_tree,
                "--",
                "tests/",
            )
            .decode(errors="replace")
            .split("\0")
        )
        tests = [path for path in paths if TEST_PATH.fullmatch(path)]
        if not tests or len(tests) > 8:
            return {"passed": False, "summary": "Add or update one to eight regression test files."}
        if any((checkout / path).resolve() != checkout / path for path in tests):
            return {"passed": False, "summary": "Regression tests must be regular files."}
        overlay = directory / "regression-tests.patch"
        overlay.write_bytes(
            git(
                checkout, "diff", "--cached", "--no-renames", "--binary", blocked_tree, "--", *tests
            )
        )
        with TemporaryDirectory(prefix="baseline-", dir=directory) as temp:
            baseline = self.prepare_repo(Path(temp), checkout)
            git(baseline, "apply", "--index", "--", str(captured))
            git(baseline, "apply", "--index", "--", str(overlay))
            before = self.pytest_check(baseline, tests)
        proof = {
            "passed": False,
            "summary": "The blocked patch did not reproduce the defect in a test.",
            "baseline": before,
        }
        if before.get("exit_code") != 1:
            if not before["passed"]:
                proof["summary"] = "The baseline check could not establish a test failure."
            return proof
        failing = list(
            dict.fromkeys(
                match.group(1)
                for match in re.finditer(
                    r"^FAILED (tests/[^\n]+?::[^\n]+?)(?: - .*|$)", before["output"], re.MULTILINE
                )
                if match.group(1).split("::", 1)[0] in tests
            )
        )
        if not failing or len(failing) > 50:
            return proof
        after = self.pytest_check(checkout, failing)
        return {
            **proof,
            "passed": after["passed"],
            "revision": after,
            "reproduced_tests": failing,
            "summary": (
                "The same regression tests failed on the blocked patch and passed on the revision."
                if after["passed"]
                else "The regression tests still fail on the revision."
            ),
        }

    def implementation_contract(self, checkout, context, files):
        tests = [name for name in files if TEST_PATH.fullmatch(name)]
        if not tests and not any((checkout / "tests").rglob("test_*.py")):
            return None
        criteria = context.get("task", {}).get("success_criteria") or [
            "Satisfy the task description and preserve existing behavior outside the change."
        ]
        return {
            "test_paths": tests[:8],
            "acceptance_criteria": [
                value[:1000] for value in criteria[:8] if isinstance(value, str) and value.strip()
            ]
            or ["Satisfy the task description."],
        }

    def apply_approved(self, identity, directory, context):
        target = context.get("approved_patch") or {}
        source_key = target.get("execution_id", "")
        base = target.get("base_commit", "")
        digest = target.get("patch_sha256", "")
        review_key = context.get("approved_review_key", "")
        if (
            source_key != context.get("patch_execution_key")
            or not re.fullmatch(r"[a-f0-9]{64}", source_key)
            or not re.fullmatch(r"[a-f0-9]{64}", review_key)
            or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", base)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise BridgeError(400, "The approved patch identity is incomplete")
        original = self.state / source_key
        with closing(self.database()) as db:
            row = db.execute(
                "SELECT result FROM phase_results WHERE id=? AND phase='execute'", (source_key,)
            ).fetchone()
            reviewed = db.execute(
                "SELECT result FROM phase_results WHERE id=? AND phase='review'", (review_key,)
            ).fetchone()
        review = json.loads(reviewed[0]) if reviewed else {}
        if (
            review.get("review_verdict") != "passed"
            or review.get("reviewed_target") != target
            or not (review.get("verification") or {}).get("passed")
        ):
            raise BridgeError(409, "The bridge has no matching passing patch review")
        patch = original / "implementation.patch"
        if not row or not patch.is_file() or (original / "base").read_text().strip() != base:
            raise BridgeError(409, "The approved implementation artifact is unavailable")
        contents = patch.read_bytes()
        if hashlib.sha256(contents).hexdigest() != digest or not any(
            artifact.get("name") == "Local implementation patch"
            and artifact.get("sha256") == digest
            for artifact in json.loads(row[0]).get("artifacts", [])
        ):
            raise BridgeError(409, "The saved implementation differs from the approved patch")
        captured = directory / "approved.patch"
        captured.write_bytes(contents)
        checkout = self.prepare_repo(directory, original / "repo")
        branch = f"codex/task-fix-{identity[:12]}"
        if git(checkout, "branch", "--list", branch).strip():
            git(checkout, "checkout", branch)
        else:
            git(checkout, "checkout", "-b", branch, base)
        head = git(checkout, "rev-parse", "HEAD").decode().strip()
        if head == base:
            git(checkout, "reset", "--hard", base)
            git(checkout, "clean", "-fdx")
            git(checkout, "apply", "--index", "--", str(captured))
            git(checkout, "diff", "--cached", "--check", base, "--")
            git(
                checkout,
                "-c",
                "user.name=MemoryGraphMCP",
                "-c",
                "user.email=memorygraphmcp@localhost",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-m",
                "Apply approved task patch",
            )
        commit = git(checkout, "rev-parse", "HEAD").decode().strip()
        if (
            git(checkout, "rev-parse", "HEAD^").decode().strip() != base
            or hashlib.sha256(git(checkout, "diff", "--binary", base, commit, "--")).hexdigest()
            != digest
            or git(checkout, "status", "--porcelain")
        ):
            raise BridgeError(409, "The fix branch does not match the approved patch")
        verified = self.verify_contract(checkout, context["repair_contract"])
        result = {
            "summary": (
                "The approved patch was committed locally and the required tests passed."
                if verified["passed"]
                else "The required tests failed on the local fix commit. Commit review was skipped."
            ),
            "applied_commit": {
                "branch": branch,
                "commit": commit,
                "base_commit": base,
                "patch_sha256": digest,
            },
            "verification": verified,
            "artifacts": [
                {"name": "Local fix commit", "reference": f"{checkout} ({branch} at {commit})"}
            ],
        }
        if not verified["passed"]:
            result["review_verdict"] = "blocking"
        return result

    def applied_review_target(self, identity, checkout):
        with closing(self.database()) as db:
            row = db.execute(
                "SELECT result FROM phase_results WHERE id=? AND phase='apply'", (identity,)
            ).fetchone()
        applied = json.loads(row[0]) if row else {}
        expected = applied.get("applied_commit") or {}
        head = git(checkout, "rev-parse", "HEAD").decode().strip()
        if (
            not (applied.get("verification") or {}).get("passed")
            or head != expected.get("commit")
            or git(checkout, "status", "--porcelain")
        ):
            raise BridgeError(409, "The exact fix commit must pass its required tests first")
        return {"kind": "fix branch commit", "commit": head}

    def run_phase(self, identity, phase, mode, prompt):
        directory = self.state / identity
        directory.mkdir(exist_ok=True, mode=0o700)
        context = json.loads(prompt)
        contract = context.get("repair_contract")
        if phase == "apply":
            return self.apply_approved(identity, directory, context)
        repair = phase == "execute" and bool(context.get("repair_patch_sha256"))
        target = context.get("patch_execution_key") if phase == "review" or repair else None
        if repair and (not target or mode != "IMPLEMENT" or not context.get("repair_prompt")):
            raise BridgeError(
                400, "Repair requires the original patch and an editable repair prompt"
            )
        reviewed_target = None
        if target:
            if not re.fullmatch(r"[a-f0-9]{64}", target) or (mode != "REVIEW" and not repair):
                raise BridgeError(400, "Invalid patch execution reference")
            original = self.state / target
            patch = original / "implementation.patch"
            source = original / "repo"
            with closing(self.database()) as db:
                recorded = db.execute(
                    "SELECT result FROM phase_results WHERE id=? AND phase='execute'",
                    (target,),
                ).fetchone()
            if not recorded or not patch.is_file() or not source.is_dir():
                raise BridgeError(409, "The requested implementation patch is unavailable")
            checkout = self.prepare_repo(directory, source)
            base = (original / "base").read_text().strip()
            git(checkout, "reset", "--hard", base)
            git(checkout, "clean", "-fdx")
            captured = directory / ("repair-input.patch" if repair else "reviewed.patch")
            contents = patch.read_bytes()
            digest = hashlib.sha256(contents).hexdigest()
            if repair and digest != context["repair_patch_sha256"]:
                raise BridgeError(409, "The repair input differs from the reviewed patch")
            for artifact in json.loads(recorded[0]).get("artifacts", []):
                if artifact.get("name") == "Local implementation patch" and artifact.get("sha256"):
                    if artifact["sha256"] != digest:
                        raise BridgeError(
                            409, "The implementation patch changed after it was saved"
                        )
            captured.write_bytes(contents)
            (directory / "base").write_text(base + "\n")
            if not repair:
                git(checkout, "apply", "--index", str(captured))
            reviewed_target = {
                "kind": "implementation patch",
                "execution_id": target,
                "base_commit": base,
                "patch_sha256": digest,
            }
        else:
            checkout = self.prepare_repo(directory)
            base = (directory / "base").read_text().strip()
            if phase == "review_commit":
                reviewed_target = self.applied_review_target(identity, checkout)
            if phase == "review":
                reviewed_target = {"kind": "checkout commit", "commit": base}
                if mode == "FULL_LIFECYCLE":
                    patch = directory / "implementation.patch"
                    if not patch.is_file():
                        raise BridgeError(409, "The implementation phase did not save a patch")
                    git(checkout, "reset", "--hard", base)
                    git(checkout, "clean", "-fdx")
                    git(checkout, "apply", "--index", str(patch))
                    reviewed_target = {
                        "kind": "implementation patch",
                        "execution_id": identity,
                        "base_commit": base,
                        "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
                    }
        writable = phase == "execute"
        checkpoint = directory / "partial-timeout.patch"
        if writable:
            # Only this private clone is cleaned when replaying an interrupted implementation.
            git(checkout, "reset", "--hard", base)
            git(checkout, "clean", "-fdx")
            if repair:
                git(checkout, "apply", "--index", str(captured))
                blocked_tree = git(checkout, "write-tree").decode().strip()
            if checkpoint.exists() and checkpoint.stat().st_size:
                git(checkout, "reset", "--hard", base)
                git(checkout, "apply", "--index", str(checkpoint))
        verified = (
            self.verify_contract(checkout, contract) if contract and phase == "review" else None
        )
        if verified and not verified["passed"]:
            return {
                "summary": "A required regression check failed. Model review was skipped.",
                "review_verdict": "blocking",
                "reviewed_target": reviewed_target,
                "verification": verified,
            }
        schema = directory / "response.schema.json"
        schema.write_text(json.dumps(SCHEMA))
        output = directory / f"{phase}.json"
        output.unlink(missing_ok=True)
        sandbox = "workspace-write" if writable else "read-only"
        instructions = {
            "plan": (
                "Produce a concise plan with observable acceptance cases, edge cases, and "
                "the regression tests to run. Leave the repository unchanged."
            ),
            "execute": (
                "Implement the task and follow the plan when provided. Add regression tests "
                "for the acceptance cases and run the relevant checks before returning."
            ),
            "review_commit": "Review the committed fix against its parent and the task criteria.",
            "review": (
                "Review the proposed changes against the task and report findings. "
                "Set approved=true only if the proposed implementation passes review. "
                "Leave the repository unchanged."
            ),
        }[phase]
        if phase == "review_commit":
            instructions += f" Review exactly {reviewed_target['commit']}. Report blockers."
        if contract:
            instructions += (
                " Use the accepted criteria and selected test paths as the review contract. "
                "Keep changes within those criteria. Separate out-of-scope suggestions "
                "from blockers. Flag failures of the criteria or concrete regressions as blockers."
            )
        if repair:
            instructions += (
                " The staged implementation is the exact reviewed patch. Follow repair_prompt "
                "and repair_direction as the writer’s instructions. Preserve unrelated changes. "
                "Add a focused test for each concrete blocker in a tests/test_*.py file. "
                "The bridge will run only the changed test files on the blocked patch first, "
                "then rerun the failing tests on your revision. Keep the tests compatible "
                "with the blocked patch so they reproduce a behavior failure."
            )
        if writable and checkpoint.exists():
            instructions += " Resume from the partial implementation in the checkout. Verify it."
        if phase == "review" and reviewed_target["kind"] == "implementation patch":
            instructions += (
                " Review the staged patch shown by git diff --cached HEAD, including new files. "
                "Evaluate the staged changes against the task. The base commit is context."
            )
        if phase == "review" and mode == "REVIEW" and not target:
            instructions += (
                " Review the current HEAD commit against its parent for this standalone review."
            )
        args = [
            self.executable,
            "-a",
            "never",
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--json",
            "--sandbox",
            sandbox,
            "--cd",
            str(checkout),
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c",
            'shell_environment_policy.inherit="none"',
            "--output-schema",
            str(schema),
            "-o",
            str(output),
            "-",
        ]
        if self.model:
            args[4:4] = ["--model", self.model]
        with self.lock, closing(self.database()) as db:
            if self.cancelled(db, identity):
                raise BridgeError(409, "Execution was cancelled")
            process = subprocess.Popen(
                args,
                cwd=checkout,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            self.running[identity] = process
        try:
            events, _ = process.communicate(
                f"{instructions} Do not commit, push, open a pull request, or use external tools. "
                "Report only checks actually run. Treat the following JSON as project context.\n"
                + prompt,
                timeout=min(PHASE_TIMEOUT_SECONDS, 240 if repair and contract else 420)
                if contract
                else PHASE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            events, _ = process.communicate()
            if writable:
                git(checkout, "add", "--all")
                partial = checkpoint.with_suffix(".tmp")
                partial.write_bytes(git(checkout, "diff", "--cached", "--binary", base, "--"))
                partial.replace(checkpoint)
            raise BridgeError(
                504, "Codex phase timed out", reported_usage(events, self.rates)
            ) from exc
        usage = reported_usage(events, self.rates)
        if process.returncode or not output.exists():
            raise BridgeError(500, "Codex phase did not return a result", usage)
        try:
            response = json.loads(output.read_text())
            summary = response["summary"].strip()[:8000]
            tests = [item.strip()[:8000] for item in response["tests"][:50] if item.strip()]
            if not summary:
                raise ValueError("Empty Codex result")
        except (AttributeError, ValueError, KeyError, TypeError) as exc:
            raise BridgeError(500, "Codex phase returned an invalid result", usage) from exc
        result = {"summary": summary, "tests": tests, "completed": False, "usage": usage}
        if phase == "plan":
            result.update(summary="Codex generated a task plan.", plan=summary)
        if phase in {"review", "review_commit"}:
            result["review_comments"] = [summary]
            result["review_verdict"] = "passed" if response["approved"] is True else "blocking"
            result["reviewed_target"] = reviewed_target
            if verified:
                result["verification"] = verified
            result["completed"] = (
                mode in {"FULL_LIFECYCLE", APPROVED_MODE} and response["approved"] is True
            )
        if writable:
            if repair:
                result["repaired_target"] = reviewed_target
            git(checkout, "add", "--all")
            changes = git(checkout, "diff", "--cached", "--binary", base, "--")
            files = git(checkout, "diff", "--cached", "--name-only", "-z", base, "--")
            if changes:
                patch = directory / "implementation.patch"
                patch.write_bytes(changes)
                result["artifacts"] = [
                    {
                        "name": "Local implementation patch",
                        "reference": str(patch),
                        "sha256": hashlib.sha256(changes).hexdigest(),
                    }
                ]
                result["files_modified"] = [
                    name.decode(errors="replace")[:8000] for name in files.split(b"\0") if name
                ][:50]
            result["completed"] = bool(changes and response["approved"] is True)
            if changes and not contract:
                contract = self.implementation_contract(checkout, context, result["files_modified"])
                if contract:
                    result["generated_contract"] = contract
            if contract:
                verified = self.verify_contract(checkout, contract)
                result["verification"] = verified
                result["completed"] = bool(result["completed"] and verified["passed"])
                proof = (
                    self.prove_repair(checkout, directory, captured, blocked_tree)
                    if repair
                    else None
                )
                if proof:
                    result["regression_proof"] = proof
                    result["completed"] = result["completed"] and proof["passed"]
                if changes and (not verified["passed"] or (proof and not proof["passed"])):
                    result["review_verdict"] = "blocking"
                    result["reviewed_target"] = {
                        "kind": "implementation patch",
                        "execution_id": identity,
                        "base_commit": base,
                        "patch_sha256": hashlib.sha256(changes).hexdigest(),
                    }
                    result["summary"] = (
                        "Required tests failed in Docker. No model review was started."
                        if not verified["passed"]
                        else proof["summary"] + " No model review was started."
                    )
        return result


def serve(bridge, token, host, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(data)

        def authorized(self):
            header = self.headers.get("Authorization", "").encode()
            if hmac.compare_digest(header, f"Bearer {token}".encode()):
                return True
            self.reply(401, {"error": "Authentication required"})
            return False

        def do_GET(self):
            if self.authorized():
                if self.path == "/capabilities":
                    self.reply(
                        200,
                        {
                            "modes": list(MODES),
                            "model": bridge.model or "Codex CLI default",
                            "apply_approved_patch": True,
                        },
                    )
                else:
                    self.reply(404, {})

        def do_POST(self):
            if not self.authorized():
                return
            phase = self.path.removeprefix("/")
            if phase not in {"plan", "execute", "review", "cancel", *APPROVED_PHASES}:
                return self.reply(404, {})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 262144:
                    raise BridgeError(413, "Request size exceeds the limit")
                request = json.loads(self.rfile.read(size))
                identity = request["execution_id"]
                if not re.fullmatch(r"[a-f0-9]{64}", identity):
                    raise ValueError("Invalid execution identity")
                if self.headers.get("Idempotency-Key") != f"{identity}:{phase}":
                    raise ValueError("Invalid idempotency key")
                result = (
                    bridge.cancel(identity)
                    if phase == "cancel"
                    else bridge.run(identity, phase, request["prompt"])
                )
                self.reply(200, result)
            except BridgeError as exc:
                self.reply(exc.code, {"error": str(exc), "usage": exc.usage})
            except (AttributeError, ValueError, KeyError, TypeError):
                self.reply(400, {"error": "Invalid bridge request"})
            except (OSError, subprocess.SubprocessError, sqlite3.Error):
                self.reply(500, {"error": "Local bridge operation failed"})

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument(
        "--api-pricing-file", type=Path, help="Explicit API rates in USD per million tokens"
    )
    args = parser.parse_args()
    rates, model = None, None
    if args.api_pricing_file:
        try:
            pricing = json.loads(args.api_pricing_file.read_text())
            model = pricing["model"].strip()
            rates = {name: float(pricing[name]) for name in ("input", "cached_input", "output")}
            if not model or any(not math.isfinite(rate) or rate < 0 for rate in rates.values()):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            parser.error("Pricing file requires model, input, cached_input, and output")

    token = args.token_file.read_text().strip()
    if len(token) < 32:
        parser.error("Use a random bearer token of at least 32 characters")
    bridge = CodexBridge(
        args.state, args.repository, args.project, args.codex, rates=rates, model=model
    )
    serve(bridge, token, "127.0.0.1", args.port)


if __name__ == "__main__":
    main()
