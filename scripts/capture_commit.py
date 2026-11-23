"""Write commit evidence to the configured local capture spool."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], timeout=60)


def capture(spool: Path, index_branch: str | None) -> None:
    sha, date, message = git("show", "-s", "--format=%H%x00%cI%x00%s", "HEAD").decode().split("\0", 2)
    common = ("show", "--format=", "--first-parent", "--diff-merges=first-parent", "--root", sha)
    files = git(*common, "--name-only", "-z").decode("utf-8", errors="replace").strip("\0").split("\0")
    patch = git("-c", "core.quotePath=false", *common, "--no-ext-diff", "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/").decode(
        "utf-8", errors="replace",
    )
    timestamp = datetime.now(timezone.utc)
    item = {"queued_at": timestamp.isoformat(), "commit": {
        "sha": sha, "timestamp": date, "subject": message.strip()[:500],
        "changed_files": [name for name in files[:500] if name], "diff": patch[:250_000],
        "truncated": len(files) > 500 or len(patch) > 250_000,
    }}
    branch = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                            capture_output=True, text=True, check=False, timeout=10).stdout.strip()
    if index_branch and branch == index_branch:
        paths = git("ls-tree", "-rz", "--name-only", sha).decode("utf-8", errors="replace").split("\0")
        snapshots = {}
        total = 0
        for path in paths:
            if path.endswith(".py"):
                content = git("show", f"{sha}:{path}")
                total += len(content)
                if total > 8_000_000:
                    print("MemoryGraphMCP capture: Python snapshot exceeds the limit. Commit evidence queued.")
                    break
                snapshots[path] = content.decode("utf-8", errors="replace")
        else:
            item["python_snapshot"] = snapshots

    spool.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{timestamp:%Y%m%d%H%M%S%f}-{sha}.capture"
    with tempfile.NamedTemporaryFile(dir=spool, mode="w", encoding="utf-8", delete=False) as temp:
        name_temp = Path(temp.name)
        try:
            json.dump(item, temp, ensure_ascii=False)
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(name_temp, spool / name)
        finally:
            name_temp.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spool", type=Path)
    parser.add_argument("--index-branch")
    args = parser.parse_args()
    capture(args.spool, args.index_branch)
