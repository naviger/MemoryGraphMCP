"""Write a bounded test summary for GitHub Actions artifact upload."""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

MAX_FAILURES = 500
MAX_LABEL = 1000
_EXCEPTION = re.compile(
    r"^E[ \t]+(?:[\w]+\.)*([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception))\b",
    re.MULTILINE,
)


MAX_WARNINGS = 100
_test_warnings = []
_test_warning_count = 0


def pytest_warning_recorded(warning_message, nodeid):
    """Keep a bounded diagnostic without exporting arbitrary warning output."""
    global _test_warning_count
    _test_warning_count += 1
    if len(_test_warnings) >= MAX_WARNINGS:
        return
    path = Path(warning_message.filename).resolve()
    try:
        source = path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        source = nodeid.split("::", 1)[0] or None
    if source and source.startswith(".venv/"):
        source = nodeid.split("::", 1)[0] or None
    category = warning_message.category.__name__
    detail = "Pytest reported a warning. Open the CI log for the full diagnostic."
    if "get_event_loop_policy" in str(warning_message.message):
        detail = "A test dependency uses the deprecated asyncio.get_event_loop_policy API."
    elif category == "InsecureKeyLengthWarning":
        detail = "The JWT HMAC test key is shorter than the recommended minimum."
    _test_warnings.append(
        {
            "name": category[:MAX_LABEL],
            "source_path": source if source and len(source) <= MAX_LABEL else None,
            "message": detail,
            "stage": "test",
        }
    )


def pytest_unconfigure(config):
    Path("test-warnings.json").write_text(
        json.dumps(
            {
                "count": _test_warning_count,
                "warnings": _test_warnings,
            }
        ),
        encoding="utf-8",
    )


def build_warnings(path: Path) -> list[dict]:
    warnings = []
    if not path.is_file():
        return warnings
    with path.open(errors="replace") as log:
        for line in log:
            text = line.strip()
            if "chunks are larger than" in text:
                name = "Vite chunk size warning"
                detail = "Vite reported a JavaScript chunk above its configured size limit."
                source = "review-ui/package.json"
            elif "Module level directives cause errors" in text:
                name = "Bundling directive warning"
                detail = "The frontend bundler ignored a dependency's module-level directive."
                source = "review-ui/package.json"
            elif re.match(r"^(?:WARNING:|npm warn |[A-Za-z]+Warning:)", text) and (
                "Running pip as" not in text
            ):
                name = "Build warning"
                detail = (
                    "The build tool emitted a warning. Open the CI log for the full diagnostic."
                )
                source = "pyproject.toml"
            else:
                continue
            warnings.append(
                {"name": name, "message": detail, "source_path": source, "stage": "build"}
            )
    return warnings


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
    build = os.environ.get("BUILD_OUTCOME", "skipped")
    warnings_file = Path("test-warnings.json")
    test_warnings = (
        json.loads(warnings_file.read_text())
        if warnings_file.exists()
        else {
            "count": 0,
            "warnings": [],
        }
    )
    warnings = build_warnings(Path("build.log"))
    if outcome == "success" and not report.is_file():
        raise SystemExit("The successful test step did not produce a JUnit report")
    payload = {
        **read_report(report),
        "run_id": f"{os.environ['GITHUB_RUN_ID']}:{os.environ['GITHUB_RUN_ATTEMPT']}",
        "commit": os.environ["GITHUB_SHA"],
        "finished_at": os.environ.get("TEST_FINISHED_AT") or datetime.now(UTC).isoformat(),
        "status": {
            "success": "passed",
            "failure": "failed",
            "cancelled": "cancelled",
            "skipped": "cancelled",
        }[outcome],
        "build_status": {
            "success": "passed",
            "failure": "failed",
            "cancelled": "skipped",
            "skipped": "skipped",
        }[build],
        "warning_count": len(warnings) + test_warnings["count"],
        "warnings": (warnings + test_warnings["warnings"])[:MAX_WARNINGS],
    }
    Path("test-run.json").write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
