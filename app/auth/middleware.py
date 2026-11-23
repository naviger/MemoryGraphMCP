from __future__ import annotations

import anyio
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.auth.browser_oidc import SESSION_COOKIE, BrowserOIDC
from app.auth.jwt import JWTVerifier
from app.domain.exceptions import AuthenticationError


class BearerTokenMiddleware:
    def __init__(
        self,
        app,
        verifier: JWTVerifier,
        protected_prefix: str = "/mcp",
        exempt_paths: set[str] | None = None,
        additional_prefixes: tuple[str, ...] = (),
        browser: BrowserOIDC | None = None,
        browser_prefix: str = "/api/review",
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.protected_prefix = protected_prefix.rstrip("/") or "/"
        self._protected_prefixes = (
            self.protected_prefix, *(prefix.rstrip("/") or "/" for prefix in additional_prefixes),
        )
        self.exempt_paths = exempt_paths or {"/healthz", "/readyz", "/docs", "/openapi.json", "/redoc"}
        self.browser = browser
        self.browser_prefix = browser_prefix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        protected = any(
            prefix == "/" or path == prefix or path.startswith(f"{prefix}/")
            for prefix in self._protected_prefixes
        )
        if not protected or path in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin1"): value.decode("latin1") for key, value in scope.get("headers", [])}
        authorization = headers.get("authorization", "")
        if not authorization and self.browser and (
            path == self.browser_prefix or path.startswith(f"{self.browser_prefix}/")
        ):
            claims = self.browser.claims(Request(scope).cookies.get(SESSION_COOKIE))
            if claims:
                if scope["method"] not in {"GET", "HEAD", "OPTIONS"} and (
                    headers.get("origin") != self.browser.origin
                ):
                    await JSONResponse({"detail": "Invalid request origin"}, status_code=403)(
                        scope, receive, send,
                    )
                    return
                scope.setdefault("state", {}).update(auth_claims=claims, browser_session=True)
                await self.app(scope, receive, send)
                return
        if not authorization.lower().startswith("bearer "):
            await JSONResponse({"detail": "Missing bearer token"}, status_code=401)(scope, receive, send)
            return

        token = authorization.split(" ", 1)[1].strip()
        try:
            claims = await anyio.to_thread.run_sync(self.verifier.verify, token)
        except AuthenticationError as exc:
            await JSONResponse({"detail": str(exc)}, status_code=401)(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["auth_claims"] = claims.model_dump(mode="json")
        await self.app(scope, receive, send)
