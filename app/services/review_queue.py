from __future__ import annotations

from datetime import UTC, datetime

import anyio

from app.domain.models import (
    ADRConflictListQuery,
    ADRConflictResolution,
    ADRResolutionAction,
    ADRResolutionDecision,
    ConfidenceScore,
    MemoryRevalidationResult,
    MemoryValidation,
    ReviewQueue,
    ReviewQueueQuery,
)
from app.repositories.protocol import MemoryRepository


class ReviewQueueService:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def get_queue(
        self, request: ReviewQueueQuery, document_keys: list[str] | None = None
    ) -> ReviewQueue:
        return await anyio.to_thread.run_sync(self._get_queue, request, document_keys)

    async def revalidate(
        self,
        key: str,
        reviewer: str,
        evidence: list[str],
        confidence: ConfidenceScore | None,
    ) -> MemoryRevalidationResult:
        validation = MemoryValidation(
            reviewer=reviewer,
            checked_at=datetime.now(UTC),
            evidence=evidence,
        )
        return await anyio.to_thread.run_sync(
            self.repository.revalidate_memory,
            key,
            validation,
            confidence,
        )

    async def resolve_conflict(
        self,
        source_key: str,
        target_key: str,
        reviewer: str,
        action: ADRResolutionAction,
        rationale: str,
    ) -> ADRConflictResolution:
        decision = ADRResolutionDecision(
            action=action,
            rationale=rationale,
            resolved_by=reviewer,
            resolved_at=datetime.now(UTC),
        )
        return await anyio.to_thread.run_sync(
            self.repository.resolve_adr_conflict,
            source_key,
            target_key,
            decision,
        )

    def _get_queue(
        self, request: ReviewQueueQuery, document_keys: list[str] | None = None
    ) -> ReviewQueue:
        scope = {"document_keys": document_keys} if document_keys is not None else {}
        review_date = request.as_of or datetime.now(UTC).date()
        return ReviewQueue(
            review_date=review_date,
            overdue_validations=self.repository.list_overdue_validations(
                review_date,
                request.limit,
                request.validation_offset,
                **scope,
            ),
            unresolved_adr_conflicts=self.repository.list_adr_conflicts(
                ADRConflictListQuery(
                    as_of=request.as_of,
                    limit=request.limit,
                    offset=request.conflict_offset,
                ),
                **scope,
            ),
        )
