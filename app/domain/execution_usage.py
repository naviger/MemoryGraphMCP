"""Usage evidence reported by an execution provider."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field


class ExecutionUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ExecutionAttempt(BaseModel):
    phase: int = Field(ge=0)
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    state: Literal["running", "succeeded", "failed", "interrupted"] = "running"
    failure: str | None = None
    usage: ExecutionUsage | None = None
