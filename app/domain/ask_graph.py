from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.domain.models import ADRConflict, MemoryKey


class AskGraphQuery(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
    as_of: date | None = None
    project_key: MemoryKey | None = None


class AnswerCitation(BaseModel):
    key: str
    title: str
    destination: Literal["records", "entities"] = "records"


class AnswerFinding(BaseModel):
    text: str
    citations: list[AnswerCitation] = Field(min_length=1)


class AskGraphAnswer(BaseModel):
    as_of: date
    summary: str
    findings: list[AnswerFinding]
    conflicts: list[ADRConflict]
    gaps: list[str]
