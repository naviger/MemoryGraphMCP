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


def test_activity_refreshes_rotating_tokens_without_extending_absolute_session(monkeypatch):
    import anyio

    from app.auth import browser_oidc
    from app.auth.browser_oidc import BrowserSession
    from app.auth.jwt import JWTClaims

    clock = [1000.0]
    monkeypatch.setattr(browser_oidc, "time", lambda: clock[0])
    settings = Settings(_env_file=None, auth_required=False, oidc_browser_idle_seconds=120)
    verifier = JWTVerifier(settings)
    oidc = BrowserOIDC(settings, verifier)
    exchanges = []

    async def exchange(client, endpoint, form):
        exchanges.append(form["refresh_token"])
        await anyio.sleep(0)
        return {"access_token": "updated", "refresh_token": "rotation"}

    monkeypatch.setattr(oidc, "_exchange", exchange)
    monkeypatch.setattr(
        JWTVerifier,
        "verify",
        lambda self, token: JWTClaims(
            sub="writer",
            raw={"roles": ["reader"]},
            exp=int(clock[0]) + 100,
        ),
    )

    async def exercise():
        oidc.sessions["session"] = BrowserSession(
            claims={"subject": "writer", "display_name": "Writer", "raw": {"roles": ["writer"]}},
            id_token="identity",
            end_session_endpoint=None,
            access_expires=1040,
            absolute_expires=1280,
            last_activity=1000,
            token_endpoint="https://issuer.example/token",
            refresh_token="initial",
        )
        clock[0] = 1041
        results = []

        async def request():
            results.append(await oidc.claims("session", active=True))

        async with anyio.create_task_group() as requests:
            requests.start_soon(request)
            requests.start_soon(request)
        assert len(results) == 2 and exchanges == ["initial"]
        assert all(result["raw"]["roles"] == ["reader"] for result in results)
        assert oidc.sessions["session"].refresh_token == "rotation"
        clock[0] = 1120
        assert await oidc.claims("session") is not None
        assert exchanges == ["initial", "rotation"]
        assert oidc.sessions["session"].last_activity == 1041
        clock[0] = 1162
        assert await oidc.claims("session", active=True) is None

        oidc.sessions["maxed"] = BrowserSession(
            claims={},
            id_token="identity",
            end_session_endpoint=None,
            access_expires=1400,
            absolute_expires=1280,
            last_activity=1279,
            token_endpoint="https://issuer.example/token",
            refresh_token="initial",
        )
        clock[0] = 1280
        assert await oidc.claims("maxed", active=True) is None

    anyio.run(exercise)
