"""Project-granted task and knowledge capture endpoints."""

from datetime import UTC, datetime
from functools import partial

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.auth.permissions import WRITER_REQUIRED, writer_subject
from app.auth.project_access import current_project_access
from app.domain.capture_checkpoint import CaptureHistoryInput, CaptureStatus
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import MemoryKey
from app.domain.project_workspace import (
    AssistDraft,
    RecommendAgent,
    StartExecution,
    TaskUpdate,
    WorkDraft,
)


def build_workspace_router(prefix, workspace, executor, capture):
    router = APIRouter(prefix=f"{prefix}/projects", tags=["Project workspace"])

    def authorize(project, request, *, write=False):
        access = current_project_access.get()
        if access is not None:
            access.require(project, write=write)
        actor = writer_subject(getattr(request.state, "auth_claims", {}))
        if write and not actor:
            raise HTTPException(403, WRITER_REQUIRED)
        return actor

    async def invoke(fn, *args):
        try:
            return await anyio.to_thread.run_sync(partial(fn, *args))
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/{project}/workspace")
    async def overview(project: MemoryKey, request: Request):
        actor = authorize(project, request)
        access = current_project_access.get()
        can_write = bool(
            actor and (not access or access.unrestricted or access.grants.get(project) == "write")
        )
        items = await invoke(workspace.entities, project)
        await executor.refresh()
        return {
            "items": [
                node for node in items if node.entity_type in {"Task", "Goal", "Risk", "Constraint"}
            ],
            "can_write": can_write,
            "agents": executor.modes(),
            "draft_available": bool(executor.local.url),
        }

    @router.post("/{project}/workspace")
    async def create(project: MemoryKey, draft: WorkDraft, request: Request):
        actor = authorize(project, request, write=True)
        return await invoke(workspace.create, project, draft, actor)

    @router.post("/{project}/workspace/draft")
    async def draft(project: MemoryKey, data: AssistDraft, request: Request):
        authorize(project, request, write=True)
        try:
            await executor.refresh()
            return await executor.local.draft(data)
        except (
            ArchitectureGraphError,
            httpx.HTTPError,
            ValidationError,
            KeyError,
            ValueError,
        ) as exc:
            raise HTTPException(503, "The local model could not generate a usable draft") from exc

    @router.post("/{project}/workspace/tasks/{task}")
    async def update(project: MemoryKey, task: MemoryKey, data: TaskUpdate, request: Request):
        actor = authorize(project, request, write=True)
        return await invoke(workspace.task_update, project, task, data, actor)

    @router.post("/{project}/workspace/tasks/{task}/recommend-agent")
    async def recommend(
        project: MemoryKey, task: MemoryKey, data: RecommendAgent, request: Request
    ):
        authorize(project, request, write=True)
        record = await invoke(workspace.task, project, task)
        try:
            return await executor.recommend(record, data.mode)
        except (ArchitectureGraphError, httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(503, "Could not obtain an eligible agent recommendation") from exc

    @router.get("/{project}/workspace/tasks/{task}/executions")
    async def history(project: MemoryKey, task: MemoryKey, request: Request):
        authorize(project, request)
        return await invoke(workspace.executions, project, task)

    @router.post("/{project}/workspace/tasks/{task}/executions")
    async def start(project: MemoryKey, task: MemoryKey, command: StartExecution, request: Request):
        actor = authorize(project, request, write=True)
        await executor.refresh()
        if command.mode not in executor.modes().get(command.agent, []):
            raise HTTPException(422, "This agent does not support the selected mode")
        return await invoke(workspace.start, project, task, command, actor)

    @router.post("/{project}/workspace/tasks/{task}/executions/{execution}/retry")
    async def retry(project: MemoryKey, task: MemoryKey, execution: MemoryKey, request: Request):
        actor = authorize(project, request, write=True)
        return await invoke(workspace.retry_execution, project, task, execution, actor)

    @router.post("/{project}/workspace/tasks/{task}/executions/{execution}/cancel")
    async def cancel(project: MemoryKey, task: MemoryKey, execution: MemoryKey, request: Request):
        actor = authorize(project, request, write=True)
        jobs = await invoke(workspace.executions, project, task)
        job = next((item for item in jobs if item.key == execution), None)
        if job is None:
            raise HTTPException(404, "Execution not found")
        try:
            return await executor.cancel(job, actor)
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(503, "The agent could not confirm cancellation") from exc

    @router.get("/{project}/capture")
    async def capture_status(project: MemoryKey, request: Request) -> CaptureStatus:
        authorize(project, request)
        if not capture or project != capture.project:
            return CaptureStatus(configured=False, interval_seconds=0)
        return await invoke(capture.status)

    @router.post("/{project}/capture")
    async def schedule(project: MemoryKey, data: CaptureHistoryInput, request: Request):
        actor = authorize(project, request, write=True)
        if not capture or project != capture.project or data.stream not in capture.streams():
            raise HTTPException(404, "Capture stream is not configured for this project")
        if data.since >= datetime.now(UTC):
            raise HTTPException(422, "Choose a date in the past")
        return await invoke(capture.request_history, data, actor)

    return router
