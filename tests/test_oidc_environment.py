from __future__ import annotations

from app.config.settings import Settings


def test_keycloak_env_aliases_are_loaded_from_dotenv_values() -> None:
    settings = Settings(
        auth_required=True,
        keycloak_url="http://localhost:8080",
        keycloak_realm="naviger",
        keycloak_client_id="memorygraphmcp",
        oidc_issuer="http://localhost:8080/realms/naviger",
        oidc_audience="memorygraphmcp",
    )

    assert settings.keycloak_realm == "naviger"
    assert settings.keycloak_issuer == "http://localhost:8080/realms/naviger"
    assert settings.effective_jwt_audience == "memorygraphmcp"


def test_keycloak_client_id_is_used_when_no_explicit_audience_is_set() -> None:
    settings = Settings(
        _env_file=None,
        auth_required=True,
        keycloak_url="http://localhost:8080",
        keycloak_realm="naviger",
        keycloak_client_id="memorygraphmcp",
        oidc_issuer="http://localhost:8080/realms/naviger",
    )

    assert settings.effective_jwt_audience == "memorygraphmcp"
