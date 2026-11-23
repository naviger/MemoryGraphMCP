"""Read GitHub history and join it to the project's imported CI and cleanup evidence."""

import asyncio
import re

import anyio
import httpx

from app.config.settings import Settings
from app.domain.architecture import Project
from app.domain.project_changes import (
    ChangeCI,
    ChangeRepository,
    ChangeView,
    ProjectChange,
    ProjectChanges,
)
from app.repositories.architecture_protocol import ArchitectureRepository

_LIMIT = 25
_SLUG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*")


class ProjectChangesService:
    def __init__(self, settings: Settings, graph: ArchitectureRepository):
        self.settings = settings
        self.graph = graph
        self.github_repositories = dict(
            (key.strip(), slug.strip())
            for entry in settings.github_change_repositories.split(",")
            for key, sep, slug in [entry.partition("=")]
            if sep
        )

    def _repositories(self, project: str) -> list[ChangeRepository] | None:
        if not isinstance(self.graph.read_entity(project), Project):
            return None
        repositories = []
        for node in self.graph.project_source_repositories(project):
            slug = self.github_repositories.get(node.key)
            if (
                project == self.settings.ci_project_key
                and node.key == self.settings.ci_repository_key
            ):
                slug = self.settings.ci_github_repository
            repositories.append(ChangeRepository(
                key=node.key,
                name=node.name,
                github=slug if isinstance(slug, str) and _SLUG.fullmatch(slug) else None,
            ))
        return repositories

    async def history(
        self, project: str, view: ChangeView, repository: str | None,
    ) -> ProjectChanges | None:
        repos = await anyio.to_thread.run_sync(self._repositories, project)
        if repos is None:
            return None
        selected = [repo for repo in repos if repository is None or repo.key == repository]
        if repository and not selected:
            raise LookupError("Repository is not linked to this project.")
        headers = {"Accept": "application/vnd.github+json"}
        token = self.settings.ci_github_read_token
        if token and token.get_secret_value():
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        unavailable = []
        limiter = asyncio.Semaphore(4)
        async with httpx.AsyncClient(
            base_url="https://api.github.com/repos/", headers=headers, timeout=10,
        ) as github:
            async def read(repo: ChangeRepository) -> list[ProjectChange]:
                if not repo.github:
                    return []
                async with limiter:
                    try:
                        return await self._history(github, project, view, repo)
                    except (httpx.HTTPError, KeyError, TypeError, ValueError):
                        unavailable.append(repo.key)
                        return []

            groups = await asyncio.gather(*(read(repo) for repo in selected))
        changes = sorted(
            (row for group in groups for row in group),
            key=lambda row: (row.date, row.url), reverse=True,
        )
        return ProjectChanges(
            repositories=repos, changes=changes,
            unavailable_repositories=sorted(unavailable), limit_per_repository=_LIMIT,
        )

    async def _history(
        self, github: httpx.AsyncClient, project: str, view: ChangeView, repo: ChangeRepository,
    ) -> list[ProjectChange]:
        params = {"per_page": str(_LIMIT)}
        if view == "pulls":
            params.update(state="all", sort="updated", direction="desc")
        response = await github.get(f"{repo.github}/{view}", params=params)
        if view == "commits" and response.status_code == 409:
            return []
        response.raise_for_status()
        base = f"https://github.com/{repo.github}"
        rows = [
            self._pull(repo.key, base, item) if view == "pulls"
            else self._commit(repo.key, base, item)
            for item in response.json()
        ]
        commits = list({row.commit for row in rows if row.commit})
        runs = await anyio.to_thread.run_sync(
            self.graph.change_test_runs, project, repo.key, commits,
        ) if commits else {}
        fixes = {}
        if view == "pulls" and (
            project == self.settings.ci_project_key
            and repo.key == self.settings.ci_repository_key
        ):
            fixes = await anyio.to_thread.run_sync(
                self.graph.change_branch_cleanups, project, [row.url for row in rows],
            )
        for row in rows:
            run = runs.get(row.commit)
            if run:
                run_id, attempt = run.run_id.split(":")
                row.ci = ChangeCI(
                    run_id=run.run_id, status=run.status,
                    url=f"{base}/actions/runs/{run_id}/attempts/{attempt}",
                )
            if row.url in fixes:
                row.cleanup = fixes[row.url]
        return rows

    @staticmethod
    def _pull(key: str, base: str, item: dict) -> ProjectChange:
        merged = item["merged_at"]
        status = item["state"]
        if merged:
            status = "merged"
        elif status == "open" and item["draft"]:
            status = "draft"
        number = int(item["number"])
        return ProjectChange(
            repository_key=key, number=number, title=item["title"], url=f"{base}/pull/{number}",
            author=(item.get("user") or {}).get("login"), date=merged or item["created_at"],
            status=status, commit=item["merge_commit_sha"] if merged else item["head"]["sha"],
        )

    @staticmethod
    def _commit(key: str, base: str, item: dict) -> ProjectChange:
        sha = item["sha"]
        commit = item["commit"]
        author = commit.get("author") or {}
        return ProjectChange(
            repository_key=key, commit=sha, url=f"{base}/commit/{sha}", title=commit["message"],
            author=(item.get("author") or {}).get("login") or author.get("name"),
            date=(commit.get("committer") or {}).get("date") or author["date"],
        )
