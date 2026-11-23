from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any

import jwt
from jwt import PyJWTError
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config.settings import Settings
from app.domain.exceptions import AuthenticationError


def profile_name(claims: Mapping[str, Any]) -> str | None:
    def text(key: str) -> str:
        value = claims.get(key)
        return value.strip() if isinstance(value, str) else ""

    full_name = " ".join(filter(None, (text("given_name"), text("family_name"))))
    return text("name") or full_name or text("preferred_username") or None


class JWTClaims(BaseModel):
    subject: str = Field(alias="sub")
    issuer: str | None = Field(default=None, alias="iss")
    audience: list[str] = Field(default_factory=list, alias="aud")
    scopes: list[str] = Field(default_factory=list, alias="scp")
    issued_at: int | None = Field(default=None, alias="iat")
    expires_at: int | None = Field(default=None, alias="exp")
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("audience", "scopes", mode="before")
    @classmethod
    def coerce_string_or_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ValueError("Expected a string or list of strings")

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "JWTClaims":
        model = cls.model_validate(payload)
        model.raw = payload
        return model


@dataclass(frozen=True)
class JWTVerifier:
    settings: Settings

    @cached_property
    def jwks_client(self) -> jwt.PyJWKClient | None:
        uri = self.settings.keycloak_jwks_uri
        return jwt.PyJWKClient(uri) if uri else None

    def verify(self, token: str) -> JWTClaims:
        key = self.settings.jwt_secret_key or self.settings.jwt_public_key
        algorithms = [self.settings.jwt_algorithm]
        issuer = self.settings.jwt_issuer
        audience = self.settings.jwt_audience
        required = ["sub"]
        try:
            if not key:
                issuer = self.settings.effective_jwt_issuer
                audience = self.settings.effective_jwt_audience
                if not (issuer and audience and self.jwks_client):
                    raise AuthenticationError("JWT signing key is not configured")
                key = self.jwks_client.get_signing_key_from_jwt(token).key
                algorithms = ["RS256"]
                required += ["exp", "iss", "aud"]

            payload = jwt.decode(
                token,
                key=key,
                algorithms=algorithms,
                audience=audience,
                issuer=issuer,
                options={"require": required, "verify_aud": True, "verify_iss": bool(issuer)},
            )
            role_claim = payload.get(self.settings.oidc_role_claim)
            if self.settings.oidc_role_claim != "roles" and isinstance(role_claim, list):
                payload["roles"] = role_claim
            return JWTClaims.from_payload(payload)
        except (PyJWTError, ValidationError) as exc:
            raise AuthenticationError("Invalid bearer token") from exc

    def verify_id_token(self, token: str, *, client_id: str, nonce: str) -> JWTClaims:
        from secrets import compare_digest

        if self.jwks_client is None:
            raise AuthenticationError("OIDC signing keys are unavailable")
        try:
            key = self.jwks_client.get_signing_key_from_jwt(token).key
            payload = jwt.decode(
                token, key, algorithms=["RS256"],
                issuer=self.settings.effective_jwt_issuer, audience=client_id,
                options={"require": ["sub", "exp", "iss", "aud", "iat", "nonce"]},
            )
            token_nonce = payload.get("nonce")
            if not isinstance(token_nonce, str) or not compare_digest(
                token_nonce.encode(), nonce.encode(),
            ):
                raise AuthenticationError("Invalid OIDC nonce")
            claims = JWTClaims.from_payload(payload)
            azp = payload.get("azp")
            if (azp is not None or len(claims.audience) > 1) and azp != client_id:
                raise AuthenticationError("Invalid OIDC authorized party")
            return claims
        except (PyJWTError, ValidationError) as exc:
            raise AuthenticationError("Invalid ID token") from exc
