from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MGMCP_", env_file=".env", extra="ignore")

    app_name: str = "MemoryGraphMCP"
    app_version: str = "0.1.0"
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")

    http_host: str = Field(default="0.0.0.0")
    http_port: int = Field(default=8000, ge=1, le=65535)
    mcp_mount_path: str = Field(default="/mcp")

    auth_required: bool = True
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_secret_key: str | None = None
    jwt_public_key: str | None = None
    jwt_algorithm: str = Field(default="HS256")

    arango_url: str = Field(
        default="http://localhost:8529",
        validation_alias=AliasChoices("ARANGO_HOST", "MGMCP_ARANGO_URL"),
    )
    arango_database: str = Field(
        default="memorygraph",
        validation_alias=AliasChoices("ARANGO_DATABASE", "MGMCP_ARANGO_DATABASE"),
    )
    arango_username: str = Field(
        default="root",
        validation_alias=AliasChoices("ARANGO_USERNAME", "MGMCP_ARANGO_USERNAME"),
    )
    arango_password: str = Field(
        default="",
        validation_alias=AliasChoices("ARANGO_PASSWORD", "MGMCP_ARANGO_PASSWORD"),
    )
    arango_root_password: str = Field(default="")
    arango_architecture_entities_collection: str = "architecture_entities"
    arango_architecture_edges_collection: str = "architecture_edges"
    arango_documents_collection: str = Field(default="memory_documents")
    arango_edges_collection: str = Field(default="memory_edges")
    arango_vectors_collection: str = Field(default="memory_vectors")
    arango_search_view: str = Field(default="memory_search")
    arango_vector_view: str = Field(default="memory_vectors_view")

    search_result_limit: int = Field(default=10, ge=1, le=100)
    vector_dimensions: int = Field(default=1536, ge=1)

    otel_service_name: str = Field(default="MemoryGraphMCP")
    otel_exporter_otlp_endpoint: str | None = None

    @field_validator("mcp_mount_path")
    @classmethod
    def normalize_mcp_mount_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            path = f"/{path}"
        return path.rstrip("/") or "/"

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        if self.auth_required and not (self.jwt_secret_key or self.jwt_public_key):
            raise ValueError("JWT authentication is enabled but no signing key was configured")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
