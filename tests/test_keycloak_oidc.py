from __future__ import annotations

from pathlib import Path

from app.auth.jwt import JWTVerifier
from app.config.settings import Settings


def test_settings_expose_keycloak_oidc_values() -> None:
    settings = Settings(
        _env_file=None,
        auth_required=True,
        keycloak_url="http://localhost:8080",
        keycloak_realm="naviger",
        keycloak_client_id="memorygraphmcp",
        oidc_issuer="http://localhost:8080/realms/naviger",
        oidc_audience="memorygraphmcp",
    )

    assert settings.keycloak_issuer == "http://localhost:8080/realms/naviger"
    assert settings.keycloak_jwks_uri == "http://localhost:8080/realms/naviger/protocol/openid-connect/certs"
    assert settings.effective_jwt_audience == "memorygraphmcp"


def test_jwt_verifier_supports_keycloak_oidc(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        auth_required=True,
        jwt_secret_key=None,
        jwt_public_key=None,
        keycloak_url="http://localhost:8080",
        keycloak_realm="naviger",
        keycloak_client_id="memorygraphmcp",
        oidc_issuer="http://localhost:8080/realms/naviger",
        oidc_audience="memorygraphmcp",
    )

    class FakeSigningKey:
        key = "public-key"
        algorithm = "RS256"

    class FakeJWKClient:
        def __init__(self, url: str) -> None:
            assert url == "http://localhost:8080/realms/naviger/protocol/openid-connect/certs"

        def get_signing_key_from_jwt(self, token: str) -> FakeSigningKey:
            assert token == "oidc-token"
            return FakeSigningKey()

    def fake_decode(token: str, key: str, algorithms: list[str], audience: str | None, issuer: str | None, options: dict[str, object]):
        assert token == "oidc-token"
        assert key == "public-key"
        assert algorithms == ["RS256"]
        assert audience == "memorygraphmcp"
        assert issuer == "http://localhost:8080/realms/naviger"
        assert options["verify_aud"] is True
        assert options["verify_iss"] is True
        return {"sub": "alice", "iss": issuer, "aud": audience}

    monkeypatch.setattr("app.auth.jwt.jwt.PyJWKClient", FakeJWKClient)
    monkeypatch.setattr("app.auth.jwt.jwt.decode", fake_decode)

    claims = JWTVerifier(settings).verify("oidc-token")

    assert claims.subject == "alice"
    assert claims.audience == ["memorygraphmcp"]


def test_compose_configures_keycloak_and_arangodb_from_env() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text()
    local_auth = (root / "docker-compose.local-auth.yml").read_text()

    assert "${KEYCLOAK_REALM:-" in compose
    assert "${ARANGO_DATABASE:-" in compose
    assert "${ARANGO_ROOT_PASSWORD:?" in compose
    assert "MGMCP_OIDC_BROWSER_CLIENT_ID: memorygraphmcp" in local_auth
    assert "MGMCP_OIDC_AUDIENCE: memorygraphmcp-api" in local_auth
