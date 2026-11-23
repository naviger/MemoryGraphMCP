from __future__ import annotations

from collections.abc import Mapping

WRITER_REQUIRED = "An authenticated writer or admin is required"


def access_role(claims: Mapping) -> str:
    raw = claims.get("raw", {})
    realm_access = raw.get("realm_access")
    realm_roles = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
    roles = [
        role
        for group in (raw.get("roles"), realm_roles)
        if isinstance(group, list)
        for role in group
    ]
    if "admin" in roles:
        return "admin"
    if "writer" in roles:
        return "writer"
    return "reader"


def writer_subject(claims: Mapping) -> str | None:
    subject = claims.get("subject")
    if isinstance(subject, str) and subject and access_role(claims) in {"writer", "admin"}:
        return subject
    return None
