import pytest
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


@pytest.mark.parametrize("logout_replica", [0, 1])
def test_warm_browser_replicas_share_refresh_and_logout(tmp_path, monkeypatch, logout_replica):
    import anyio
    from cryptography.fernet import Fernet

    from app.auth import browser_oidc
    from app.auth.browser_oidc import BrowserSession
    from app.auth.jwt import JWTClaims

    now = 1000.0
    monkeypatch.setattr(browser_oidc, "time", lambda: now)
    issuer = "http://127.0.0.1:8180/realms/test"
    path = tmp_path / "sessions.sqlite"
    settings = Settings(
        _env_file=None,
        auth_required=True,
        oidc_issuer=issuer,
        oidc_audience="api",
        oidc_jwks_uri=issuer + "/jwks",
        oidc_browser_client_id="browser",
        oidc_browser_origin="http://127.0.0.1:3200",
        oidc_browser_session_db=str(path),
        oidc_browser_session_key=Fernet.generate_key().decode(),
        oidc_browser_idle_seconds=120,
    )

    def new_browser():
        return BrowserOIDC(settings, JWTVerifier(settings))

    async def exercise():
        nonlocal now
        first = new_browser()
        await first.persist(
            "opaque-cookie",
            BrowserSession(
                claims={"subject": "writer", "display_name": "Writer", "raw": {}},
                id_token="secret-id-token",
                end_session_endpoint=None,
                access_expires=1140,
                absolute_expires=2000,
                last_activity=now,
                token_endpoint=issuer + "/token",
                refresh_token="secret-refresh-token",
            ),
            now,
        )
        stored = path.read_bytes()
        assert all(
            value not in stored
            for value in (
                b"secret-refresh-token",
                b"secret-id-token",
                b"opaque-cookie",
            )
        )
        second = new_browser()
        replicas = [first, second]
        for replica in replicas:
            assert (await replica.claims("opaque-cookie", active=True))["subject"] == "writer"

        now = 1115.0
        exchanges = []

        async def exchange(self, client, endpoint, form):
            exchanges.append(form["refresh_token"])
            await anyio.sleep(0.05)
            return {"access_token": "updated", "refresh_token": "rotated"}

        monkeypatch.setattr(BrowserOIDC, "_exchange", exchange)
        monkeypatch.setattr(JWTVerifier, "verify", lambda self, token: JWTClaims(
            sub="writer", raw={"roles": ["reader"]}, exp=1300,
        ))
        results = []

        async def refresh(replica):
            results.append(await replica.claims("opaque-cookie", active=True))

        async with anyio.create_task_group() as group:
            for replica in replicas:
                group.start_soon(refresh, replica)
        assert exchanges == ["secret-refresh-token"]
        assert all(result["raw"]["roles"] == ["reader"] for result in results)

        wrong_key = settings.model_copy(
            update={
                "oidc_browser_session_key": type(settings.oidc_browser_session_key)(
                    Fernet.generate_key().decode()
                ),
            }
        )
        assert await BrowserOIDC(wrong_key, JWTVerifier(wrong_key)).claims("opaque-cookie") is None
        assert await replicas[logout_replica].end_session("opaque-cookie") is not None
        for replica in replicas:
            assert await replica.claims("opaque-cookie", active=True) is None
        assert await new_browser().claims("opaque-cookie") is None

    anyio.run(exercise)
