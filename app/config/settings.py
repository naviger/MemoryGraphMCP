from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.models import MemoryKey


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
    project_authorization_required: bool = False
    project_grants_claim: str = Field(default="mgmcp_projects", min_length=1)
    arango_provision_storage: bool = True
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_secret_key: str | None = None
    jwt_public_key: str | None = None
    jwt_algorithm: str = Field(default="HS256")

    keycloak_url: str | None = Field(
        default=None, validation_alias=AliasChoices("MGMCP_KEYCLOAK_URL", "KEYCLOAK_URL", "keycloak_url")
    )
    keycloak_realm: str | None = Field(
        default=None, validation_alias=AliasChoices("MGMCP_KEYCLOAK_REALM", "KEYCLOAK_REALM", "keycloak_realm")
    )
    keycloak_client_id: str | None = Field(
        default=None, validation_alias=AliasChoices("MGMCP_KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_ID", "keycloak_client_id")
    )
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_uri: str | None = None
    oidc_browser_origin: str = ""
    oidc_browser_client_id: str = ""
    oidc_browser_client_secret: SecretStr | None = None
    oidc_browser_token_endpoint: str = ""
    oidc_browser_discovery_url: str = ""
    oidc_browser_scopes: str = ""
    oidc_browser_audience: str = ""
    oidc_browser_token_auth: Literal["client_secret_basic", "client_secret_post"] = "client_secret_basic"
    oidc_role_claim: str = "roles"

    arango_url: str = Field(
        default="http://localhost:8530",
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
    arango_architecture_events_collection: str = "architecture_events"
    graph_event_producer_subject: str | None = None
    graph_event_project_key: MemoryKey | None = None
    capture_project_key: MemoryKey | None = None
    capture_repository_key: MemoryKey | None = None
    capture_spool: str = ""
    capture_interval: int = Field(default=60, ge=5, le=3600)
    capture_index_github: bool = False
    capture_github_repository: str = Field(default="", pattern=r"^(?:[\w.-]+/[\w.-]+)?$")
    ci_test_run_token: SecretStr | None = None
    ci_project_key: str = ""
    ci_repository_key: str = ""
    ci_github_repository: str = Field(default="", pattern=r"^(?:[\w.-]+/[\w.-]+)?$")
    ci_github_branch: str = "main"
    ci_github_read_token: SecretStr | None = None
    github_change_repositories: str = ""
    ci_github_publish_token: SecretStr | None = None
    test_fix_ollama_url: str = ""
    test_fix_ollama_model: str = "qwen3-coder:30b"
    workspace_agent_urls: dict[str, str] = Field(default_factory=dict)
    workspace_agent_allowed_urls: list[str] = Field(default_factory=list)
    workspace_ollama_allowed_urls: list[str] = Field(default_factory=list)
    workspace_agent_token: SecretStr | None = None
    draft_worker_url: str = ""
    draft_worker_token: SecretStr | None = None
    arango_documents_collection: str = Field(default="memory_documents")
    arango_edges_collection: str = Field(default="memory_edges")
    arango_vectors_collection: str = Field(default="memory_vectors")
    arango_search_view: str = Field(default="memory_search")
    arango_vector_view: str = Field(default="memory_vectors_view")

    embedding_provider: str = Field(default="deterministic")
    embedding_model: str = Field(default="deterministic-hash")
    embedding_dimension: int = Field(default=1536, ge=1)
    embedding_base_url: str = Field(default="http://localhost:11435")
    embedding_timeout_seconds: float = Field(default=30.0, ge=1.0)
    embedding_strict_mode: bool = False
    embedding_version: str | None = None

    embed_ollama_base_url: str = Field(
        default="http://localhost:11435",
        validation_alias=AliasChoices("EMBED_OLLAMA_BASE_URL", "MGMCP_EMBED_OLLAMA_BASE_URL"),
    )
    embed_model: str = Field(
        default="nomic-embed-text",
        validation_alias=AliasChoices("EMBED_MODEL", "MGMCP_EMBED_MODEL", "EMBEDDING_MODEL", "MGMCP_EMBEDDING_MODEL"),
    )
    embed_auto_pull: bool = Field(
        default=True,
        validation_alias=AliasChoices("EMBED_AUTO_PULL", "MGMCP_EMBED_AUTO_PULL"),
    )
    embed_require_ready: bool = Field(
        default=False,
        validation_alias=AliasChoices("EMBED_REQUIRE_READY", "MGMCP_EMBED_REQUIRE_READY"),
    )
    embed_timeout_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("EMBED_TIMEOUT_SECONDS", "MGMCP_EMBED_TIMEOUT_SECONDS"),
    )

    search_result_limit: int = Field(default=10, ge=1, le=100)
    vector_dimensions: int = Field(default=1536, ge=1)

    otel_service_name: str = Field(default="MemoryGraphMCP")
    otel_exporter_otlp_endpoint: str | None = None

    @field_validator("graph_event_producer_subject", "graph_event_project_key", mode="before")
    @classmethod
    def normalize_tracker_setting(cls, value: str | None) -> str | None:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("mcp_mount_path")
    @classmethod
    def normalize_mcp_mount_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            path = f"/{path}"
        return path.rstrip("/") or "/"

    @property
    def keycloak_issuer(self) -> str | None:
        if self.keycloak_url and self.keycloak_realm:
            return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}"
        return None

    @property
    def effective_jwt_issuer(self) -> str | None:
        return self.jwt_issuer or self.oidc_issuer or self.keycloak_issuer

    @property
    def effective_jwt_audience(self) -> str | None:
        return self.jwt_audience or self.oidc_audience or self.keycloak_client_id

    @property
    def keycloak_jwks_uri(self) -> str | None:
        issuer = self.oidc_issuer or self.keycloak_issuer
        return self.oidc_jwks_uri or (
            f"{issuer.rstrip('/')}/protocol/openid-connect/certs" if issuer else None
        )

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        if self.project_authorization_required and not self.auth_required:
            raise ValueError("Project authorization requires authentication")
        if self.project_authorization_required and self.arango_username == "root":
            raise ValueError("Project authorization requires a non-root ArangoDB runtime account")
        if self.auth_required and not (self.jwt_secret_key or self.jwt_public_key):
            if not (self.keycloak_jwks_uri and self.effective_jwt_issuer and self.effective_jwt_audience):
                raise ValueError("JWT authentication requires a signing key or an OIDC issuer, audience, and JWKS URI")
        if self.auth_required and (self.oidc_browser_origin or self.oidc_browser_client_id):
            origin = urlsplit(self.oidc_browser_origin)
            if (
                not origin.hostname or origin.username or origin.password
                or origin.path not in {"", "/"} or origin.query or origin.fragment
                or (origin.scheme != "https" and (
                    origin.scheme != "http" or origin.hostname not in {"localhost", "127.0.0.1", "::1"}
                ))
                or not self.oidc_browser_client_id or not self.effective_jwt_issuer
                or not self.effective_jwt_audience or not self.keycloak_jwks_uri
                or self.jwt_secret_key or self.jwt_public_key
            ):
                raise ValueError("Browser OIDC requires a UI origin, client ID, issuer, audience and JWKS")
        if self.graph_event_producer_subject or self.graph_event_project_key:
            if not (
                self.auth_required
                and self.graph_event_producer_subject
                and self.graph_event_project_key
            ):
                raise ValueError("Tracker events require authentication, a producer and a project")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
