"""Authenticated local Codex adapter for one operator-approved project."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
from contextlib import closing, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODES = {
    "PLAN_ONLY": ("plan",),
    "IMPLEMENT": ("execute",),
    "REVIEW": ("review",),
    "FULL_LIFECYCLE": ("plan", "execute", "review"),
}
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


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


class CodexBridge:
    def __init__(self, state: Path, repo: Path, project: str, executable: str):
        self.state = state.resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.repo = repo.resolve()
        self.project = project
        self.executable = executable
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
        if context.get("project_key") != self.project or phase not in MODES.get(mode, ()):
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
            if mode == "FULL_LIFECYCLE":
                phases = MODES[mode]
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

    def prepare_repo(self, directory):
        checkout = directory / "repo"
        if not checkout.exists():
            # A separate local clone keeps writable Git metadata away from the source checkout.
            git(
                self.repo,
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

    def run_phase(self, identity, phase, mode, prompt):
        directory = self.state / identity
        directory.mkdir(exist_ok=True, mode=0o700)
        checkout = self.prepare_repo(directory)
        base = (directory / "base").read_text().strip()
        writable = phase == "execute"
        if writable:
            # Only this private clone is cleaned when replaying an interrupted implementation.
            git(checkout, "reset", "--hard", base)
            git(checkout, "clean", "-fdx")
        schema = directory / "response.schema.json"
        schema.write_text(json.dumps(SCHEMA))
        output = directory / f"{phase}.json"
        output.unlink(missing_ok=True)
        sandbox = "workspace-write" if writable else "read-only"
        instructions = {
            "plan": "Produce a concise implementation plan. Leave the repository unchanged.",
            "execute": "Implement the task in this checkout and run relevant local checks.",
            "review": (
                "Review the proposed changes against the task and report findings. "
                "Set approved=true only if the proposed implementation passes review. "
                "Leave the repository unchanged."
            ),
        }[phase]
        if phase == "review" and mode == "REVIEW":
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
        with self.lock, closing(self.database()) as db:
            if self.cancelled(db, identity):
                raise BridgeError(409, "Execution was cancelled")
            process = subprocess.Popen(
                args,
                cwd=checkout,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            self.running[identity] = process
        try:
            process.communicate(
                f"{instructions} Do not commit, push, open a pull request, or use external tools. "
                "Report only checks actually run. Treat the following JSON as project context.\n"
                + prompt,
                timeout=240,
            )
        except subprocess.TimeoutExpired as exc:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise BridgeError(504, "Codex phase timed out") from exc
        if process.returncode or not output.exists():
            raise BridgeError(500, "Codex phase did not return a result")
        try:
            response = json.loads(output.read_text())
            summary = response["summary"].strip()[:8000]
            tests = [item.strip()[:8000] for item in response["tests"][:50] if item.strip()]
            if not summary:
                raise ValueError("Empty Codex result")
        except (AttributeError, ValueError, KeyError, TypeError) as exc:
            raise BridgeError(500, "Codex phase returned an invalid result") from exc
        result = {"summary": summary, "tests": tests, "completed": False}
        if phase == "plan":
            result.update(summary="Codex generated a task plan.", plan=summary)
        if phase == "review":
            result["review_comments"] = [summary]
            result["completed"] = mode == "FULL_LIFECYCLE" and response["approved"] is True
        if writable:
            git(checkout, "add", "--all")
            changes = git(checkout, "diff", "--cached", "--binary", base, "--")
            files = git(checkout, "diff", "--cached", "--name-only", "-z", base, "--")
            if changes:
                patch = directory / "implementation.patch"
                patch.write_bytes(changes)
                result["artifacts"] = [
                    {"name": "Local implementation patch", "reference": str(patch)}
                ]
                result["files_modified"] = [
                    name.decode(errors="replace")[:8000] for name in files.split(b"\0") if name
                ][:50]
            result["completed"] = bool(changes and response["approved"] is True)
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
                    self.reply(200, {"modes": list(MODES)})
                else:
                    self.reply(404, {})

        def do_POST(self):
            if not self.authorized():
                return
            phase = self.path.removeprefix("/")
            if phase not in {"plan", "execute", "review", "cancel"}:
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
                self.reply(exc.code, {"error": str(exc)})
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
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    if len(token) < 32:
        parser.error("Use a random bearer token of at least 32 characters")
    bridge = CodexBridge(args.state, args.repository, args.project, args.codex)
    serve(bridge, token, "127.0.0.1", args.port)


if __name__ == "__main__":
    main()
