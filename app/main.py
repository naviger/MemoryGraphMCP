from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException
from fastmcp.utilities.lifespan import combine_lifespans

from app.auth.jwt import JWTVerifier
from app.auth.middleware import BearerTokenMiddleware
from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.config.telemetry import configure_telemetry, instrument_fastapi
from app.mcp.server import build_mcp_server
from app.repositories.factory import build_repository
from app.services.knowledge_base import StaticKnowledgeBase
from app.services.search_service import MemorySearchService

logger = structlog.get_logger(__name__)


def create_application(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    configure_telemetry(settings)

    repository = build_repository(settings)
    service = MemorySearchService(repository=repository)
    knowledge_base = StaticKnowledgeBase()
    mcp = build_mcp_server(settings=settings, service=service, knowledge_base=knowledge_base)
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.repository = repository
        app.state.service = service
        app.state.mcp = mcp
        await anyio.to_thread.run_sync(repository.ensure_storage_ready)
        logger.info(
            "memorygraphmcp_storage_ready",
            database=settings.arango_database,
            username=settings.arango_username,
            documents_collection=settings.arango_documents_collection,
            edges_collection=settings.arango_edges_collection,
            vectors_collection=settings.arango_vectors_collection,
            search_view=settings.arango_search_view,
            vector_view=settings.arango_vector_view,
        )
        logger.info("memorygraphmcp_starting", environment=settings.environment)
        yield
        logger.info("memorygraphmcp_stopping")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=combine_lifespans(lifespan, mcp_app.lifespan),
    )
    if settings.auth_required:
        app.add_middleware(
            BearerTokenMiddleware,
            verifier=JWTVerifier(settings),
            protected_prefix=settings.mcp_mount_path,
        )
    app.mount(settings.mcp_mount_path, mcp_app)
    instrument_fastapi(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, str]:
        try:
            await anyio.to_thread.run_sync(repository.ensure_connected)
        except Exception:  # pragma: no cover - readiness should surface infrastructure failures
            raise HTTPException(status_code=503, detail="Not ready")
        return {"status": "ready"}

    return app


app = create_application()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.http_host,
        port=settings.http_port,
        reload=False,
        factory=False,
    )


if __name__ == "__main__":
    main()
