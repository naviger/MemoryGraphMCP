from __future__ import annotations

import pytest

jwt = pytest.importorskip("jwt")

from app.auth.jwt import JWTVerifier
from app.config.settings import Settings


def test_verify_hs256_token() -> None:
    settings = Settings(
        _env_file=None, auth_required=False, jwt_secret_key="super-secret-key-for-testing-only",
        jwt_algorithm="HS256", jwt_audience="MemoryGraphMCP",
    )
    token = jwt.encode({"sub": "agent-1", "aud": "MemoryGraphMCP"}, "super-secret-key-for-testing-only", algorithm="HS256")

    claims = JWTVerifier(settings).verify(token)

    assert claims.subject == "agent-1"
    assert claims.audience == ["MemoryGraphMCP"]
