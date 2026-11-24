"""Run configured checks in a disposable local container without credentials or networking."""

import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4


def run_isolated_check(worktree: Path, image: str, command: list[str]) -> dict:
    name = "mgmcp-fix-" + uuid4().hex
    docker = shutil.which("docker") or "/usr/local/bin/docker"
    args = [
        docker,
        "run",
        "--rm",
        "--name",
        name,
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "96",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--user",
        "65532:65532",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m",
        "--workdir",
        "/src",
        "--mount",
        f"type=bind,source={worktree},target=/src,readonly",
        image,
        *command,
    ]
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, timeout=90)
        except FileNotFoundError:
            return {"command": " ".join(command), "passed": False, "output": "Docker unavailable."}
        except subprocess.TimeoutExpired:
            subprocess.run(
                [docker, "rm", "-f", name],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return {"command": " ".join(command), "passed": False, "output": "Check timed out."}
        output.seek(max(0, output.tell() - 24_000))
        text = output.read().decode(errors="replace")
    return {
        "command": " ".join(command),
        "passed": result.returncode == 0,
        "output": text,
        "exit_code": result.returncode,
    }
