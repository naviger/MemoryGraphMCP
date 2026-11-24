import json
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from app.domain.architecture import RelationshipType
from app.domain.models import ADRConflictResolution, ArchitecturalDecisionRecord, MemoryKey, next_review_due
from app.domain.project_guidance import PublishedGuidance

GitRevision = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
_EMBEDDING_FIELDS = {
    "embedding", "embedding_provider", "embedding_model", "embedding_dimension",
    "embedding_version",
}
ADR_STORAGE_FIELDS = _EMBEDDING_FIELDS | {"id"}


def portable_adr(record: ArchitecturalDecisionRecord) -> ArchitecturalDecisionRecord:
    payload = record.model_dump(exclude=ADR_STORAGE_FIELDS)
    payload["metadata"] = {
        key: value for key, value in record.metadata.items()
        if key not in _EMBEDDING_FIELDS | {"embedding_fallback_used"}
    }
    return ArchitecturalDecisionRecord.model_validate(payload)


class SnapshotADR(ArchitecturalDecisionRecord):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def schedule_review(self, info: ValidationInfo) -> "SnapshotADR":
        preserve_missing = (info.context or {}).get("preserve_missing_review_dates")
        if self.next_review_due is None and not preserve_missing:
            self.next_review_due = next_review_due(self, self.last_validated or self.date)
        return self


class SnapshotGuidance(PublishedGuidance):
    model_config = ConfigDict(extra="forbid")


class ADROwnership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defined_by: list[MemoryKey]
    repository: str | None = None
    project_outbound: list[RelationshipType] = Field(default_factory=list)
    project_inbound: list[RelationshipType] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize(self) -> "ADROwnership":
        self.defined_by = sorted(set(self.defined_by))
        self.project_outbound = sorted(set(self.project_outbound))
        self.project_inbound = sorted(set(self.project_inbound))
        if not self.defined_by and self.repository is None:
            raise ValueError("ADR ownership requires a defining link or repository name")
        return self


class CoexistenceDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adr_key: MemoryKey
    project_keys: list[MemoryKey]
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def from_record(
        cls, record: ArchitecturalDecisionRecord, project_keys: list[str],
    ) -> "CoexistenceDependency":
        evidence = portable_adr(record).model_dump(
            mode="json", exclude=ADR_STORAGE_FIELDS | {"next_review_due"},
        )
        return cls(
            adr_key=record.key, project_keys=sorted(set(project_keys)),
            fingerprint=sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
        )


class SnapshotCoexistence(ADRConflictResolution):
    model_config = ConfigDict(extra="forbid")

    action: Literal["coexist"]
    dependency: CoexistenceDependency | None = None

    def portable_json(self) -> dict:
        return self.model_dump(
            mode="json", exclude={"dependency"} if self.dependency is None else None,
        )

    @property
    def record_key(self) -> str:
        first, second = sorted((self.source_key, self.target_key))
        return f"coexistence/{first}/{second}"


class KnowledgeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: Literal[1] = 1
    project_key: MemoryKey
    repository_key: MemoryKey
    source_revision: GitRevision
    adrs: list[SnapshotADR]
    guidance: SnapshotGuidance | None
    coexistence: list[SnapshotCoexistence] = Field(default_factory=list)
    adr_ownership: dict[MemoryKey, ADROwnership] = Field(default_factory=dict)

    def ownership_for(self, key: str) -> ADROwnership:
        return self.adr_ownership.get(key) or ADROwnership(defined_by=[self.repository_key])

    @model_validator(mode="after")
    def validate_adrs(self) -> "KnowledgeSnapshot":
        keys = set()
        for record in self.adrs:
            if record.status != "accepted" or record.model_dump() != portable_adr(record).model_dump():
                raise ValueError("Snapshots require accepted ADRs without database storage fields")
            if record.key in keys:
                raise ValueError(f"Duplicate ADR key: {record.key}")
            keys.add(record.key)
        self.adrs.sort(key=lambda record: record.key)
        if self.adr_ownership.keys() - keys:
            raise ValueError("ADR ownership must reference an ADR in the snapshot")
        self.adr_ownership = {
            key: owner for key, owner in sorted(self.adr_ownership.items())
            if owner != ADROwnership(defined_by=[self.repository_key])
        }
        pairs = set()
        for resolution in self.coexistence:
            endpoints = {resolution.source_key, resolution.target_key}
            external = endpoints - keys
            dependency = resolution.dependency
            if len(endpoints) != 2 or not endpoints & keys:
                raise ValueError("Coexistence requires two distinct ADRs with a project endpoint")
            if external != ({dependency.adr_key} if dependency else set()):
                raise ValueError("Coexistence dependency must identify the external ADR")
            if resolution.record_key in pairs:
                raise ValueError(f"Duplicate coexistence resolution: {resolution.record_key}")
            pairs.add(resolution.record_key)
        self.coexistence.sort(key=lambda resolution: resolution.record_key)
        return self

    @classmethod
    def from_database(
        cls, project: str, repository: str, revision: str,
        adrs: list[ArchitecturalDecisionRecord], guidance: PublishedGuidance | None,
        coexistence: list[ADRConflictResolution] | None = None,
        *, preserve_missing_review_dates: bool = False,
        adr_ownership: dict[str, ADROwnership] | None = None,
    ) -> "KnowledgeSnapshot":
        return cls.model_validate({
            "project_key": project,
            "repository_key": repository,
            "source_revision": revision,
            "adrs": [portable_adr(adr).model_dump() for adr in adrs],
            "guidance": guidance.model_dump() if guidance else None,
            "coexistence": [resolution.model_dump() for resolution in coexistence or []],
            "adr_ownership": adr_ownership or {},
        }, context={"preserve_missing_review_dates": preserve_missing_review_dates})

    def records(self) -> dict[str, dict]:
        records = {
            f"adr/{adr.key}": adr.model_dump(mode="json", exclude=ADR_STORAGE_FIELDS)
            for adr in self.adrs
        }
        for key, ownership in self.adr_ownership.items():
            records[f"adr/{key}"]["ownership"] = ownership.model_dump(mode="json")
        if self.guidance is not None:
            records["guidance"] = self.guidance.model_dump(mode="json")
        records.update({
            resolution.record_key: resolution.portable_json()
            for resolution in self.coexistence
        })
        return records

    def portable_json(self) -> dict:
        payload = self.model_dump(mode="json", exclude={"adrs", "coexistence"})
        if not self.adr_ownership:
            payload.pop("adr_ownership")
        payload["coexistence"] = [resolution.portable_json() for resolution in self.coexistence]
        payload["adrs"] = [
            adr.model_dump(mode="json", exclude=ADR_STORAGE_FIELDS) for adr in self.adrs
        ]
        return payload


class KnowledgeDrift(BaseModel):
    record: str
    status: Literal["git_only", "database_only", "different", "occupied"]
    git: dict | None
    database: dict | None


def compare_knowledge(
    git: KnowledgeSnapshot, database: KnowledgeSnapshot, occupied: dict[str, dict],
) -> list[KnowledgeDrift]:
    if (git.project_key, git.repository_key) != (database.project_key, database.repository_key):
        raise ValueError("Snapshot owners must match")
    git_records, database_records = git.records(), database.records()
    drift = []
    for key in sorted(git_records.keys() | database_records.keys()):
        git_value, db_value = git_records.get(key), database_records.get(key)
        if key in occupied:
            drift.append(KnowledgeDrift(
                record=key, status="occupied", git=git_value, database=occupied[key],
            ))
        elif git_value != db_value:
            status = "different"
            if git_value is None:
                status = "database_only"
            elif db_value is None:
                status = "git_only"
            drift.append(KnowledgeDrift(
                record=key, status=status, git=git_value, database=db_value,
            ))
    return drift
