from __future__ import annotations

import pytest

jwt = pytest.importorskip("jwt")

from app.auth.jwt import JWTVerifier
from app.config.settings import Settings


def test_verify_hs256_token() -> None:
    settings = Settings(auth_required=False, jwt_secret_key="super-secret", jwt_algorithm="HS256")
    token = jwt.encode({"sub": "agent-1", "aud": "MemoryGraphMCP"}, "super-secret", algorithm="HS256")

    claims = JWTVerifier(settings).verify(token)

    assert claims.subject == "agent-1"
    assert claims.audience == ["MemoryGraphMCP"]
