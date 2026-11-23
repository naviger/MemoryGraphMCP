"""Project grants derived exclusively from verified access-token claims."""
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field

from pydantic import TypeAdapter, ValidationError

from app.auth.permissions import access_role
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import MemoryKey


class ProjectAccessDenied(ArchitectureGraphError):
    def __init__(self):
        super().__init__("Project access is required")


@dataclass
class ProjectAccess:
    unrestricted: bool = False
    grants: dict[str, str] = field(default_factory=dict)
    cache: dict = field(default_factory=dict)

    def require(self, project: str, *, write: bool = False) -> None:
        if not self.unrestricted and (
            project not in self.grants or write and self.grants[project] != "write"
        ):
            raise ProjectAccessDenied()


current_project_access: ContextVar[ProjectAccess | None] = ContextVar("project_access", default=None)
_key = TypeAdapter(MemoryKey)


def access_for_claims(claims: Mapping, claim_name: str) -> ProjectAccess:
    if not claims.get("subject"):
        return ProjectAccess()
    role = access_role(claims)
    if role == "admin":
        return ProjectAccess(unrestricted=True)
    values = claims.get("raw", {}).get(claim_name)
    if not isinstance(values, dict) or len(values) > 100:
        return ProjectAccess()
    grants = {}
    for project, permission in values.items():
        if permission not in ("read", "write"):
            continue
        try:
            project = _key.validate_python(project)
        except ValidationError:
            continue
        grants[project] = "write" if permission == "write" and role == "writer" else "read"
    return ProjectAccess(grants=grants)


async def serve_with_project_access(app, scope, receive, send, claim_name: str):
    claims = scope.get("state", {}).get("auth_claims", {})
    access = current_project_access.set(access_for_claims(claims, claim_name))
    try:
        await app(scope, receive, send)
    finally:
        current_project_access.reset(access)
