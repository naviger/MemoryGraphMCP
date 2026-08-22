from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryKind = Literal["memory", "architecture", "adr", "design-language", "domain-concept"]
SearchMode = Literal["lexical", "semantic", "hybrid"]


class MemoryDocument(BaseModel):
    id: str | None = None
    key: str | None = None
    kind: MemoryKind = "memory"
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySearchRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=100)
    kinds: list[MemoryKind] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mode: SearchMode = "lexical"
    embedding: list[float] | None = None


class MemorySearchHit(BaseModel):
    document: MemoryDocument
    score: float = 0.0
    highlights: list[str] = Field(default_factory=list)


class RelatedEntity(BaseModel):
    entity_id: str
    label: str
    relation: str
    depth: int = 1
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphTraversalRequest(BaseModel):
    seed_entity_id: str
    depth: int = Field(default=2, ge=1, le=6)
    limit: int = Field(default=20, ge=1, le=100)
