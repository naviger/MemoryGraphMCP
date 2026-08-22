class MemoryGraphError(Exception):
    """Base exception for MemoryGraphMCP domain and application failures."""


class AuthenticationError(MemoryGraphError):
    """Raised when bearer token validation fails."""


class RepositoryError(MemoryGraphError):
    """Raised when persistence operations fail."""
