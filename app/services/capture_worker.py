"""Background adapters for local commits, GitHub activity, and graph changes."""
from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import anyio
import httpx
import structlog

from app.config.settings import Settings
from app.domain.architecture import (
    CodeRevision,
    Constraint,
    ConversationSummary,
    Repository,
    Task,
    architecture_key,
)
from app.domain.capture_checkpoint import (
    CaptureCheckpoint,
    CaptureHistoryInput,
    CaptureStatus,
    CaptureStream,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.knowledge_capture import CaptureEvent, CommitCapture, CommitEnvelope
from app.domain.test_runs import TestRun
from app.services.knowledge_capture import KnowledgeCaptureService, _fingerprint
from app.services.python_architecture import MAX_FILES, MAX_SOURCE_BYTES, PythonArchitectureIndexer

logger = structlog.get_logger(__name__)
_MAX_ENVELOPE_BYTES = 24_000_000
_UNREACHABLE_HEAD = "Previous branch head is no longer reachable. Request a backfill to establish a new baseline."


class CaptureWorker:
    def __init__(self, settings: Settings, capture: KnowledgeCaptureService):
        self.settings = settings
        self.capture = capture
        self.graph = capture.graph
        self.project = settings.capture_project_key
        self.repository = settings.capture_repository_key

    def streams(self) -> list[CaptureStream]:
        streams: list[CaptureStream] = ["graph"]
        if self.settings.capture_spool and self.repository:
            streams.append("spool")
        if self.settings.capture_github_repository and self.repository:
            streams.extend(["commits", "pulls"])
            if self.settings.capture_index_github:
                streams.append("index")
        return streams

    def checkpoint_scope(self, stream: CaptureStream) -> str:
        if stream == "graph":
            return "journal"
        if stream == "spool":
            return self.repository
        branch = self.settings.ci_github_branch if stream != "pulls" else ""
        return f"{self.repository}:{self.settings.capture_github_repository}:{branch}"

    def checkpoint(self, stream: CaptureStream) -> CaptureCheckpoint:
        return self.graph.read_capture_checkpoint(self.project, self.checkpoint_scope(stream), stream)

    def save_checkpoint(self, checkpoint: CaptureCheckpoint) -> CaptureCheckpoint:
        return self.graph.save_capture_checkpoint(
            self.project, self.checkpoint_scope(checkpoint.stream), checkpoint,
        )

    def status(self) -> CaptureStatus:
        checkpoints = []
        for stream in self.streams():
            checkpoint = self.checkpoint(stream)
            if stream == "graph":
                count, oldest = self.graph.capture_journal_lag(self.project)
                checkpoint.pending_count = count
                checkpoint.oldest_pending = oldest
            if stream == "spool":
                pending = []
                for path in Path(self.settings.capture_spool).glob("*.capture"):
                    with suppress(FileNotFoundError):
                        pending.append(path.lstat().st_mtime)
                checkpoint.pending_count = len(pending)
                checkpoint.oldest_pending = datetime.fromtimestamp(min(pending), UTC) if pending else None
            checkpoints.append(checkpoint)
        return CaptureStatus(configured=True, interval_seconds=self.settings.capture_interval, streams=checkpoints)

    def request_history(self, command: CaptureHistoryInput, actor: str) -> CaptureCheckpoint:
        current = self.checkpoint(command.stream)
        if current.history and not current.failure:
            raise ArchitectureGraphError("A history scan is already queued for this stream")
        return self.save_checkpoint(current.model_copy(update={
            "history": command.action, "history_since": command.since,
            "history_requested_by": actor, "retry_at": None,
        }))

    async def poll_stream(self, stream: CaptureStream, job) -> None:
        try:
            checkpoint = await anyio.to_thread.run_sync(self.checkpoint, stream)
            now = datetime.now(UTC)
            if checkpoint.retry_at and checkpoint.retry_at > now:
                return
            try:
                progress = await job(checkpoint)
            except Exception as exc:
                failures = checkpoint.consecutive_failures + 1
                reason = str(exc) if isinstance(exc, CaptureHistoryError) else type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    reason = f"GitHub HTTP {exc.response.status_code}"
                updates = {
                    "failure": reason, "last_failure": now, "consecutive_failures": failures,
                    "retry_at": now + timedelta(seconds=min(3600, self.settings.capture_interval * 2 ** min(failures - 1, 10))),
                }
            else:
                updates = {
                    **(progress or {}), "last_success": datetime.now(UTC), "failure": None,
                    "consecutive_failures": 0, "retry_at": None,
                    "history": None, "history_since": None,
                }
                if checkpoint.history:
                    updates.update(last_history_action=checkpoint.history, last_history_completed=updates["last_success"])
            updates.update(observed_head=checkpoint.observed_head, observed_timestamp=checkpoint.observed_timestamp)
            await anyio.to_thread.run_sync(self.save_checkpoint, checkpoint.model_copy(update=updates))
        except Exception as exc:
            logger.warning("knowledge_capture_checkpoint_failed", stream=stream, error_type=type(exc).__name__)

    async def run(self) -> None:
        while True:
            for stream in self.streams():
                if stream == "graph":
                    await self.poll_stream(stream, lambda _: anyio.to_thread.run_sync(self.read_graph))
                elif stream == "spool":
                    await self.poll_stream(stream, lambda _: anyio.to_thread.run_sync(self.read_spool))
                else:
                    await self.poll_stream(stream, self.read_github)
            await anyio.sleep(self.settings.capture_interval)

    def read_spool(self) -> None:
        if not self.settings.capture_spool or not self.repository:
            return
        for path in sorted(Path(self.settings.capture_spool).glob("*.capture"))[:100]:
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                with path.open("rb") as file:
                    payload = file.read(_MAX_ENVELOPE_BYTES + 1)
                if len(payload) > _MAX_ENVELOPE_BYTES:
                    raise ValueError("Capture envelope exceeds size limit")
                item = CommitEnvelope.model_validate_json(payload)
            except (OSError, ValueError) as exc:
                logger.warning("knowledge_capture_invalid_file", error_type=type(exc).__name__)
                path.rename(path.with_suffix(".invalid"))
                raise CaptureHistoryError("Malformed spool item moved to quarantine") from exc
            self.capture_local_commit(item)
            path.unlink(missing_ok=True)

    def capture_local_commit(self, item: CommitEnvelope) -> None:
        if item.python_snapshot is not None:
            self.index_snapshot(item.commit, item.python_snapshot, item.queued_at or item.commit.timestamp)
        self.capture.commit(self.project, self.repository, item.commit)

    def index_snapshot(
        self, commit: CommitCapture, sources: dict[str, str], order: datetime,
    ) -> None:
        roots = self.graph.project_source_repositories(self.project)
        root = next((node for node in roots if node.key == self.repository), None)
        if root is None:
            raise ArchitectureGraphError("Capture repository is outside the configured project")
        event_id = f"{root.key}:{commit.sha}"
        index_key = architecture_key("capture", self.project, "index", event_id)
        if self.graph.read_entity(index_key) is not None:
            return
        revision = CodeRevision(
            repository_key=root.key, commit=commit.sha,
            github_repository=self.settings.capture_github_repository or None,
        )
        index = PythonArchitectureIndexer().build(root.name, sources, root=root, revision=revision)
        count = self.graph.replace_index(index, capture_order=order.astimezone(UTC).isoformat(timespec="microseconds"))
        if count == 0 and index.links:
            return
        self.capture.store(CaptureEvent(
            source="index", event_id=event_id, project_key=self.project,
            timestamp=commit.timestamp, related_keys=[root.key],
            evidence=[f"git commit {commit.sha}"],
            summary=f"Indexed {len(index.entities)} architecture entities in {root.name}.",
        ), key=index_key)

    def read_graph(self) -> None:
        for row in self.graph.capture_journal(self.project, 100):
            if row["event_type"] == "test.run.finished":
                run = TestRun.model_validate(row["payload"])
                source = "ci"
                summary = (f"CI attempt {run.run_id} {run.status}: {run.passed} passed, "
                           f"{run.failed} failed, {run.skipped} skipped.")
                if run.build_status:
                    summary += f" Build {run.build_status}."
                if run.warning_count is not None:
                    summary += f" {run.warning_count} warnings."
                evidence = [f"git commit {run.commit}", f"GitHub Actions attempt {run.run_id}"]
                paths = {failure.source_path for failure in run.failures if failure.source_path}
                repository = self.graph.read_entity(run.repository_key)
                related = [*row["affected_entity_keys"], *[
                    node.key for node in self.graph.project_scope(self.project).entities
                    if node.entity_type == "TestSuite" and node.source_path in paths
                    and repository and (
                        node.repository_key == repository.key if node.repository_key
                        else node.repository == repository.name
                    )
                ]]
            else:
                source = "task"
                summary = f"Tracker applied {row['event_type']} for {row['task_key']}."
                evidence = [f"{row['source']} event {row['event_id']}"]
                related = row["affected_entity_keys"]
            self.capture.store(CaptureEvent(
                source=source, event_id=row["_key"], project_key=self.project,
                timestamp=row["recorded_at"], summary=summary, evidence=evidence,
                related_keys=[key for key in related if self.graph.read_entity(key) is not None],
            ), key=f"capture-{row['_key']}")

        scope = self.graph.project_scope(self.project).entities
        scoped_keys = {node.key for node in scope}
        for conversation in self.graph.capture_conversations(sorted(scoped_keys)):
            self.capture.store(CaptureEvent(
                source="conversation", event_id=conversation.key, project_key=self.project,
                timestamp=conversation.timestamp, summary=conversation.summary,
                related_keys=[conversation.key, *conversation.decision_keys, *conversation.risk_keys, *conversation.task_keys],
                evidence=[f"Conversation summary {conversation.key}"],
            ))

        for node in scope:
            if node.entity_type not in {"ADR", "Task", "Constraint", "ConversationSummary", "Repository"}:
                continue
            entity = self.graph.read_entity(node.key)
            now = datetime.now(UTC)
            related = [node.key]
            if node.entity_type == "ADR" and node.memory_key:
                document = self.capture.memory.read_document(node.memory_key)
                if document is None:
                    continue
                fields = document.model_dump(include={
                    "key", "title", "status", "decision", "rationale", "consequences",
                    "effective_date", "expiration_date", "conflict_scope",
                })
                source, summary = "adr", f"ADR {document.title} recorded as {document.status or 'undesignated'}."
            elif isinstance(entity, ConversationSummary):
                related.extend(key for key in (*entity.decision_keys, *entity.risk_keys, *entity.task_keys)
                               if key in scoped_keys)
                source, summary, now = "conversation", entity.summary, entity.timestamp
                fields = entity.model_dump()
            elif isinstance(entity, Task):
                source, summary = "task", f"Task {entity.name} recorded as {entity.status}."
                fields = entity.model_dump(include={"name", "description", "status"})
            elif isinstance(entity, Constraint):
                source, summary = "constraint", f"Constraint {entity.name} recorded as {entity.enforcement_status}."
                fields = entity.model_dump()
            elif isinstance(entity, Repository) and entity.role == "source":
                version = self.graph.last_index_revision(entity.key)
                if not version:
                    continue
                source, summary, now = "index", f"Code index refreshed for {entity.name}.", version
                fields = {"index": version}
            else:
                continue
            self.capture.store(CaptureEvent(
                source=source, event_id=node.key if source == "conversation" else f"{node.key}:{_fingerprint(fields)}",
                project_key=self.project, timestamp=now, summary=summary[:4000],
                related_keys=related, evidence=[f"Graph entity {node.key}"],
            ))

    async def read_github(self, checkpoint: CaptureCheckpoint) -> dict:
        headers = {"Accept": "application/vnd.github+json"}
        token = self.settings.ci_github_read_token
        if token:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        async with httpx.AsyncClient(
            base_url=f"https://api.github.com/repos/{self.settings.capture_github_repository}/",
            headers=headers, timeout=10,
        ) as github:
            return await self.scan_github(github, checkpoint)

    async def scan_github(self, github: httpx.AsyncClient, checkpoint: CaptureCheckpoint) -> dict:
        stream = checkpoint.stream
        pull = stream == "pulls"
        path = "pulls" if pull else "commits"
        params = {"state": "all", "sort": "updated", "direction": "desc"} if pull else {
            "sha": self.settings.ci_github_branch,
        }
        if checkpoint.history_since and not pull:
            params["since"] = checkpoint.history_since.isoformat()

        async def page_rows(number, size):
            response = await github.get(path, params={**params, "page": number, "per_page": size})
            response.raise_for_status()
            return response.json()

        latest = await page_rows(1, 1)
        if not latest:
            if checkpoint.cursor and not checkpoint.history and not pull:
                raise CaptureHistoryError(_UNREACHABLE_HEAD)
            return {}
        head = latest[0]
        stamp = _github_time(head, pull)
        identity = head["updated_at"] if pull else head["sha"]
        checkpoint.observed_head = identity
        checkpoint.observed_timestamp = stamp
        progress = {"cursor": identity, "source_timestamp": stamp}
        if stream == "index":
            await self.index_github_head(github, head)
            return progress
        if not pull and checkpoint.cursor and not checkpoint.history:
            if identity != checkpoint.cursor:
                await self.compare_commits(github, checkpoint.cursor, identity)
            return progress

        baseline = not checkpoint.cursor and not checkpoint.history
        per_page = 25 if baseline else 100
        floor = checkpoint.history_since
        if pull and not floor and checkpoint.source_timestamp:
            floor = checkpoint.source_timestamp - timedelta(minutes=5)
        if not pull:
            params["sha"] = identity
        pending = []
        for page in range(1, 101):
            rows = await page_rows(page, per_page)
            eligible = [row for row in rows if not floor or _github_time(row, pull) >= floor]
            pending.extend(eligible)
            if len(rows) < per_page or (pull and len(eligible) < len(rows)) or baseline:
                break
        else:
            raise CaptureHistoryError("History scan exceeded 10,000 items. Request a narrower backfill.")
        if pull and (await page_rows(1, 1)) != latest:
            raise CaptureHistoryError("Pull requests changed during pagination. Retrying from the saved cursor.")
        for row in reversed(pending):
            await self.read_github_change(github, row, pull=pull)
        if checkpoint.history == "replay":
            return {}
        if checkpoint.history == "backfill" and checkpoint.cursor and (
            pull or checkpoint.failure != _UNREACHABLE_HEAD
        ):
            return {}
        return progress

    async def compare_commits(self, github: httpx.AsyncClient, base: str, head: str) -> None:
        path = f"compare/{base}...{head}"
        rows = []
        for page in range(1, 101):
            response = await github.get(path, params={"page": page, "per_page": 100})
            if response.status_code == 404:
                raise CaptureHistoryError(_UNREACHABLE_HEAD)
            response.raise_for_status()
            payload = response.json()
            if payload["status"] not in {"ahead", "identical"}:
                raise CaptureHistoryError(_UNREACHABLE_HEAD)
            rows.extend(payload["commits"])
            if len(rows) >= payload["total_commits"]:
                break
            if not payload["commits"]:
                raise CaptureHistoryError("GitHub omitted part of the commit comparison")
        else:
            raise CaptureHistoryError("Commit comparison exceeded 10,000 items. Request a backfill.")
        if len({row["sha"] for row in rows}) != payload["total_commits"]:
            raise CaptureHistoryError("GitHub returned an incomplete commit comparison")
        for row in rows:
            await self.read_github_change(github, row, pull=False)

    async def index_github_head(self, github: httpx.AsyncClient, head: dict) -> None:
        commit = CommitCapture(
            sha=head["sha"], timestamp=head["commit"]["committer"]["date"],
            subject="", changed_files=[], diff="",
        )
        key = architecture_key("capture", self.project, "index", f"{self.repository}:{commit.sha}")
        if await anyio.to_thread.run_sync(self.graph.read_entity, key):
            return
        archive = bytearray()
        async with github.stream("GET", f"zipball/{commit.sha}", follow_redirects=True) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                archive.extend(chunk)
                if len(archive) > _MAX_ENVELOPE_BYTES:
                    raise ValueError("GitHub archive exceeds capture limit")
        await anyio.to_thread.run_sync(
            lambda: self.index_snapshot(commit, _read_python_zip(archive), datetime.now(UTC)),
        )

    async def read_github_change(self, github: httpx.AsyncClient, row: dict, *, pull: bool) -> None:
        if pull:
            number = int(row["number"])
            event_id = f"{self.repository}:{number}:{row['head']['sha']}:{row['state']}:{row['updated_at']}"
            path, source = f"pulls/{number}/files", "pull_request"
            timestamp = datetime.fromisoformat(row["updated_at"])
            why = f"{row['title']} ({'merged' if row.get('merged_at') else row['state']})"
        else:
            sha = row["sha"]
            if len(sha) != 40 or any(letter not in "0123456789abcdef" for letter in sha):
                raise ValueError("Invalid GitHub commit identity")
            event_id = f"{self.repository}:{sha}"
            path, source = f"commits/{sha}", "commit"
            timestamp = datetime.fromisoformat(row["commit"]["committer"]["date"])
            why = row["commit"]["message"].split("\n", 1)[0]
        key = architecture_key("capture", self.project, source, event_id)
        if await anyio.to_thread.run_sync(self.graph.read_entity, key):
            return
        response = await github.get(path, params={"per_page": 100})
        response.raise_for_status()
        payload = response.json()
        files = payload if pull else payload["files"]
        paths = [file["filename"] for file in files[:100]]
        diff = "\n".join(f"+++ b/{file['filename']}\n{file.get('patch', '')}" for file in files[:100])
        await anyio.to_thread.run_sync(partial(
            self.capture.change, self.project, source, event_id, timestamp, diff[:250_000],
            explicit=[self.repository], paths=paths, repository_key=self.repository, why=why,
            evidence=[row["html_url"]], partial=len(diff) > 250_000 or len(files) >= 100,
        ))


class CaptureHistoryError(RuntimeError):
    pass


def _github_time(row: dict, pull: bool) -> datetime:
    return datetime.fromisoformat(row["updated_at"] if pull else row["commit"]["committer"]["date"])


def _read_python_zip(archive: bytes | bytearray) -> dict[str, str]:
    files = {}
    total = 0
    with ZipFile(BytesIO(archive)) as snapshot:
        for member in snapshot.infolist():
            path = PurePosixPath(member.filename)
            if member.is_dir() or path.suffix != ".py":
                continue
            if len(path.parts) < 2 or path.is_absolute() or ".." in path.parts:
                raise ValueError("Invalid path in GitHub archive")
            total += member.file_size
            if total > MAX_SOURCE_BYTES or len(files) >= MAX_FILES:
                raise ValueError("Python snapshot exceeds capture limits")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative in files:
                raise ValueError("Duplicate path in GitHub archive")
            files[relative] = snapshot.read(member).decode("utf-8")
    return files
