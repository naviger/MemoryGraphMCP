class MemoryGraphError(Exception):
    """Base exception for MemoryGraphMCP domain and application failures."""


class AuthenticationError(MemoryGraphError):
    """Raised when bearer token validation fails."""


class RepositoryError(MemoryGraphError):
    """Raised when persistence operations fail."""


class EmbeddingError(MemoryGraphError):
    """Raised when embedding generation or validation fails."""


class EmbeddingServiceUnavailable(EmbeddingError):
    """Raised when the embedding service is unreachable or unsuitable for use."""


class EmbeddingModelMissing(EmbeddingError):
    """Raised when the configured embedding model is not available."""


class EmbeddingCapabilityDisabled(EmbeddingError):
    """Raised when embedding capability is intentionally disabled."""


class ADRError(MemoryGraphError):
    """Raised when an ADR operation violates a lifecycle or relationship rule."""


class RevalidationError(MemoryGraphError):
    """Raised when a memory record cannot be revalidated."""


class ArchitectureGraphError(MemoryGraphError):
    """Raised when a code index or architecture graph operation is invalid."""
