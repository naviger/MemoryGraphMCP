"""Durable source checkpoints and operator-requested history scans."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

CaptureStream = Literal["spool", "graph", "commits", "pulls", "index"]


class CaptureCheckpoint(BaseModel):
    stream: CaptureStream
    revision: int = 0
    cursor: str | None = None
    observed_head: str | None = None
    observed_timestamp: AwareDatetime | None = None
    last_history_action: Literal["replay", "backfill"] | None = None
    last_history_completed: AwareDatetime | None = None
    pending_count: int | None = None
    oldest_pending: AwareDatetime | None = None
    source_timestamp: AwareDatetime | None = None
    last_success: AwareDatetime | None = None
    last_failure: AwareDatetime | None = None
    failure: str | None = None
    consecutive_failures: int = 0
    retry_at: AwareDatetime | None = None
    history: Literal["replay", "backfill"] | None = None
    history_since: AwareDatetime | None = None
    history_requested_by: str | None = None


class CaptureHistoryInput(BaseModel):
    stream: Literal["commits", "pulls"]
    action: Literal["replay", "backfill"]
    since: AwareDatetime


class CaptureStatus(BaseModel):
    configured: bool
    interval_seconds: int
    streams: list[CaptureCheckpoint] = Field(default_factory=list)
