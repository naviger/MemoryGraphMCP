import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.codex_bridge import MODES, BridgeError, CodexBridge, git


@pytest.fixture
def bridge(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    (source / "fixture.txt").write_text("baseline\n")
    git(source, "add", ".")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "baseline",
    )
    cli = tmp_path / "fake-codex"
    cli.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
instructions = sys.stdin.read()
sandbox = sys.argv[sys.argv.index("--sandbox")+1]
target = pathlib.Path("fixture.txt")
if instructions.startswith("Implement"):
    assert sandbox == "workspace-write"
    target.write_text("changed by adapter\n")
else:
    assert sandbox == "read-only"
    if instructions.startswith("Review") and "FULL_LIFECYCLE" in instructions:
        assert target.read_text() == "changed by adapter\n"
result = {"summary": instructions.split(".")[0], "tests": ["fixture check"], "approved": True}
pathlib.Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps(result))
""".replace("adapter\n", "adapter\\n")
    )
    cli.chmod(0o700)
    return CodexBridge(tmp_path / "state", source, "target-project", str(cli))


@pytest.mark.parametrize("mode", MODES)
def test_supported_modes_preserve_source_and_replay(bridge, mode):
    identity = "a" * 64
    prompt = json.dumps({"project_key": "target-project", "mode": mode})
    results = {phase: bridge.run(identity, phase, prompt) for phase in MODES[mode]}
    assert (bridge.repo / "fixture.txt").read_text() == "baseline\n"
    assert git(bridge.repo, "status", "--porcelain") == b""
    restarted = CodexBridge(bridge.state, bridge.repo, bridge.project, bridge.executable)
    for phase, result in results.items():
        assert restarted.run(identity, phase, prompt) == result
    if "execute" in results:
        result = results["execute"]
        assert result["files_modified"] == ["fixture.txt"]
        assert b"+changed by adapter" in Path(result["artifacts"][0]["reference"]).read_bytes()
        assert result["completed"]
    if "review" in results:
        assert results["review"]["review_comments"]
        assert results["review"]["completed"] is (mode == "FULL_LIFECYCLE")


def test_scope_cancel_and_phase_order(bridge):
    identity = "b" * 64
    prompt = json.dumps({"project_key": "target-project", "mode": "FULL_LIFECYCLE"})
    for phase, context, code in (
        ("plan", json.dumps({"project_key": "other", "mode": "PLAN_ONLY"}), 403),
        ("review", prompt, 409),
    ):
        with pytest.raises(BridgeError) as error:
            bridge.run(identity, phase, context)
        assert error.value.code == code
    with ThreadPoolExecutor() as pool:
        assert pool.submit(bridge.cancel, identity).result()["cancelled"]
    with pytest.raises(BridgeError, match="cancelled"):
        bridge.run(identity, "plan", prompt)


def test_timeout_preserves_and_restores_partial_implementation(bridge, monkeypatch):
    import scripts.codex_bridge as module

    identity = "c" * 64
    prompt = json.dumps({"project_key": "target-project", "mode": "IMPLEMENT"})
    monkeypatch.setattr(module, "PHASE_TIMEOUT_SECONDS", 1)
    bridge_cli = Path(bridge.executable)
    bridge_cli.write_text("""#!/usr/bin/env python3
import pathlib, time
pathlib.Path("partial.txt").write_text("retained")
time.sleep(20)
""")
    with pytest.raises(BridgeError) as error:
        bridge.run(identity, "execute", prompt)
    assert error.value.code == 504
    checkpoint = bridge.state / identity / "partial-timeout.patch"
    assert b"+retained" in checkpoint.read_bytes()
    bridge_cli.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
assert pathlib.Path("partial.txt").read_text() == "retained"
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({
    "summary": "Resumed from the checkpoint.", "tests": [], "approved": True,
}))
""")
    result = bridge.run(identity, "execute", prompt)
    assert result["completed"]
    assert result["files_modified"] == ["partial.txt"]


def test_reported_usage_prices_failed_and_successful_turns():
    from scripts.codex_bridge import reported_usage

    events = "\n".join(
        json.dumps({"type": event, "usage": usage})
        for event, usage in (
            (
                "turn.completed",
                {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20},
            ),
            ("turn.failed", {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 5}),
            ("error", {}),
        )
    )
    assert reported_usage(events, {"input": 2, "cached_input": 1, "output": 4}) == {
        "input_tokens": 150,
        "cached_input_tokens": 50,
        "output_tokens": 25,
        "estimated_cost_usd": 0.00035,
    }
    assert reported_usage(events)["estimated_cost_usd"] is None
    assert reported_usage('{"type":"turn.failed"}') is None


def test_review_targets_saved_patch_even_after_checkout_changes(bridge):
    import hashlib

    source, review = "d" * 64, "e" * 64
    implementation = bridge.run(
        source,
        "execute",
        json.dumps(
            {
                "project_key": "target-project",
                "mode": "IMPLEMENT",
            }
        ),
    )
    original = Path(implementation["artifacts"][0]["reference"])
    result = bridge.run(
        review,
        "review",
        json.dumps(
            {
                "project_key": "target-project",
                "mode": "REVIEW",
                "patch_execution_key": source,
            }
        ),
    )
    assert result["review_verdict"] == "passed"
    assert result["reviewed_target"] == {
        "kind": "implementation patch",
        "execution_id": source,
        "base_commit": git(bridge.repo, "rev-parse", "HEAD").decode().strip(),
        "patch_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
    }
    assert (bridge.state / review / "repo" / "fixture.txt").read_text() == "changed by adapter\n"
    original.write_bytes(original.read_bytes() + b"\n")
    with pytest.raises(BridgeError, match="changed"):
        bridge.run(
            "f" * 64,
            "review",
            json.dumps(
                {
                    "project_key": "target-project",
                    "mode": "REVIEW",
                    "patch_execution_key": source,
                }
            ),
        )


def test_repair_starts_from_exact_reviewed_patch_and_honors_writer_prompt(bridge):
    from hashlib import sha256

    implementation = bridge.run(
        "1" * 64,
        "execute",
        json.dumps(
            {
                "project_key": "target-project",
                "mode": "IMPLEMENT",
            }
        ),
    )
    original = Path(implementation["artifacts"][0]["reference"])
    original_digest = sha256(original.read_bytes()).hexdigest()
    cli = Path(bridge.executable)
    cli.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
assert "Use the edited remediation text" in sys.stdin.read()
fixture = pathlib.Path("fixture.txt")
assert fixture.read_text().strip() == "changed by adapter"
fixture.write_text("repaired in a new clone")
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({
    "summary": "Fixed finding.", "tests": [], "approved": True,
}))
""")
    prompt = {
        "project_key": "target-project",
        "mode": "IMPLEMENT",
        "patch_execution_key": "1" * 64,
        "repair_prompt": "Use the edited remediation text",
    }
    with pytest.raises(BridgeError, match="differs"):
        bridge.run("2" * 64, "execute", json.dumps({**prompt, "repair_patch_sha256": "0" * 64}))
    result = bridge.run(
        "3" * 64,
        "execute",
        json.dumps(
            {
                **prompt,
                "repair_patch_sha256": original_digest,
            }
        ),
    )
    assert result["repaired_target"]["patch_sha256"] == original_digest
    assert b"+repaired in a new clone" in Path(result["artifacts"][0]["reference"]).read_bytes()
    assert sha256(original.read_bytes()).hexdigest() == original_digest


def test_required_regression_failure_skips_the_model_reviewer(bridge, monkeypatch):
    implementation = bridge.run(
        "4" * 64,
        "execute",
        json.dumps(
            {
                "project_key": "target-project",
                "mode": "IMPLEMENT",
            }
        ),
    )

    def fail_required_test(checkout, contract):
        assert (checkout / "fixture.txt").read_text().strip() == "changed by adapter"
        return {"passed": False, "command": "pytest tests/test_regression.py", "output": "failed"}

    monkeypatch.setattr(bridge, "verify_contract", fail_required_test)
    Path(bridge.executable).unlink()
    result = bridge.run(
        "5" * 64,
        "review",
        json.dumps(
            {
                "project_key": "target-project",
                "mode": "REVIEW",
                "patch_execution_key": "4" * 64,
                "repair_contract": {"test_paths": ["tests/test_regression.py"]},
            }
        ),
    )
    assert result["review_verdict"] == "blocking"
    assert result["reviewed_target"]["patch_sha256"] == implementation["artifacts"][0]["sha256"]
    assert not result["verification"]["passed"]
    assert "usage" not in result


@pytest.mark.parametrize("passed", [True, False])
def test_apply_approved_patch_and_review_exact_commit(bridge, monkeypatch, passed):
    source_id, job_id, review_id = "a" * 64, "b" * 64, "c" * 64
    project = {"project_key": bridge.project}
    implementation = bridge.run(source_id, "execute", json.dumps({**project, "mode": "IMPLEMENT"}))
    base = git(bridge.repo, "rev-parse", "HEAD").decode().strip()
    digest = implementation["artifacts"][0]["sha256"]
    contract = {"test_paths": ["tests/test_fixture.py"]}
    calls = []

    def verify(checkout, selected):
        assert selected == contract
        assert (checkout / "fixture.txt").read_text() == "changed by adapter\n"
        assert git(checkout, "status", "--porcelain") == b""
        calls.append(git(checkout, "rev-parse", "HEAD").decode().strip())
        return {"passed": passed, "command": "fixture check", "output": "fixture result"}

    monkeypatch.setattr(
        bridge,
        "verify_contract",
        lambda checkout, selected: {"passed": True, "command": "pre-apply", "output": ""},
    )
    bridge.run(
        review_id,
        "review",
        json.dumps(
            {
                **project,
                "mode": "REVIEW",
                "patch_execution_key": source_id,
                "repair_contract": contract,
            }
        ),
    )
    monkeypatch.setattr(bridge, "verify_contract", verify)
    context = {
        **project,
        "mode": "APPLY_APPROVED",
        "approved_review_key": review_id,
        "patch_execution_key": source_id,
        "repair_contract": contract,
        "approved_patch": {
            "kind": "implementation patch",
            "execution_id": source_id,
            "base_commit": base,
            "patch_sha256": digest,
        },
    }
    prompt = json.dumps(context)
    with pytest.raises(BridgeError, match="earlier phase"):
        bridge.run(job_id, "review_commit", prompt)
    with pytest.raises(BridgeError, match="no matching passing patch review"):
        bridge.run(
            job_id,
            "apply",
            json.dumps(
                {
                    **context,
                    "approved_patch": {**context["approved_patch"], "patch_sha256": "0" * 64},
                }
            ),
        )
    result = bridge.run(job_id, "apply", prompt)
    committed = result["applied_commit"]
    assert calls == [committed["commit"]]
    assert committed["base_commit"] == base
    assert committed["patch_sha256"] == digest
    assert committed["branch"] == f"codex/task-fix-{job_id[:12]}"
    assert bridge.run(job_id, "apply", prompt) == result
    assert len(calls) == 1
    clone = bridge.state / job_id / "repo"
    assert git(clone, "remote") == b""
    assert git(bridge.repo, "rev-parse", "HEAD").decode().strip() == base
    assert git(bridge.repo, "status", "--porcelain") == b""
    if passed:
        review = bridge.run(job_id, "review_commit", prompt)
        assert review["review_verdict"] == "passed"
        assert review["reviewed_target"] == {
            "kind": "fix branch commit",
            "commit": committed["commit"],
        }
    else:
        with pytest.raises(BridgeError, match="must pass"):
            bridge.run(job_id, "review_commit", prompt)


def test_initial_implementation_selects_and_runs_required_checks(bridge, monkeypatch):
    tests = bridge.repo / "tests"
    tests.mkdir()
    (tests / "test_fixture.py").write_text("def test_fixture(): pass\n")
    git(bridge.repo, "add", ".")
    git(
        bridge.repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "Add baseline tests",
    )
    checks = []

    def verify(checkout, contract):
        assert checkout.joinpath("fixture.txt").read_text() == "changed by adapter\n"
        checks.append(contract)
        return {"passed": True, "command": "pytest", "output": ""}

    monkeypatch.setattr(bridge, "verify_contract", verify)
    identity = "d" * 64
    context = {"project_key": bridge.project, "mode": "FULL_LIFECYCLE"}
    bridge.run(identity, "plan", json.dumps(context))
    result = bridge.run(identity, "execute", json.dumps(context))
    contract = result["generated_contract"]
    assert contract["test_paths"] == []  # Run the suite when no changed test file was identified.
    assert result["verification"]["passed"]
    review = bridge.run(identity, "review", json.dumps({**context, "repair_contract": contract}))
    assert review["verification"]["passed"]
    assert checks == [contract, contract]


@pytest.mark.parametrize("expected", ["revised", "blocked", "neither"])
def test_regression_proof_runs_same_test_on_blocked_patch_and_revision(
    bridge, monkeypatch, expected
):
    import subprocess
    import sys

    directory = bridge.state / ("d" * 64)
    directory.mkdir()
    checkout = bridge.prepare_repo(directory)
    (checkout / "fixture.txt").write_text("blocked")
    git(checkout, "add", ".")
    captured = directory / "repair-input.patch"
    captured.write_bytes(git(checkout, "diff", "--cached", "--binary"))
    blocked_tree = git(checkout, "write-tree").decode().strip()
    (checkout / "fixture.txt").write_text("revised")
    (checkout / "tests").mkdir()
    (checkout / "tests/test_regression.py").write_text(
        "from pathlib import Path\n"
        "def test_regression():\n"
        f"    assert Path('fixture.txt').read_text() == {expected!r}\n"
    )
    git(checkout, "add", ".")

    def pytest_check(repo, paths):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *paths],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "passed": result.returncode == 0,
            "exit_code": result.returncode,
            "output": result.stdout,
            "command": "pytest",
        }

    monkeypatch.setattr(bridge, "pytest_check", pytest_check)
    proof = bridge.prove_repair(checkout, directory, captured, blocked_tree)
    assert proof["passed"] is (expected == "revised")
    if expected != "blocked":
        assert proof["reproduced_tests"] == ["tests/test_regression.py::test_regression"]
        assert proof["revision"]["passed"] is (expected == "revised")
    else:
        assert proof["baseline"]["passed"]
        assert "revision" not in proof
