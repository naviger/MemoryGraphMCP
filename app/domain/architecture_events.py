from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.domain.architecture import EntityName, TaskStatus
from app.domain.models import MemoryKey

GraphEventType = Literal["task.upserted", "task.completed"]


class TaskEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: EntityName
    occurred_at: AwareDatetime
    project_key: MemoryKey
    task_key: MemoryKey

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def occurred_at_us(self) -> int:
        return (self.occurred_at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


class TaskUpserted(TaskEvent):
    event_type: Literal["task.upserted"]
    name: EntityName
    description: str | None = None
    status: TaskStatus | None = None


class TaskCompleted(TaskEvent):
    event_type: Literal["task.completed"]


GraphEvent = Annotated[
    TaskUpserted | TaskCompleted,
    # The event name selects the required payload fields.
    Field(discriminator="event_type"),
]


class GraphEventReceipt(BaseModel):
    event_id: EntityName
    event_type: GraphEventType
    source: str
    project_key: MemoryKey
    task_key: MemoryKey
    occurred_at: AwareDatetime
    recorded_at: AwareDatetime
    affected_entity_keys: list[MemoryKey]
    outcome: Literal["applied", "ignored_stale"]
