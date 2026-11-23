from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException
from fastmcp.utilities.lifespan import combine_lifespans

from app.api.effectiveness import build_effectiveness_router
from app.api.review import REVIEW_API_PREFIX, build_review_router
from app.api.test_runs import build_test_run_router
from app.auth.browser_oidc import BrowserOIDC
from app.auth.jwt import JWTVerifier
from app.auth.middleware import BearerTokenMiddleware
from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.config.telemetry import configure_telemetry, instrument_fastapi
from app.domain.review_details import SearchEmbeddingDiagnostics
from app.mcp.server import build_mcp_server
from app.repositories.factory import build_repository
from app.services.architecture_service import ArchitectureService
from app.services.ask_graph import AskGraphService
from app.services.capture_worker import CaptureWorker
from app.services.context_package import ContextPackageService
from app.services.effectiveness import EffectivenessService
from app.services.embeddings import EmbeddingService
from app.services.github_test_runs import GitHubBranchHead, poll_test_runs
from app.services.ingestion_service import MemoryIngestionService
from app.services.knowledge_base import StaticKnowledgeBase
from app.services.knowledge_capture import KnowledgeCaptureService
from app.services.observability import MemoryObservability
from app.services.project_changes import ProjectChangesService
from app.services.review_details import ReviewDetailService
from app.services.review_queue import ReviewQueueService
from app.services.search_service import MemorySearchService
from app.services.test_fix import TestFixService
from app.services.token_usage import TokenUsageService

logger = structlog.get_logger(__name__)


def create_application(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    configure_telemetry(settings)

    repository = build_repository(settings)
    observability = MemoryObservability()
    embedding_service = EmbeddingService.from_settings(settings)
    service = MemorySearchService(repository=repository, embedding_service=embedding_service, observability=observability)
    ingestion_service = MemoryIngestionService(repository=repository, embedding_service=embedding_service, observability=observability)
    knowledge_base = StaticKnowledgeBase()
    graph_repository = getattr(repository, "architecture", None)
    ci_head = GitHubBranchHead()
    architecture_service = ArchitectureService(graph_repository, repository) if graph_repository else None
    mcp = build_mcp_server(
        settings=settings,
        service=service,
        knowledge_base=knowledge_base,
        ingestion_service=ingestion_service,
        architecture_service=architecture_service,
    )
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.repository = repository
        app.state.service = service
        app.state.ingestion_service = ingestion_service
        app.state.embedding_service = embedding_service
        app.state.observability = observability
        app.state.mcp = mcp
        embedding_status = await anyio.to_thread.run_sync(embedding_service.ensure_ready)
        app.state.embedding_status = embedding_status
        if embedding_status["status"] == "unavailable" and settings.embed_require_ready:
            raise RuntimeError(embedding_status["error_summary"] or "Embedding capability unavailable")
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
        logger.info("memorygraphmcp_starting", environment=settings.environment, embedding_status=embedding_status)
        async with anyio.create_task_group() as background:
            if graph_repository and all((
                settings.ci_github_repository, settings.ci_project_key,
                settings.ci_repository_key,
            )):
                background.start_soon(poll_test_runs, settings, graph_repository, ci_head)
            if graph_repository and settings.capture_project_key:
                worker = CaptureWorker(settings, KnowledgeCaptureService(graph_repository, repository))
                background.start_soon(worker.run)
            try:
                yield
            finally:
                background.cancel_scope.cancel()
        logger.info("memorygraphmcp_stopping")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=combine_lifespans(lifespan, mcp_app.lifespan),
    )
    verifier = JWTVerifier(settings)
    browser_oidc = BrowserOIDC(settings, verifier)
    app.include_router(browser_oidc.router())
    if settings.auth_required:
        app.add_middleware(
            BearerTokenMiddleware,
            verifier=verifier,
            protected_prefix=settings.mcp_mount_path,
            additional_prefixes=(REVIEW_API_PREFIX,),
            browser=browser_oidc,
        )
    provider = embedding_service.primary_provider
    review_details = ReviewDetailService(
        repository,
        graph_repository,
        SearchEmbeddingDiagnostics(
            provider=provider.name,
            model=provider.model,
            dimension=provider.dimension,
            version=getattr(provider, "version", None),
        ),
    )
    context_packages = ContextPackageService(graph_repository, repository) if graph_repository else None
    app.include_router(build_review_router(
        ReviewQueueService(repository), review_details,
        AskGraphService(repository, review_details, context_packages), settings.auth_required, service,
        ci_github_repository=settings.ci_github_repository,
        ci_project_key=settings.ci_project_key,
        ci_head=ci_head.current,
        tracker_project_key=settings.graph_event_project_key,
        ci_repository_key=settings.ci_repository_key,
        test_fixes=TestFixService(settings, graph_repository, repository)
        if graph_repository else None,
        changes=ProjectChangesService(settings, graph_repository) if graph_repository else None,
        context_packages=context_packages,
        usage=TokenUsageService(graph_repository) if graph_repository and hasattr(graph_repository, "record_usage") else None,
    ))
    if graph_repository is not None and hasattr(graph_repository, "effectiveness_graph"):
        app.include_router(build_effectiveness_router(
            EffectivenessService(graph_repository, repository), REVIEW_API_PREFIX,
        ))
    app.include_router(build_test_run_router(settings, graph_repository))
    app.mount(settings.mcp_mount_path, mcp_app)
    instrument_fastapi(app)

    async def get_embedding_status() -> dict[str, object]:
        status = getattr(app.state, "embedding_status", None)
        if status is None:
            return await anyio.to_thread.run_sync(embedding_service.ensure_ready)
        return status

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        embedding_status = await get_embedding_status()
        status = "ok" if embedding_status["status"] in {"ready", "degraded"} else "error"
        payload = {
            "status": status,
            "embedding.status": embedding_status["status"],
            "embedding.model": embedding_status["model"],
            "embedding.provider_url": embedding_status["provider_url"],
            "embedding.last_probe_at": embedding_status.get("last_probe_at"),
            "embedding.error_summary": embedding_status.get("error_summary"),
        }
        return payload

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, object]:
        try:
            await anyio.to_thread.run_sync(repository.ensure_connected)
        except Exception:  # pragma: no cover - readiness should surface infrastructure failures
            raise HTTPException(status_code=503, detail="Not ready")
        embedding_status = await get_embedding_status()
        if embedding_status["status"] == "unavailable" and settings.embed_require_ready:
            raise HTTPException(status_code=503, detail="Embedding capability unavailable")
        return {"status": "ready", "embedding.status": embedding_status["status"], "semantic_search_available": embedding_status["status"] in {"ready", "degraded"}}

    @app.get("/capabilities", include_in_schema=False)
    async def capabilities() -> dict[str, object]:
        embedding_status = await get_embedding_status()
        return {
            "semantic_search_available": embedding_status["status"] in {"ready", "degraded"},
            "embedding": {
                "status": embedding_status["status"],
                "model": embedding_status["model"],
                "provider_url": embedding_status["provider_url"],
                "last_probe_at": embedding_status.get("last_probe_at"),
                "error_summary": embedding_status.get("error_summary"),
            },
        }

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
