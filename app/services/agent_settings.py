"""Administratively managed task agents and read-only connection probes."""

from urllib.parse import urlsplit

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.exceptions import ArchitectureGraphError
from app.domain.project_workspace import AgentName, ExecutionMode

MODES = {
    "PLAN_ONLY": {"plan"},
    "IMPLEMENT": {"execute"},
    "REVIEW": {"review"},
    "FULL_LIFECYCLE": {"plan", "execute", "review"},
}
OLLAMA_MODES = ["PLAN_ONLY", "REVIEW"]
AGENTS = ("codex", "claude-code", "openhands", "local")


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=300)
    modes: list[ExecutionMode] = Field(default_factory=list, max_length=4)


class AgentEdit(BaseModel):
    agent: AgentName
    revision: int = Field(ge=0)
    config: AgentConfig


class AgentRegistry:
    def __init__(self, settings, graph):
        self.settings = settings
        self.graph = graph
        self.defaults = {
            key: AgentConfig(url=value.rstrip("/"), modes=list(MODES))
            for key, value in settings.workspace_agent_urls.items()
            if key in AGENTS
        }
        if settings.test_fix_ollama_url and "local" not in self.defaults:
            self.defaults["local"] = AgentConfig(
                url=settings.test_fix_ollama_url.rstrip("/"),
                model=settings.test_fix_ollama_model,
                modes=OLLAMA_MODES,
            )
        bridge_urls = {value.rstrip("/") for value in settings.workspace_agent_allowed_urls}
        ollama_urls = {value.rstrip("/") for value in settings.workspace_ollama_allowed_urls}
        if settings.test_fix_ollama_url:
            ollama_urls.add(settings.test_fix_ollama_url.rstrip("/"))
        self.allowed = {
            name: (
                ollama_urls
                if name == "local"
                else (bridge_urls | {settings.workspace_agent_urls.get(name, "").rstrip("/")})
                - ollama_urls
                - {""}
            )
            for name in AGENTS
        }
        # Preserve the legacy environment-only local coding adapter.
        self.local_bridge_url = settings.workspace_agent_urls.get("local", "").rstrip("/")

    def permits_execution(self, name, config):
        if name == "local" and not config.model:
            return bool(config.url and config.url == self.local_bridge_url)
        return config.url in self.allowed[name]

    def read(self):
        row = self.graph.read_agent_registry()
        values = self.defaults | {
            key: AgentConfig.model_validate(value) for key, value in row["agents"].items()
        }
        return row["revision"], values

    def normalize(self, edit):
        config = edit.config
        url = config.url.strip().rstrip("/")
        if not url:
            return AgentConfig()
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or url not in self.allowed[edit.agent]
        ):
            raise ArchitectureGraphError(
                "This endpoint has not been approved for the selected agent"
            )
        if edit.agent == "local":
            if not config.model.strip():
                raise ArchitectureGraphError("An installed Ollama model is required")
            return AgentConfig(url=url, model=config.model.strip(), modes=OLLAMA_MODES)
        return AgentConfig(url=url, modes=list(dict.fromkeys(config.modes)))

    def save(self, edit, actor):
        config = self.normalize(edit)
        if config.url and not config.modes:
            raise ArchitectureGraphError("Select at least one supported execution mode")
        self.graph.save_agent_registry(edit.agent, config.model_dump(), edit.revision, actor)

    def bridge_token(self, name, url):
        if self.settings.workspace_agent_urls.get(name, "").rstrip("/") == url:
            return self.settings.workspace_agent_token
        return None

    async def probe(self, edit):
        config = self.normalize(edit)
        if not config.url:
            raise ArchitectureGraphError("Enter an approved endpoint to test")
        token = self.bridge_token(edit.agent, config.url)
        headers = {"Authorization": "Bearer " + token.get_secret_value()} if token else {}
        try:
            async with httpx.AsyncClient(
                timeout=5,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                route = "/api/tags" if edit.agent == "local" else "/capabilities"
                with anyio.fail_after(8):
                    async with client.stream(
                        "GET", config.url + route, headers=headers
                    ) as response:
                        response.raise_for_status()
                        payload = bytearray()
                        async for chunk in response.aiter_bytes():
                            payload.extend(chunk)
                            if len(payload) > 262_144:
                                raise ValueError("Capability response too large")
                import json

                data = json.loads(payload)
            if edit.agent == "local":
                installed = [item.get("name") or item.get("model") for item in data["models"]]
                names = {config.model, config.model + ":latest"}
                if not names.intersection(installed):
                    return {
                        "ok": False,
                        "message": "Ollama responded, but the selected model is unavailable.",
                        "modes": [],
                    }
                modes = OLLAMA_MODES
            else:
                modes = AdvertisedModes.model_validate(data).modes
            return {"ok": True, "message": "Connection verified.", "modes": modes}
        except (
            TimeoutError,
            httpx.HTTPError,
            ValidationError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
        ):
            return {"ok": False, "message": "Connection or capability check failed.", "modes": []}


class AdvertisedModes(BaseModel):
    modes: list[ExecutionMode] = Field(min_length=1, max_length=4)
