"""Pull completed test runs into the local architecture journal."""

import re
from hashlib import sha256
from io import BytesIO
from time import monotonic
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

import anyio
import httpx
import structlog
from arango.exceptions import ArangoError
from pydantic import ValidationError

from app.config.settings import Settings
from app.domain.exceptions import ArchitectureGraphError
from app.domain.test_runs import TestRun, TestRunInput
from app.repositories.architecture_protocol import ArchitectureRepository

_MAX_BYTES = 2_000_000
_LOG = structlog.get_logger(__name__)


class GitHubBranchHead:
    def __init__(self) -> None:
        self._snapshot: tuple[str, float] | None = None

    def remember(self, commit: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Invalid GitHub commit SHA")
        self._snapshot = (commit, monotonic())

    def current(self) -> str | None:
        if self._snapshot and monotonic() - self._snapshot[1] < 600:
            return self._snapshot[0]
        return None


async def pull_test_runs(
    settings: Settings, graph: ArchitectureRepository, head: GitHubBranchHead | None = None,
) -> None:
    repo = settings.ci_github_repository
    branch = settings.ci_github_branch
    headers = {"Accept": "application/vnd.github+json"}
    if settings.ci_github_read_token:
        headers["Authorization"] = (
            f"Bearer {settings.ci_github_read_token.get_secret_value()}"
        )
    async with httpx.AsyncClient(
        base_url=f"https://api.github.com/repos/{repo}/",
        headers=headers,
        follow_redirects=True,
        timeout=20,
    ) as client:
        if head is not None:
            try:
                branch_url = f"branches/{quote(branch, safe='')}"
                response = await client.get(branch_url)
                if response.status_code in {403, 404} and settings.ci_github_read_token:
                    async with httpx.AsyncClient(timeout=5) as public:
                        response = await public.get(f"https://api.github.com/repos/{repo}/{branch_url}")
                response.raise_for_status()
                head.remember(response.json()["commit"]["sha"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                _LOG.warning("github_branch_head_unavailable", error=type(exc).__name__)
        response = await client.get(
            "actions/workflows/tests.yml/runs",
            params={"branch": branch, "event": "push", "status": "completed", "per_page": 10},
        )
        response.raise_for_status()
        candidates = [
            (f"{run['id']}:{run['run_attempt']}", run)
            for run in reversed(response.json()["workflow_runs"])
            if run["event"] == "push" and run["head_branch"] == branch
            and (run.get("head_repository") or {}).get("full_name", "").casefold() == repo.casefold()
        ]
        known = await anyio.to_thread.run_sync(
            graph.known_test_run_ids, settings.ci_repository_key,
            [run_id for run_id, _ in candidates],
        )
        for run_id, run in candidates:
            if run_id in known:
                continue
            try:
                data = await _read_artifact(client, run)
                if data is None:
                    continue
                report = TestRunInput.model_validate_json(data)
                if report.run_id != run_id or report.commit != run["head_sha"]:
                    raise ValueError("Artifact identity does not match the GitHub workflow run")
                record = TestRun(
                    **report.model_dump(),
                    project_key=settings.ci_project_key,
                    repository_key=settings.ci_repository_key,
                )
                await anyio.to_thread.run_sync(graph.put_test_run, record)
                _LOG.info("github_test_run_imported", run_id=run_id, commit=report.commit)
            except (
                ArchitectureGraphError, BadZipFile, httpx.HTTPError, KeyError,
                ValidationError, ValueError,
            ) as exc:
                _LOG.warning(
                    "github_test_run_import_failed", run_id=run_id, error=type(exc).__name__
                )


async def _read_artifact(client: httpx.AsyncClient, run: dict) -> bytes | None:
    name = f"memorygraph-test-run-{run['run_attempt']}"
    response = await client.get(f"actions/runs/{run['id']}/artifacts", params={"name": name})
    response.raise_for_status()
    artifact = next((
        item for item in response.json()["artifacts"]
        if item["name"] == name and not item["expired"] and item["size_in_bytes"] <= _MAX_BYTES
    ), None)
    if artifact is None:
        return None

    archive = bytearray()
    async with client.stream("GET", f"actions/artifacts/{artifact['id']}/zip") as download:
        download.raise_for_status()
        async for chunk in download.aiter_bytes():
            archive.extend(chunk)
            if len(archive) > _MAX_BYTES:
                raise ValueError("Artifact exceeds download size limit")
    digest = artifact.get("digest")
    if digest and digest != f"sha256:{sha256(archive).hexdigest()}":
        raise ValueError("Artifact digest does not match GitHub metadata")
    with ZipFile(BytesIO(archive)) as package:
        member = package.getinfo("test-run.json")
        if member.file_size > _MAX_BYTES:
            raise ValueError("Test report exceeds size limit")
        return package.read(member)


async def poll_test_runs(
    settings: Settings, graph: ArchitectureRepository, head: GitHubBranchHead,
) -> None:
    while True:
        try:
            await pull_test_runs(settings, graph, head)
        except (ArangoError, httpx.HTTPError, KeyError, ValueError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            _LOG.warning("github_test_run_poll_failed", status=status, error=type(exc).__name__)
        await anyio.sleep(120)
