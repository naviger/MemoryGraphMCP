from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWTError
from pydantic import BaseModel, Field, field_validator

from app.domain.exceptions import AuthenticationError
from app.config.settings import Settings


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
        return list(value)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "JWTClaims":
        model = cls.model_validate(payload)
        model.raw = payload
        return model


@dataclass(frozen=True)
class JWTVerifier:
    settings: Settings

    def verify(self, token: str) -> JWTClaims:
        algorithms = [self.settings.jwt_algorithm]
        key = self._signing_key()
        options = {"require": ["sub"]}

        try:
            payload = jwt.decode(
                token,
                key=key,
                algorithms=algorithms,
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                options=options,
            )
        except PyJWTError as exc:
            raise AuthenticationError("Invalid bearer token") from exc

        return JWTClaims.from_payload(payload)

    def _signing_key(self) -> str:
        if self.settings.jwt_secret_key:
            return self.settings.jwt_secret_key
        if self.settings.jwt_public_key:
            return self.settings.jwt_public_key
        raise AuthenticationError("JWT signing key is not configured")
