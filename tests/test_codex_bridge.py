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
