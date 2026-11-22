"""Write a bounded test summary for GitHub Actions artifact upload."""

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

MAX_FAILURES = 500
MAX_LABEL = 1000
_EXCEPTION = re.compile(
    r"^E[ \t]+(?:[\w]+\.)*([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception))\b",
    re.MULTILINE,
)


def read_report(path: Path) -> dict:
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    failures = []
    if not path.is_file():
        return {**counts, "failures": failures}
    for case in ElementTree.parse(path).iter("testcase"):
        kind = next((tag for tag in ("error", "failure") if case.find(tag) is not None), None)
        if kind:
            counts["failed"] += 1
            if len(failures) < MAX_FAILURES:
                source = PurePosixPath(case.get("file") or "")
                source_path = None
                if source.parts and not source.is_absolute() and ".." not in source.parts:
                    normalized = str(source)
                    if len(normalized) <= MAX_LABEL:
                        source_path = normalized
                name = ".".join(filter(None, (case.get("classname"), case.get("name"))))
                detail = {
                    "kind": kind,
                    "name": (name or "Unnamed test")[:MAX_LABEL],
                    "source_path": source_path,
                }
                # Keep an exception class only. Pytest can print a chained root cause before
                # the wrapper exception, so retain the first reported exception.
                node = case.find(kind)
                exception = _EXCEPTION.search(node.text or "") if node is not None else None
                if exception:
                    detail["exception_type"] = exception[1][:100]
                failures.append(detail)
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    return {**counts, "failures": failures}


def main(report: Path) -> None:
    outcome = os.environ["TEST_OUTCOME"]
    if outcome == "success" and not report.is_file():
        raise SystemExit("The successful test step did not produce a JUnit report")
    payload = {
        **read_report(report),
        "run_id": f"{os.environ['GITHUB_RUN_ID']}:{os.environ['GITHUB_RUN_ATTEMPT']}",
        "commit": os.environ["GITHUB_SHA"],
        "finished_at": os.environ["TEST_FINISHED_AT"],
        "status": {"success": "passed", "failure": "failed", "cancelled": "cancelled"}[outcome],
    }
    Path("test-run.json").write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
