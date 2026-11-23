from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.effectiveness import build_effectiveness_router
from app.domain.effectiveness import DevelopmentOutcome, HoursEvidence
from app.services.effectiveness import _agent_check_outcomes, economics


def change(**updates):
    return DevelopmentOutcome(**{
        "project_key": "example", "repository_key": "repo", "change_id": "abc123",
        "actor": "agent", "occurred_at": datetime.now(UTC) - timedelta(hours=1),
        "evidence": "Test log", **updates,
    })


def ci(attempt, status, *, repository="repo", commit="abc123"):
    return {"event_type": "test.run.finished", "payload": {
        "run_id": f"44:{attempt}", "finished_at": f"2026-07-01T10:{attempt:02d}:00Z",
        "repository_key": repository, "commit": commit, "status": status,
    }}


def test_exact_commit_attempts_fill_missing_agent_results():
    rows = [ci(1, "passed", repository="other"), ci(1, "failed"),
            ci(2, "cancelled"), ci(3, "passed"), ci(5, "failed", commit="different")]
    assert _agent_check_outcomes([change(), change(actor="human")], rows) == ([False], [1])
    assert _agent_check_outcomes([change()], rows[2:]) == ([], [])
    reported = change(first_pass_success=True, retries=0)
    assert _agent_check_outcomes([reported], rows) == ([True], [0])


def test_estimates_use_only_comparable_rows_and_cited_effort():
    telemetry = [
        {"event_type": "retrieval.usage", "context_tokens": 100, "savings_tokens": 200},
        {"event_type": "retrieval.usage", "context_tokens": 150, "savings_tokens": -50},
        {"event_type": "retrieval.usage", "context_tokens": 20, "savings_tokens": None},
    ]
    estimates = economics(telemetry, [
        change(avoided_effort=[
            HoursEvidence(category="review", hours=2, confidence=.8, evidence="Before and after logs"),
            HoursEvidence(category="rework", hours=1, confidence=.2, evidence="Recorded avoided edit"),
        ]),
    ], 5)
    assert estimates["token_savings"].value == 150
    assert estimates["token_savings"].confidence == .33
    assert estimates["context_reduction_percent"].value == 37.5
    assert estimates["model_cost_avoided"].value == .00075
    assert estimates["engineering_hours_avoided"].value == 3
    assert estimates["engineering_hours_avoided"].confidence == .6
    assert estimates["onboarding_hours_avoided"].value is None
    assert economics([], [], 5)["model_cost_avoided"].confidence is None


def test_role_checks_and_validation_for_outcome_reports():
    saved = []

    async def report(outcome, writer):
        saved.append((outcome, writer))
        return {"key": "receipt", "outcome": outcome, "recorded_at": datetime.now(UTC), "recorded_by": writer}

    app = FastAPI()
    app.include_router(build_effectiveness_router(SimpleNamespace(report=report), "/api/review"))

    @app.middleware("http")
    async def claims(request, call_next):
        role = request.headers.get("x-test-role", "reader")
        request.state.auth_claims = {"subject": role, "raw": {"roles": [role]}}
        return await call_next(request)

    client = TestClient(app)
    path = "/api/review/effectiveness/outcomes"
    payload = change().model_dump(mode="json")
    assert client.post(path, json=payload).status_code == 403
    assert not saved
    writer_response = client.post(path, json=payload, headers={"x-test-role": "writer"})
    assert writer_response.status_code == 200
    assert writer_response.json()["recorded_by"] == "writer"
    with pytest.raises(ValidationError, match="linked task"):
        change(task_outcome="completed")
    with pytest.raises(ValidationError, match="successful first pass"):
        change(first_pass_success=True, retries=1)
