from __future__ import annotations

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request

from app.auth.permissions import WRITER_REQUIRED, writer_subject


def require_writer() -> str:
    """Return the verified subject of an authenticated writer or admin."""
    try:
        claims = get_http_request().scope.get("state", {}).get("auth_claims", {})
    except RuntimeError as exc:
        raise ToolError(WRITER_REQUIRED) from exc

    subject = writer_subject(claims)
    if subject is None:
        raise ToolError(WRITER_REQUIRED)
    return subject
