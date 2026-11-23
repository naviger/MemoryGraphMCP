from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.auth.permissions import WRITER_REQUIRED, writer_subject
from app.domain.effectiveness import (
    DevelopmentOutcome,
    EffectivenessQuery,
    EffectivenessReport,
    OutcomeReceipt,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import MemoryKey
from app.services.effectiveness import EffectivenessService


def build_effectiveness_router(service: EffectivenessService, prefix: str) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["Engineering effectiveness"])

    @router.get("/effectiveness")
    async def get_effectiveness(
        query: Annotated[EffectivenessQuery, Query()], response: Response,
    ) -> EffectivenessReport:
        try:
            report = await service.summary(query)
        except ArchitectureGraphError as exc:
            raise HTTPException(404, str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return report

    @router.get("/effectiveness/repositories")
    async def get_repositories(project_key: MemoryKey, response: Response) -> dict[str, str]:
        try:
            repositories = await service.repositories(project_key)
        except ArchitectureGraphError as exc:
            raise HTTPException(404, str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return repositories

    @router.post("/effectiveness/outcomes")
    async def report_outcome(outcome: DevelopmentOutcome, request: Request) -> OutcomeReceipt:
        actor = writer_subject(getattr(request.state, "auth_claims", {}))
        if actor is None:
            raise HTTPException(403, WRITER_REQUIRED)
        try:
            return await service.report(outcome, actor)
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
