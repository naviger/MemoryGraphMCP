class MemoryGraphError(Exception):
    """Base exception for MemoryGraphMCP domain and application failures."""


class AuthenticationError(MemoryGraphError):
    """Raised when bearer token validation fails."""


class RepositoryError(MemoryGraphError):
    """Raised when persistence operations fail."""


class ArchitectureGraphError(MemoryGraphError):
    """Raised when a code index or architecture graph operation is invalid."""
