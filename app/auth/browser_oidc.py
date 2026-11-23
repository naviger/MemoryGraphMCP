from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from time import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

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


@dataclass(frozen=True)
class BrowserSession:
    claims: dict
    id_token: str
    end_session_endpoint: str | None
    expires: float


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

    def require_origin(self, origin: str | None) -> None:
        if not self.enabled or origin != self.origin:
            raise HTTPException(403, "The request must come from the configured UI origin")

    def claims(self, session_id: str | None) -> dict | None:
        session = self.sessions.get(session_id)
        if session and session.expires > time():
            return session.claims
        if session:
            self.sessions.pop(session_id, None)
        return None

    def _cleanup(self) -> None:
        now = time()
        self.flows = {key: value for key, value in self.flows.items() if value.expires > now}
        self.sessions = {key: value for key, value in self.sessions.items() if value.expires > now}

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
                    secret = self.settings.oidc_browser_client_secret
                    data = {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": f"{self.origin}/api/auth/callback",
                        "code_verifier": flow.verifier,
                        "client_id": self.client_id,
                    }
                    auth = None
                    if secret:
                        if self.settings.oidc_browser_token_auth == "client_secret_post":
                            data["client_secret"] = secret.get_secret_value()
                        else:
                            auth = (self.client_id, secret.get_secret_value())
                            data.pop("client_id")
                    result = await client.post(
                        self.settings.oidc_browser_token_endpoint or metadata["token_endpoint"],
                        data=data,
                        auth=auth,
                    )
                    result.raise_for_status()
                    tokens = result.json()
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
                expires = min(access.expires_at, identity.expires_at, time() + 3600)
            except (httpx.HTTPError, AuthenticationError, KeyError, TypeError, ValueError):
                response.headers["Location"] = with_query(target, auth_error="signin")
                return response

            self._cleanup()
            if len(self.sessions) >= 4096:
                raise HTTPException(503, "Too many browser sessions")
            self.sessions.pop(request.cookies.get(SESSION_COOKIE), None)
            session_id = token_urlsafe(32)
            self.sessions[session_id] = BrowserSession(
                {
                    **access.model_dump(mode="json"),
                    "display_name": profile_name(identity.raw) or profile_name(access.raw),
                },
                id_token,
                metadata.get("end_session_endpoint"),
                expires,
            )
            self._cookie(response, SESSION_COOKIE, session_id, max(0, int(expires - time())))
            return response

        @router.post("/logout")
        async def logout(request: Request, response: Response) -> dict[str, str]:
            self.require_origin(request.headers.get("origin"))
            session = self.sessions.pop(request.cookies.get(SESSION_COOKIE), None)
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
