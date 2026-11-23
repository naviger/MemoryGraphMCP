from secrets import compare_digest
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config.settings import Settings
from app.domain.exceptions import ArchitectureGraphError
from app.domain.test_runs import TestRun, TestRunInput
from app.repositories.architecture_protocol import ArchitectureRepository


def build_test_run_router(settings: Settings, graph: ArchitectureRepository | None) -> APIRouter:
    router = APIRouter()
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        secret = settings.ci_test_run_token
        if not (graph and secret and settings.ci_project_key and settings.ci_repository_key):
            raise HTTPException(404, "CI test publication is disabled")
        if not credentials or not compare_digest(
            credentials.credentials.encode(), secret.get_secret_value().encode()
        ):
            raise HTTPException(
                401, "Invalid CI credential", headers={"WWW-Authenticate": "Bearer"}
            )

    @router.post("/api/ci/test-runs", dependencies=[Depends(authorize)])
    async def publish_test_run(payload: TestRunInput) -> TestRun:
        run = TestRun(
            **payload.model_dump(),
            project_key=settings.ci_project_key,
            repository_key=settings.ci_repository_key,
        )
        try:
            return await anyio.to_thread.run_sync(graph.put_test_run, run)
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
