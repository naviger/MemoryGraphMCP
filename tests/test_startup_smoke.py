from __future__ import annotations

from pathlib import Path

from app.services.embeddings import EmbeddingService


def test_embedding_service_exposes_readiness_check() -> None:
    service = EmbeddingService.default()

    assert hasattr(service, "ensure_ready")
    status = service.ensure_ready()

    assert status["status"] in {"ready", "degraded", "unavailable"}
    assert "model" in status


def test_compose_ollama_service_avoids_bin_sh_entrypoint() -> None:
    compose_text = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()

    assert "ollama/ollama" in compose_text
    assert "/bin/sh" not in compose_text
    assert "command: serve" in compose_text
