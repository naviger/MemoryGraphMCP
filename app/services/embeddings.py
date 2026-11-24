from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

import httpx
import structlog

from app.config.settings import Settings
from app.domain.exceptions import EmbeddingError
from app.domain.models import MemoryDocument

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EmbeddingArtifact:
    vector: list[float]
    provider: str
    model: str
    dimension: int
    version: str | None = None
    fallback_used: bool = False
    latency_ms: float = 0.0


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimension: int
    version: str | None

    def embed(self, text: str) -> list[float]:
        ...


class DeterministicEmbeddingProvider:
    name = "deterministic"

    def __init__(self, dimension: int, model: str = "deterministic-hash", version: str = "1") -> None:
        if dimension < 1:
            raise ValueError("Embedding dimension must be greater than zero")
        self.dimension = dimension
        self.model = model
        self.version = version

    def ensure_model(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "provider": self.name,
            "model": self.model,
            "dimension": self.dimension,
            "provider_url": "internal",
            "last_probe_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def embed(self, text: str) -> list[float]:
        tokens = re.findall(r"[A-Za-z0-9_]+", text.lower())
        if not tokens:
            tokens = [text.lower() or "empty"]

        vector = [0.0] * self.dimension
        for index, token in enumerate(tokens):
            token_hash = int.from_bytes(sha256(f"{index}:{token}".encode("utf-8")).digest(), "big")
            position = token_hash % self.dimension
            magnitude = 1.0 + ((token_hash >> 8) % 1000) / 1000.0
            sign = 1.0 if token_hash % 2 == 0 else -1.0
            vector[position] += sign * magnitude

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [round(value / norm, 8) for value in vector]


def _ollama_model_tag(model: str) -> str:
    # Ollama lists an untagged model under :latest. A registry port is not a tag.
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"


class OllamaEmbeddingProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, dimension: int, timeout_seconds: float, version: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimension = dimension
        self.version = version
        self._timeout = timeout_seconds

    def ensure_model(self) -> None:
        with httpx.Client(base_url=self.base_url, timeout=self._timeout) as client:
            response = client.post("/api/pull", json={"name": self.model})
            if response.status_code >= 400:
                raise RuntimeError(f"Embedding model pull failed: {response.text}")

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(base_url=self.base_url, timeout=self._timeout) as client:
                response = client.get("/api/tags")
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # pragma: no cover - service may be unreachable
            return {
                "status": "unavailable",
                "provider": self.name,
                "model": self.model,
                "dimension": self.dimension,
                "provider_url": self.base_url,
                "last_probe_at": None,
                "error_summary": str(exc),
            }

        items = payload.get("models", []) if isinstance(payload, dict) else []
        configured_tag = _ollama_model_tag(self.model)
        available = any(
            isinstance(item, dict)
            and isinstance(name := item.get("name"), str)
            and _ollama_model_tag(name) == configured_tag
            for item in items
        )
        status = "ready" if available else "degraded"
        return {
            "status": status,
            "provider": self.name,
            "model": self.model,
            "dimension": self.dimension,
            "provider_url": self.base_url,
            "last_probe_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error_summary": None if available else f"Model '{self.model}' is not available",
        }

    def embed(self, text: str) -> list[float]:
        with httpx.Client(base_url=self.base_url, timeout=self._timeout) as client:
            response = client.post("/api/embeddings", json={"model": self.model, "prompt": text})
            response.raise_for_status()
            payload = response.json()

        embedding = payload.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise EmbeddingError("Ollama embedding response did not include an embedding vector")
        return [float(value) for value in embedding]


class EmbeddingService:
    def __init__(
        self,
        primary_provider: EmbeddingProvider,
        fallback_provider: EmbeddingProvider,
        strict_mode: bool = False,
        auto_pull: bool = False,
        require_ready: bool = False,
    ) -> None:
        self.primary_provider = primary_provider
        self.fallback_provider = fallback_provider
        self.strict_mode = strict_mode
        self.auto_pull = auto_pull
        self.require_ready = require_ready

    @classmethod
    def default(cls, dimension: int = 1536) -> "EmbeddingService":
        provider = DeterministicEmbeddingProvider(dimension=dimension)
        return cls(primary_provider=provider, fallback_provider=provider, strict_mode=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> "EmbeddingService":
        fallback = DeterministicEmbeddingProvider(
            dimension=settings.embedding_dimension,
            model="deterministic-hash",
            version="1",
        )
        if settings.embedding_provider.lower() == "ollama":
            primary: EmbeddingProvider = OllamaEmbeddingProvider(
                base_url=settings.embedding_base_url,
                model=settings.embedding_model,
                dimension=settings.embedding_dimension,
                timeout_seconds=settings.embedding_timeout_seconds,
                version=settings.embedding_version,
            )
        else:
            primary = fallback
        return cls(
            primary_provider=primary,
            fallback_provider=fallback,
            strict_mode=settings.embedding_strict_mode,
            auto_pull=getattr(settings, "embed_auto_pull", True),
            require_ready=getattr(settings, "embed_require_ready", False),
        )

    def ensure_ready(self) -> dict[str, Any]:
        try:
            if self.auto_pull and hasattr(self.primary_provider, "ensure_model"):
                self.primary_provider.ensure_model()
            status = self.primary_provider.health()
        except Exception as exc:
            status = {
                "status": "unavailable",
                "provider": self.primary_provider.name,
                "model": self.primary_provider.model,
                "dimension": self.primary_provider.dimension,
                "provider_url": getattr(self.primary_provider, "base_url", "internal"),
                "last_probe_at": None,
                "error_summary": str(exc),
            }

        if status.get("status") == "ready":
            return {
                "status": "ready",
                "model": status.get("model", self.primary_provider.model),
                "provider_url": status.get("provider_url", getattr(self.primary_provider, "base_url", "internal")),
                "last_probe_at": status.get("last_probe_at"),
                "error_summary": status.get("error_summary"),
            }

        if self.require_ready:
            return {
                "status": "unavailable",
                "model": status.get("model", self.primary_provider.model),
                "provider_url": status.get("provider_url", getattr(self.primary_provider, "base_url", "internal")),
                "last_probe_at": status.get("last_probe_at"),
                "error_summary": status.get("error_summary") or "Embedding capability unavailable",
            }

        return {
            "status": "degraded",
            "model": status.get("model", self.primary_provider.model),
            "provider_url": status.get("provider_url", getattr(self.primary_provider, "base_url", "internal")),
            "last_probe_at": status.get("last_probe_at"),
            "error_summary": status.get("error_summary") or "Embedding capability degraded",
        }

    def embed_query(self, query: str, allow_fallback: bool = True) -> EmbeddingArtifact:
        return self._embed_text(query, allow_fallback=allow_fallback, purpose="query")

    def embed_document(self, document: MemoryDocument, allow_fallback: bool = True) -> EmbeddingArtifact:
        parts = [document.title.strip(), document.content.strip()]
        if document.tags:
            parts.append("tags: " + ", ".join(document.tags))
        if document.kind:
            parts.append(f"kind: {document.kind}")
        return self._embed_text("\n".join(part for part in parts if part), allow_fallback=allow_fallback, purpose="document")

    def embed_client_vector(self, embedding: list[float]) -> EmbeddingArtifact:
        vector = [float(value) for value in embedding]
        if len(vector) != self.primary_provider.dimension:
            raise EmbeddingError(
                f"Client embedding dimension mismatch: expected {self.primary_provider.dimension}, got {len(vector)}"
            )
        return EmbeddingArtifact(
            vector=vector,
            provider="client",
            model="client-provided",
            dimension=len(vector),
            version=None,
            fallback_used=False,
            latency_ms=0.0,
        )

    def _embed_text(self, text: str, allow_fallback: bool, purpose: str) -> EmbeddingArtifact:
        started = time.perf_counter()
        try:
            logger.info(
                "embedding_provider_selected",
                purpose=purpose,
                provider=self.primary_provider.name,
                model=self.primary_provider.model,
                dimension=self.primary_provider.dimension,
            )
            vector = self.primary_provider.embed(text)
            self._validate_dimension(vector, self.primary_provider.dimension, self.primary_provider.name)
            latency_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "embedding_success",
                purpose=purpose,
                provider=self.primary_provider.name,
                model=self.primary_provider.model,
                latency_ms=round(latency_ms, 2),
                fallback_used=False,
            )
            return EmbeddingArtifact(
                vector=vector,
                provider=self.primary_provider.name,
                model=self.primary_provider.model,
                dimension=self.primary_provider.dimension,
                version=getattr(self.primary_provider, "version", None),
                fallback_used=False,
                latency_ms=round(latency_ms, 2),
            )
        except Exception as exc:
            if self.strict_mode or not allow_fallback:
                logger.error(
                    "embedding_failure",
                    purpose=purpose,
                    provider=self.primary_provider.name,
                    model=self.primary_provider.model,
                    strict_mode=self.strict_mode,
                    fallback_allowed=allow_fallback,
                    error=str(exc),
                )
                raise EmbeddingError(str(exc)) from exc

            logger.warning(
                "embedding_fallback_activated",
                purpose=purpose,
                provider=self.primary_provider.name,
                fallback_provider=self.fallback_provider.name,
                model=self.primary_provider.model,
                error=str(exc),
            )
            fallback_started = time.perf_counter()
            vector = self.fallback_provider.embed(text)
            self._validate_dimension(vector, self.fallback_provider.dimension, self.fallback_provider.name)
            latency_ms = (time.perf_counter() - fallback_started) * 1000
            logger.info(
                "embedding_success",
                purpose=purpose,
                provider=self.fallback_provider.name,
                model=self.fallback_provider.model,
                latency_ms=round(latency_ms, 2),
                fallback_used=True,
            )
            return EmbeddingArtifact(
                vector=vector,
                provider=self.fallback_provider.name,
                model=self.fallback_provider.model,
                dimension=self.fallback_provider.dimension,
                version=getattr(self.fallback_provider, "version", None),
                fallback_used=True,
                latency_ms=round(latency_ms, 2),
            )

    @staticmethod
    def _validate_dimension(vector: list[float], expected: int, provider_name: str) -> None:
        if len(vector) != expected:
            raise EmbeddingError(
                f"{provider_name} returned embedding dimension {len(vector)} but {expected} was expected"
            )