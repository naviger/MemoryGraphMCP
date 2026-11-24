"""Acceptance criteria and deterministic checks for a proposed patch."""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

Criterion = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
TestPath = Annotated[
    str, StringConstraints(pattern=r"^tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_-]+\.py$")
]


class RepairContract(BaseModel):
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=8)
    test_paths: list[TestPath] = Field(max_length=8)
    additional_tokens: int | None = Field(default=None, ge=1, le=20_000_000)


class RepairVerification(BaseModel):
    passed: bool
    command: str = Field(default="", max_length=2000)
    output: str = Field(default="", max_length=24000)


class RegressionProof(BaseModel):
    passed: bool
    summary: str
    baseline: RepairVerification | None = None
    revision: RepairVerification | None = None
    reproduced_tests: list[str] = Field(default_factory=list)
