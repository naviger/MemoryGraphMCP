from __future__ import annotations

from fastapi.responses import JSONResponse

from app.auth.jwt import JWTVerifier
from app.domain.exceptions import AuthenticationError


class BearerTokenMiddleware:
    def __init__(
        self,
        app,
        verifier: JWTVerifier,
        protected_prefix: str = "/mcp",
        exempt_paths: set[str] | None = None,
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.protected_prefix = protected_prefix.rstrip("/") or "/"
        self.exempt_paths = exempt_paths or {"/healthz", "/readyz", "/docs", "/openapi.json", "/redoc"}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith(self.protected_prefix) or path in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin1"): value.decode("latin1") for key, value in scope.get("headers", [])}
        authorization = headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            await JSONResponse({"detail": "Missing bearer token"}, status_code=401)(scope, receive, send)
            return

        token = authorization.split(" ", 1)[1].strip()
        try:
            claims = self.verifier.verify(token)
        except AuthenticationError as exc:
            await JSONResponse({"detail": str(exc)}, status_code=401)(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["auth_claims"] = claims.model_dump(mode="json")
        await self.app(scope, receive, send)
