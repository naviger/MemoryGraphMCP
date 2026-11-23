"""Background adapters for local commits, GitHub activity, and graph changes."""
from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

import anyio
import httpx
import structlog

from app.config.settings import Settings
from app.domain.architecture import (
    Constraint,
    ConversationSummary,
    Repository,
    Task,
    architecture_key,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.knowledge_capture import CaptureEvent, CommitCapture, CommitEnvelope
from app.domain.test_runs import TestRun
from app.services.knowledge_capture import KnowledgeCaptureService, _fingerprint
from app.services.python_architecture import MAX_FILES, MAX_SOURCE_BYTES, PythonArchitectureIndexer

logger = structlog.get_logger(__name__)
_MAX_ENVELOPE_BYTES = 24_000_000


class CaptureWorker:
    def __init__(self, settings: Settings, capture: KnowledgeCaptureService):
        self.settings = settings
        self.capture = capture
        self.graph = capture.graph
        self.project = settings.capture_project_key
        self.repository = settings.capture_repository_key

    async def run(self) -> None:
        while True:
            for job in (self.read_spool, self.read_graph):
                try:
                    await anyio.to_thread.run_sync(job)
                except Exception as exc:
                    logger.warning("knowledge_capture_source_failed", source=job.__name__,
                                   error_type=type(exc).__name__)
            if self.settings.capture_github_repository:
                try:
                    await self.read_github()
                except Exception as exc:
                    logger.warning("knowledge_capture_source_failed", source="github",
                                   error_type=type(exc).__name__)
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
                continue
            self.capture_local_commit(item)
            path.unlink(missing_ok=True)

    def capture_local_commit(self, item: CommitEnvelope) -> None:
        if item.python_snapshot is not None:
            try:
                self.index_snapshot(item.commit, item.python_snapshot, item.queued_at or item.commit.timestamp)
            except ArchitectureGraphError as exc:
                logger.warning("knowledge_capture_index_rejected", commit=item.commit.sha,
                               error_type=type(exc).__name__)
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
        index = PythonArchitectureIndexer().build(root.name, sources, root=root)
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
                    and repository and node.repository == repository.name
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

    async def read_github(self) -> None:
        slug = self.settings.capture_github_repository
        token = self.settings.ci_github_read_token
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        async with httpx.AsyncClient(
            base_url=f"https://api.github.com/repos/{slug}/", headers=headers, timeout=10,
        ) as github:
            for view, params in (
                ("pulls", {"state": "all", "sort": "updated", "direction": "desc"}),
                ("commits", {"sha": self.settings.ci_github_branch}),
            ):
                response = await github.get(view, params={**params, "per_page": 25})
                response.raise_for_status()
                rows = response.json()
                if view == "commits" and self.settings.capture_index_github and rows:
                    try:
                        await self.index_github_head(github, rows[0])
                    except (httpx.HTTPError, ArchitectureGraphError, BadZipFile, ValueError) as exc:
                        logger.warning("knowledge_capture_index_rejected", error_type=type(exc).__name__)
                for row in reversed(rows):
                    if view == "pulls":
                        await self.read_github_change(github, row, pull=True)
                    else:
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
            why = f"{row['title']} ({'merged' if row['merged_at'] else row['state']})"
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
