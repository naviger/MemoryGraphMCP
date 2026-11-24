from copy import deepcopy
import subprocess
from pathlib import Path

import pytest

from app.pilot_benchmark import Benchmark, Case, TrialSet, collect_scorecard, pinned_source_excerpts, trial_plan


def manifest():
    return Benchmark.model_validate({"cases": [{
        "id": "decision", "project": "Example", "commit": "a" * 40,
        "kind": "retrieval", "prompt": "Which decision governs this?",
        "as_of": "2026-11-24",
        "expected": [{
            "id": "decision", "claim": "Use canonical identifiers.",
            "citations": ["records:adr", "repo:docs/adr.md"],
        }],
    }]})


def trials():
    base = dict(case_id="decision", model="same-model", setup="same-settings",
                commit="a" * 40, evidence="local/run-log.json")
    return {"trials": [
        dict(**base, condition="repository", findings={"decision": ["repo:docs/adr.md"]}, tokens=100),
        dict(**base, condition="memorygraph", findings={"decision": ["records:wrong"]}, tokens=60),
    ]}


def test_score_groups_equivalent_citations_and_keeps_missing_measurements_unknown():
    result = collect_scorecard(manifest(), TrialSet.model_validate(trials()))
    case, = result["cases"]
    assert case["repository"]["supported_claims"] == 1
    assert case["memorygraph"]["supported_claims"] == 0
    assert case["memorygraph_minus_repository"] == {"tokens": -40, "seconds": None, "retries": None}
    assert result["retrieval"]["repository"] == {"supported_claims": 1, "required_claims": 1}


def test_plan_withholds_expected_answers():
    cases = trial_plan(manifest())
    assert cases[0]["as_of"] == "2026-11-24"
    assert "expected" not in cases[0]


def test_pair_rejects_unmatched_model_commit_and_conditions():
    for field, value in (("model", "other-model"), ("commit", "b" * 40),
                         ("setup", "other-settings"), ("condition", "repository")):
        data = deepcopy(trials())
        data["trials"][1][field] = value
        with pytest.raises(ValueError):
            collect_scorecard(manifest(), TrialSet.model_validate(data))


def test_delivery_requires_exact_commands_and_evidence():
    document = manifest().model_dump(mode="json")
    case = document["cases"][0]
    case.update(kind="delivery", checks={"regression": "python -m pytest tests/test_example.py"})
    data = trials()
    for trial in data["trials"]:
        trial["checks"] = {"regression": {
            "command": case["checks"]["regression"], "status": "pass", "evidence": "run.log",
        }}
    result = collect_scorecard(Benchmark.model_validate(document), TrialSet.model_validate(data))
    report, = result["cases"]
    assert report["repository"]["passed_checks"] == 1
    data["trials"][1]["checks"]["regression"]["command"] = "python -m pytest -k unrelated"
    with pytest.raises(ValueError, match="checks"):
        collect_scorecard(Benchmark.model_validate(document), TrialSet.model_validate(data))


def test_checked_in_pilot_manifest_is_small_and_covers_both_case_kinds():
    pilot = Benchmark.model_validate_json(Path("docs/pilot/benchmark.json").read_text())
    assert len(pilot.cases) == 4
    assert {case.project for case in pilot.cases} == {"MemoryGraphMCP", "Sextant"}
    assert {case.kind for case in pilot.cases} == {"retrieval", "delivery"}
    plans = trial_plan(pilot)
    assert all(bool(case["delivery_guidance"]) == (case["kind"] == "delivery") for case in plans)
    assert all("expected" not in case for case in plans)


def test_delivery_guidance_cannot_appear_in_a_retrieval_case():
    document = manifest().model_dump(mode="json")
    document["cases"][0]["delivery_guidance"] = ["Run a pinned check."]
    with pytest.raises(ValueError, match="Only delivery cases"):
        Benchmark.model_validate(document)



def test_source_excerpt_uses_pinned_commit_and_rejects_an_unavailable_range(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    source = tmp_path / "validation.js"
    source.write_text("function validate() {\n  return 'Never validated';\n}\n")
    git("add", "validation.js")
    git("-c", "user.name=Pilot", "-c", "user.email=pilot@example.test", "commit", "-qm", "Pinned")
    commit = git("rev-parse", "HEAD")
    source.write_text("changed in working tree\n")
    payload = manifest().cases[0].model_dump(mode="json")
    payload.update(commit=commit, source_excerpts=[dict(path="validation.js", first_line=2, last_line=2)])
    case = Case.model_validate(payload)
    assert pinned_source_excerpts(case, tmp_path) == [{
        "excerpt_id": "R1", "citation_id": "repo:validation.js", "commit": commit,
        "path": "validation.js", "first_line": 2, "last_line": 2, "text": "  return 'Never validated';",
    }]
    payload["source_excerpts"][0]["last_line"] = 4
    with pytest.raises(ValueError, match="exceeds pinned file"):
        pinned_source_excerpts(Case.model_validate(payload), tmp_path)
