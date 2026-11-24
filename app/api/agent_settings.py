"""Administrator-only management of task agent connections."""

import anyio
from fastapi import APIRouter, HTTPException, Request

from app.auth.permissions import access_role
from app.domain.exceptions import ArchitectureGraphError
from app.services.agent_settings import AgentEdit


def build_agent_settings_router(prefix, registry):
    router = APIRouter(prefix=f"{prefix}/admin/agents", tags=["Agent settings"])

    def admin(request):
        claims = getattr(request.state, "auth_claims", {})
        if not claims.get("subject") or access_role(claims) != "admin":
            raise HTTPException(403, "Administrator access is required")
        return claims["subject"]

    @router.get("")
    async def read(request: Request):
        admin(request)
        revision, agents = await anyio.to_thread.run_sync(registry.read)
        return {
            "revision": revision,
            "agents": agents,
            "allowed_urls": {agent: sorted(urls) for agent, urls in registry.allowed.items()},
        }

    @router.post("")
    async def save(edit: AgentEdit, request: Request):
        actor = admin(request)
        try:
            await anyio.to_thread.run_sync(registry.save, edit, actor)
        except ArchitectureGraphError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await read(request)

    @router.post("/test")
    async def test_connection(edit: AgentEdit, request: Request):
        admin(request)
        try:
            return await registry.probe(edit)
        except (ArchitectureGraphError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    return router
