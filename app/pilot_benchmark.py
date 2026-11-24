"""Validate and score paired architecture retrieval and delivery pilot trials."""

import argparse
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from app.domain.architecture import GitCommit
from app.domain.models import CalendarDate

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Condition = Literal["repository", "memorygraph"]
METRICS = ("tokens", "seconds", "retries")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedClaim(Input):
    id: Text
    claim: Text
    citations: list[Text] = Field(min_length=1)


class SourceExcerpt(Input):
    path: Text
    first_line: int = Field(ge=1)
    last_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_location(self):
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != self.path or self.path == ".":
            raise ValueError("Use a repository-relative source path")
        if not 0 <= self.last_line - self.first_line < 40:
            raise ValueError("Specify an ordered source range of at most 40 lines")
        return self


class Case(Input):
    id: Text
    project: Text
    repository: Text | None = None
    commit: GitCommit
    kind: Literal["retrieval", "delivery"]
    prompt: Text
    as_of: CalendarDate
    expected: list[ExpectedClaim] = Field(min_length=1)
    checks: dict[Text, Text] = Field(default_factory=dict)
    delivery_guidance: list[Text] = Field(default_factory=list)
    source_excerpts: list[SourceExcerpt] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_case(self):
        if bool(self.checks) != (self.kind == "delivery"):
            raise ValueError("Exactly the delivery cases need checks")
        if self.delivery_guidance and self.kind != "delivery":
            raise ValueError("Only delivery cases can include delivery guidance")
        if len({claim.id for claim in self.expected}) != len(self.expected):
            raise ValueError("Expected claim IDs must be unique within a case")
        return self


class Benchmark(Input):
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be unique")
        return self


class Check(Input):
    command: Text
    status: Literal["pass", "fail", "not_run"]
    evidence: Text


class Trial(Input):
    case_id: Text
    condition: Condition
    model: Text
    setup: Text
    commit: GitCommit
    evidence: Text
    findings: dict[Text, list[Text]] = Field(default_factory=dict)
    checks: dict[Text, Check] = Field(default_factory=dict)
    tokens: int | None = Field(default=None, ge=0)
    seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    retries: int | None = Field(default=None, ge=0)


class TrialSet(Input):
    trials: list[Trial] = Field(min_length=1)


def trial_plan(benchmark: Benchmark) -> list[dict]:
    return [case.model_dump(mode="json", exclude={"expected"}) for case in benchmark.cases]


def pinned_source_excerpts(case: Case, repository: Path) -> list[dict]:
    """Read selectable source evidence from the case commit, regardless of checkout state."""
    evidence = []
    for index, excerpt in enumerate(case.source_excerpts, 1):
        revision_path = f"{case.commit}:{excerpt.path}"
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repository), "show", revision_path],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if result.returncode:
            raise ValueError(f"Cannot read pinned source {revision_path}")
        lines = result.stdout.splitlines()
        if excerpt.last_line > len(lines):
            raise ValueError(f"Source range exceeds pinned file {revision_path}")
        evidence.append({
            "excerpt_id": f"R{index}",
            "citation_id": f"repo:{excerpt.path}",
            "commit": case.commit,
            **excerpt.model_dump(),
            "text": "\n".join(lines[excerpt.first_line - 1:excerpt.last_line]),
        })
    return evidence


def collect_scorecard(benchmark: Benchmark, results: TrialSet) -> dict:
    trials = {(row.case_id, row.condition): row for row in results.trials}
    conditions = ("repository", "memorygraph")
    pairs = {(case.id, condition) for case in benchmark.cases for condition in conditions}
    if set(trials) != pairs or len(trials) != len(results.trials):
        raise ValueError("Provide exactly one repository and one memorygraph trial per case")

    retrieval = {condition: {"supported_claims": 0, "required_claims": 0} for condition in conditions}
    delivery = {condition: {"cases_with_all_checks_passing": 0, "cases": 0} for condition in conditions}
    rows = []
    for case in benchmark.cases:
        baseline, assisted = (trials[case.id, condition] for condition in conditions)
        if baseline.commit != case.commit or assisted.commit != case.commit:
            raise ValueError(f"{case.id}: both trials must use the pinned checkout")
        if (baseline.model, baseline.setup) != (assisted.model, assisted.setup):
            raise ValueError(f"{case.id}: both trials must use the same model and setup")

        row = {"id": case.id, "project": case.project, "kind": case.kind, "commit": case.commit,
               "as_of": case.as_of.isoformat(), "model": baseline.model, "setup": baseline.setup}
        claim_ids = {claim.id for claim in case.expected}
        for trial in (baseline, assisted):
            if trial.findings.keys() - claim_ids:
                raise ValueError(f"{case.id}/{trial.condition}: unknown claim ID")
            if {key: value.command for key, value in trial.checks.items()} != case.checks:
                raise ValueError(f"{case.id}/{trial.condition}: report the exact configured checks")
            supported = [
                claim.id for claim in case.expected
                if set(claim.citations).intersection(trial.findings.get(claim.id, []))
            ]
            passed = sum(check.status == "pass" for check in trial.checks.values())
            row[trial.condition] = {
                "supported_claims": len(supported), "required_claims": len(claim_ids),
                "missing_claim_ids": sorted(claim_ids - set(supported)),
                "passed_checks": passed, "required_checks": len(case.checks),
                "evidence": trial.evidence,
                "checks": {key: check.model_dump() for key, check in trial.checks.items()},
                **{metric: getattr(trial, metric) for metric in METRICS},
            }
            if case.kind == "retrieval":
                totals = retrieval[trial.condition]
                totals["supported_claims"] += len(supported)
                totals["required_claims"] += len(claim_ids)
            else:
                totals = delivery[trial.condition]
                totals["cases_with_all_checks_passing"] += passed == len(case.checks)
                totals["cases"] += 1
        row["memorygraph_minus_repository"] = {}
        for metric in METRICS:
            a, b = getattr(baseline, metric), getattr(assisted, metric)
            row["memorygraph_minus_repository"][metric] = b - a if a is not None and b is not None else None
        rows.append(row)
    return {"cases": rows, "retrieval": retrieval, "delivery": delivery}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "score"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results", type=Path)
    args = parser.parse_args()
    if (args.action == "score") != (args.results is not None):
        parser.error("Provide --results for score only")
    try:
        benchmark = Benchmark.model_validate_json(args.manifest.read_text(encoding="utf-8"))
        output = trial_plan(benchmark) if args.action == "plan" else collect_scorecard(
            benchmark, TrialSet.model_validate_json(args.results.read_text(encoding="utf-8")),
        )
    except (OSError, ValidationError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
