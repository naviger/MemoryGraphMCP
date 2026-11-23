from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.browser_oidc import BrowserOIDC
from app.auth.jwt import JWTVerifier
from app.config.settings import Settings


def test_browser_login_requires_s256_and_consumes_state_once(monkeypatch):
    origin = "http://127.0.0.1:3200"
    issuer = "http://127.0.0.1:8180/realms/test"
    settings = Settings(
        _env_file=None,
        auth_required=True,
        oidc_issuer=issuer,
        oidc_audience="api",
        oidc_jwks_uri=issuer + "/jwks",
        oidc_browser_client_id="browser",
        oidc_browser_origin=origin,
    )
    oidc = BrowserOIDC(settings, JWTVerifier(settings))

    async def discover(client):
        return {"authorization_endpoint": issuer + "/authorize"}

    monkeypatch.setattr(oidc, "_discovery", discover)
    app = FastAPI()
    app.include_router(oidc.router())
    with TestClient(app, base_url=origin, follow_redirects=False) as client:
        start = client.get("/api/auth/login", params={"return_to": "//example.invalid"})
        assert start.status_code == 303
        params = parse_qs(urlsplit(start.headers["Location"]).query)
        assert params["client_id"] == ["browser"]
        assert params["scope"] == ["openid profile"]
        assert params["code_challenge_method"] == ["S256"]
        assert len(params["code_challenge"][0]) == 43
        assert len(params["nonce"][0]) >= 32
        assert "HttpOnly" in start.headers["Set-Cookie"]
        assert client.get("/api/auth/callback?state=bogus&error=access_denied").status_code == 400
        callback = {
            "state": params["state"][0],
            "error": "access_denied",
        }
        denied = client.get("/api/auth/callback", params=callback)
        assert denied.headers["Location"] == f"{origin}/?auth_error=signin"
        assert client.get("/api/auth/callback", params=callback).status_code == 400
        assert client.post("/api/auth/logout").status_code == 403
