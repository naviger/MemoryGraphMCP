from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from sqlite3 import Error as SQLiteError
from time import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.auth.browser_session_store import BrowserSessionStore
from app.auth.jwt import JWTVerifier, profile_name
from app.config.settings import Settings
from app.domain.exceptions import AuthenticationError

SESSION_COOKIE = "mgmcp_session"
_FLOW_COOKIE = "mgmcp_oidc_flow"


@dataclass(frozen=True)
class LoginFlow:
    verifier: str
    nonce: str
    return_to: str
    expires: float


@dataclass
class BrowserSession:
    claims: dict
    id_token: str
    end_session_endpoint: str | None
    access_expires: float
    absolute_expires: float
    last_activity: float
    token_endpoint: str
    refresh_token: str | None = field(repr=False)
    lock: anyio.Lock = field(default_factory=anyio.Lock, repr=False)
    next_refresh_at: float = 0


def safe_return_path(value: str) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return "/"
    return value


def with_query(url: str, **values: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    return urlunsplit(parts._replace(query=urlencode(query | values)))


class BrowserOIDC:
    def __init__(self, settings: Settings, verifier: JWTVerifier) -> None:
        self.settings = settings
        self.verifier = verifier
        self.origin = settings.oidc_browser_origin.rstrip("/")
        self.client_id = settings.oidc_browser_client_id
        self.enabled = settings.auth_required and bool(self.origin and self.client_id)
        self.secure = self.origin.startswith("https://")
        self.flows: dict[str, LoginFlow] = {}
        self.sessions: dict[str, BrowserSession] = {}
        self.store = None
        if self.enabled and settings.oidc_browser_session_db:
            key = settings.oidc_browser_session_key
            if not key or not key.get_secret_value():
                raise ValueError(
                    "A session encryption key is required for persistent browser sessions"
                )
            self.store = BrowserSessionStore(
                settings.oidc_browser_session_db,
                key.get_secret_value(),
                f"{settings.effective_jwt_issuer}:{self.client_id}:{self.origin}",
            )

    async def storage(self, operation, *args):
        try:
            return await anyio.to_thread.run_sync(partial(operation, *args))
        except SQLiteError as exc:
            raise HTTPException(503, "Browser session storage is temporarily unavailable") from exc

    @asynccontextmanager
    async def session_guard(self, session_id: str):
        if self.store is None:
            yield
            return
        try:
            async with self.store.guard(session_id):
                yield
        except (OSError, TimeoutError) as exc:
            raise HTTPException(503, "Browser session coordination is temporarily unavailable") from exc

    async def get_session(self, session_id: str) -> BrowserSession | None:
        if self.store is None:
            return self.sessions.get(session_id)
        data = await self.storage(self.store.read, session_id, time())
        if data is None:
            self.sessions.pop(session_id, None)
            return None
        session = BrowserSession(**data)
        self.sessions[session_id] = session
        return session

    async def persist(self, session_id, session, now):
        if self.store is None:
            return
        values = {name: value for name, value in vars(session).items() if name != "lock"}
        deadline = min(
            session.absolute_expires,
            session.last_activity + self.settings.oidc_browser_idle_seconds,
        )
        if not session.refresh_token:
            deadline = min(deadline, session.access_expires)
        await self.storage(self.store.write, session_id, values, deadline, now)

    async def discard(self, session_id):
        if session_id is None:
            return
        if self.store:
            await self.storage(self.store.delete, session_id)
        self.sessions.pop(session_id, None)

    async def end_session(self, session_id):
        if session_id is None:
            return None
        async with self.session_guard(session_id):
            session = await self.get_session(session_id)
            if session:
                async with session.lock:
                    await self.discard(session_id)
            return session

    def require_origin(self, origin: str | None) -> None:
        if not self.enabled or origin != self.origin:
            raise HTTPException(403, "The request must come from the configured UI origin")

    def session_alive(self, session: BrowserSession, now: float) -> bool:
        return (
            now < session.absolute_expires
            and now - session.last_activity < self.settings.oidc_browser_idle_seconds
            and (session.refresh_token is not None or now < session.access_expires)
        )

    async def claims(self, session_id: str | None, *, active: bool = False) -> dict | None:
        if session_id is None:
            return None
        async with self.session_guard(session_id):
            return await self._claims(session_id, active=active)

    async def _claims(self, session_id: str, *, active: bool) -> dict | None:
        session = await self.get_session(session_id)
        if session is None:
            return None
        async with session.lock:
            now = time()
            if self.sessions.get(session_id) is not session or not self.session_alive(session, now):
                await self.discard(session_id)
                return None
            changed = False
            if (
                session.refresh_token
                and session.access_expires - now <= 30
                and now >= session.next_refresh_at
            ):
                try:
                    async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                        tokens = await self._exchange(
                            client,
                            session.token_endpoint,
                            {
                                "grant_type": "refresh_token",
                                "refresh_token": session.refresh_token,
                            },
                        )
                    access = await anyio.to_thread.run_sync(
                        self.verifier.verify,
                        tokens["access_token"],
                    )
                    if access.subject != session.claims["subject"] or not access.expires_at:
                        raise AuthenticationError("Refreshed token identity or expiration changed")
                    replacement = tokens.get("refresh_token")
                    if replacement is not None and (
                        not isinstance(replacement, str) or not replacement
                    ):
                        raise AuthenticationError("Refreshed token is invalid")
                    session.refresh_token = replacement or session.refresh_token
                    session.access_expires = access.expires_at
                    session.claims = {
                        **access.model_dump(mode="json"),
                        "display_name": profile_name(access.raw) or session.claims["display_name"],
                    }
                    session.next_refresh_at = 0
                    changed = True
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        await self.discard(session_id)
                        return None
                    session.next_refresh_at = time() + 5
                    changed = True
                except (AuthenticationError, KeyError, TypeError, ValueError):
                    await self.discard(session_id)
                    return None
            now = time()
            if self.sessions.get(session_id) is not session or not self.session_alive(session, now):
                return None
            if now >= session.access_expires:
                if changed:
                    await self.persist(session_id, session, now)
                raise HTTPException(503, "Session refresh is temporarily unavailable")
            if active:
                session.last_activity = now
            if active or changed:
                await self.persist(session_id, session, now)
            return session.claims

    async def _exchange(self, client, endpoint, form):
        data = {**form, "client_id": self.client_id}
        secret = self.settings.oidc_browser_client_secret
        auth = None
        if secret:
            if self.settings.oidc_browser_token_auth == "client_secret_post":
                data["client_secret"] = secret.get_secret_value()
            else:
                auth = (self.client_id, secret.get_secret_value())
                data.pop("client_id")
        result = await client.post(endpoint, data=data, auth=auth)
        result.raise_for_status()
        payload = result.json()
        if not isinstance(payload, dict):
            raise AuthenticationError("Invalid token response")
        return payload

    def _cleanup(self) -> None:
        now = time()
        self.flows = {key: value for key, value in self.flows.items() if value.expires > now}
        self.sessions = {
            key: value for key, value in self.sessions.items() if self.session_alive(value, now)
        }

    def _cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )

    async def _discovery(self, client: httpx.AsyncClient) -> dict:
        issuer = self.settings.effective_jwt_issuer
        url = (
            self.settings.oidc_browser_discovery_url or f"{issuer}/.well-known/openid-configuration"
        )
        response = await client.get(url)
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("issuer") != issuer:
            raise AuthenticationError("OIDC discovery issuer mismatch")
        for name in ("authorization_endpoint", "token_endpoint", "end_session_endpoint"):
            value = metadata.get(name)
            if name == "end_session_endpoint" and not value:
                continue
            if not isinstance(value, str):
                raise AuthenticationError("OIDC provider metadata is incomplete")
            parts = urlsplit(value)
            if (
                not parts.hostname
                or parts.username
                or parts.password
                or (
                    parts.scheme != "https"
                    and (
                        self.secure
                        or parts.scheme != "http"
                        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
                    )
                )
            ):
                raise AuthenticationError("OIDC provider endpoint is invalid")
        return metadata

    def router(self) -> APIRouter:
        router = APIRouter(prefix="/api/auth")

        @router.get("/config")
        async def config(response: Response) -> dict[str, bool]:
            response.headers["Cache-Control"] = "no-store"
            return {"oidc_enabled": self.enabled}

        @router.get("/login")
        async def login(return_to: str = "/") -> RedirectResponse:
            if not self.enabled:
                raise HTTPException(404, "Browser sign-in is unavailable")
            self._cleanup()
            if len(self.flows) >= 1024:
                raise HTTPException(503, "Too many pending sign-ins")
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    metadata = await self._discovery(client)
            except (httpx.HTTPError, AuthenticationError, ValueError) as exc:
                raise HTTPException(502, "The identity provider is unavailable") from exc

            state = token_urlsafe(32)
            flow = LoginFlow(
                token_urlsafe(48), token_urlsafe(32), safe_return_path(return_to), time() + 300
            )
            self.flows[state] = flow
            challenge = (
                urlsafe_b64encode(sha256(flow.verifier.encode()).digest()).rstrip(b"=").decode()
            )
            scopes = " ".join(
                dict.fromkeys(["openid", "profile", *self.settings.oidc_browser_scopes.split()])
            )
            audience = self.settings.oidc_browser_audience
            response = RedirectResponse(
                with_query(
                    metadata["authorization_endpoint"],
                    response_type="code",
                    client_id=self.client_id,
                    scope=scopes,
                    redirect_uri=f"{self.origin}/api/auth/callback",
                    state=state,
                    nonce=flow.nonce,
                    code_challenge=challenge,
                    code_challenge_method="S256",
                    **({"audience": audience} if audience else {}),
                ),
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )
            self._cookie(response, _FLOW_COOKIE, state, 300)
            return response

        @router.get("/callback")
        async def callback(
            request: Request,
            state: str = "",
            code: str = "",
            error: str = "",
        ) -> RedirectResponse:
            if not self.enabled:
                raise HTTPException(404, "Browser sign-in is unavailable")
            received = request.cookies.get(_FLOW_COOKIE, "")
            if not (state and received and compare_digest(state.encode(), received.encode())):
                raise HTTPException(400, "Invalid sign-in state")
            flow = self.flows.pop(state, None)
            if flow is None or flow.expires <= time():
                raise HTTPException(400, "Sign-in expired. Start sign-in again.")
            target = self.origin + flow.return_to
            response = RedirectResponse(
                target, status_code=303, headers={"Cache-Control": "no-store"}
            )
            response.delete_cookie(_FLOW_COOKIE, secure=self.secure, httponly=True, samesite="lax")
            if error or not code:
                response.headers["Location"] = with_query(target, auth_error="signin")
                return response
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    metadata = await self._discovery(client)
                    endpoint = (
                        self.settings.oidc_browser_token_endpoint or metadata["token_endpoint"]
                    )
                    tokens = await self._exchange(
                        client,
                        endpoint,
                        {
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": f"{self.origin}/api/auth/callback",
                            "code_verifier": flow.verifier,
                        },
                    )
                id_token = tokens["id_token"]
                identity = await anyio.to_thread.run_sync(
                    lambda: self.verifier.verify_id_token(
                        id_token,
                        client_id=self.client_id,
                        nonce=flow.nonce,
                    )
                )
                access = await anyio.to_thread.run_sync(
                    self.verifier.verify, tokens["access_token"]
                )
                if (
                    identity.subject != access.subject
                    or not access.expires_at
                    or not identity.expires_at
                ):
                    raise AuthenticationError("OIDC token subjects or lifetime are invalid")
                now = time()
                refresh = tokens.get("refresh_token")
                if not isinstance(refresh, str) or not refresh:
                    refresh = None
                expires = now + self.settings.oidc_browser_max_seconds
                if not refresh:
                    expires = min(expires, access.expires_at, identity.expires_at)
            except (httpx.HTTPError, AuthenticationError, KeyError, TypeError, ValueError):
                response.headers["Location"] = with_query(target, auth_error="signin")
                return response

            self._cleanup()
            if len(self.sessions) >= 4096:
                raise HTTPException(503, "Too many browser sessions")
            await self.end_session(request.cookies.get(SESSION_COOKIE))
            session_id = token_urlsafe(32)
            session = BrowserSession(
                {
                    **access.model_dump(mode="json"),
                    "display_name": profile_name(identity.raw) or profile_name(access.raw),
                },
                id_token,
                metadata.get("end_session_endpoint"),
                access_expires=access.expires_at,
                absolute_expires=expires,
                last_activity=now,
                token_endpoint=endpoint,
                refresh_token=refresh,
            )
            await self.persist(session_id, session, now)
            self.sessions[session_id] = session
            self._cookie(response, SESSION_COOKIE, session_id, max(0, int(expires - time())))
            return response

        @router.post("/logout")
        async def logout(request: Request, response: Response) -> dict[str, str]:
            self.require_origin(request.headers.get("origin"))
            session = await self.end_session(request.cookies.get(SESSION_COOKIE))
            response.delete_cookie(
                SESSION_COOKIE, secure=self.secure, httponly=True, samesite="lax"
            )
            response.headers["Cache-Control"] = "no-store"
            destination = f"{self.origin}/"
            if session and session.end_session_endpoint:
                destination = with_query(
                    session.end_session_endpoint,
                    id_token_hint=session.id_token,
                    post_logout_redirect_uri=destination,
                    client_id=self.client_id,
                )
            return {"logout_url": destination}

        return router
