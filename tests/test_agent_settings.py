from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.agent_settings import build_agent_settings_router
from app.config.settings import Settings
from app.domain.exceptions import ArchitectureGraphError
from app.services.agent_settings import AgentEdit, AgentRegistry
from app.services.task_execution import TaskExecutor


class Store:
    def __init__(self):
        self.state = {"revision": 0, "agents": {}}

    def read_agent_registry(self):
        return deepcopy(self.state)

    def save_agent_registry(self, agent, config, revision, actor):
        if revision != self.state["revision"]:
            raise ArchitectureGraphError("Stale revision")
        self.state["agents"][agent] = config
        self.state["revision"] += 1


def make_registry():
    settings = Settings(
        _env_file=None,
        auth_required=False,
        workspace_agent_urls={"codex": "http://bridge:8080"},
        workspace_agent_allowed_urls=["http://new-bridge:8080"],
        workspace_ollama_allowed_urls=["http://ollama:11434"],
        workspace_agent_token="configured-token",
    )
    return AgentRegistry(settings, Store())


def test_admin_only_and_runtime_reload():
    registry = make_registry()
    app = FastAPI()

    @app.middleware("http")
    async def claims(request: Request, call_next):
        role = request.headers.get("role")
        if role:
            request.state.auth_claims = {"subject": role, "raw": {"roles": [role]}}
        return await call_next(request)

    app.include_router(build_agent_settings_router("/api/review", registry))
    client = TestClient(app)
    path = "/api/review/admin/agents"
    request = {
        "agent": "codex",
        "revision": 0,
        "config": {"url": "http://new-bridge:8080", "modes": ["PLAN_ONLY"]},
    }
    for role in ("reader", "writer"):
        headers = {"role": role}
        assert client.get(path, headers=headers).status_code == 403
        assert client.post(path, headers=headers, json=request).status_code == 403
        assert client.post(path + "/test", headers=headers, json=request).status_code == 403
    admin = {"role": "admin"}
    view = client.get(path, headers=admin)
    assert "configured-token" not in view.text
    assert view.json()["allowed_urls"]["local"] == ["http://ollama:11434"]
    assert "http://ollama:11434" not in view.json()["allowed_urls"]["codex"]
    wrong_provider = {**request, "config": {"url": "http://ollama:11434", "modes": ["PLAN_ONLY"]}}
    assert client.post(path, headers=admin, json=wrong_provider).status_code == 409
    assert client.post(path + "/test", headers=admin, json=wrong_provider).status_code == 422
    invalid = {**request, "config": {"url": "http://169.254.169.254", "modes": ["PLAN_ONLY"]}}
    assert client.post(path, headers=admin, json=invalid).status_code == 409
    assert client.post(path + "/test", headers=admin, json=invalid).status_code == 422
    assert client.post(path, headers=admin, json=request).status_code == 200
    assert client.post(path, headers=admin, json=request).status_code == 409
    executor = TaskExecutor(
        registry.settings, SimpleNamespace(graph=registry.graph), None, registry
    )
    executor.refresh_agents()
    assert executor.modes() == {"codex": ["PLAN_ONLY"]}
    assert executor.providers["codex"].token is None
    assert executor.providers["codex"].url == "http://new-bridge:8080"
    registry.save(AgentEdit(agent="codex", revision=1, config={"url": ""}), "admin")
    executor.refresh_agents()
    assert executor.modes() == {}


@pytest.mark.anyio
async def test_probes_are_read_only_and_verify_capabilities(monkeypatch):
    registry = make_registry()
    calls = []

    def serve(request):
        calls.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local:latest"}]})
        if request.url.host == "new-bridge":
            return httpx.Response(302, headers={"Location": "http://other:1"})
        return httpx.Response(200, json={"modes": ["PLAN_ONLY", "REVIEW"]})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(
            **kw,
            transport=httpx.MockTransport(serve),
        ),
    )
    assert (
        await registry.probe(
            AgentEdit(
                agent="codex",
                revision=0,
                config={
                    "url": "http://bridge:8080",
                },
            )
        )
    )["modes"] == ["PLAN_ONLY", "REVIEW"]
    assert calls[-1].headers["authorization"] == "Bearer configured-token"
    assert not (
        await registry.probe(
            AgentEdit(
                agent="codex",
                revision=0,
                config={
                    "url": "http://new-bridge:8080",
                },
            )
        )
    )["ok"]
    assert calls[-1].url.host == "new-bridge"
    assert "authorization" not in calls[-1].headers
    for name, ok in (("local", True), ("missing", False)):
        assert (
            await registry.probe(
                AgentEdit(
                    agent="local",
                    revision=0,
                    config={
                        "url": "http://ollama:11434",
                        "model": name,
                    },
                )
            )
        )["ok"] is ok
    assert all(call.method == "GET" for call in calls)
    assert registry.read()[0] == 0


@pytest.mark.anyio
async def test_recommendation_only_accepts_workflow_eligible_agents(monkeypatch):
    registry = make_registry()
    executor = TaskExecutor(registry.settings, SimpleNamespace(graph=registry.graph), None)
    executor.local.url = "http://ollama:11434"
    executor.providers = {"codex": executor.providers["codex"], "local": executor.local}
    task = SimpleNamespace(name="Add documentation", description="Explain graph linking")

    async def generate(instruction, prompt):
        assert '"eligible_agents": ["codex"]' in prompt
        return {"agent": "local", "reason": "Text drafting"}

    monkeypatch.setattr(executor.local, "generate", generate)
    with pytest.raises(ArchitectureGraphError, match="eligible agent"):
        await executor.recommend(task, "FULL_LIFECYCLE")
