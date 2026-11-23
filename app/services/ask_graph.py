from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, date, datetime
from functools import partial

import anyio

from app.domain.architecture import EntityType
from app.domain.ask_graph import AnswerCitation, AnswerFinding, AskGraphAnswer, AskGraphQuery
from app.domain.models import (
    ADRConflict,
    ADRConflictQuery,
    ADRConflictRecord,
    ArchitecturalDecisionRecord,
    MemoryDocument,
)
from app.domain.review_details import ProjectOverview
from app.repositories.protocol import MemoryRepository
from app.services.review_details import ReviewDetailService

_IGNORE = set(
    "a an and are as at be by can could current did do does for from get give how i in is it its "
    "me of on or our please should show tell that the their this to use was were what when where "
    "which who why will with would you about currently strategy policy approach rationale".split()
)
_LINK_INTENTS = {
    "test": "TestSuite",
    "tests": "TestSuite",
    "validate": "TestSuite",
    "validates": "TestSuite",
    "service": "Service",
    "services": "Service",
    "file": "SourceFile",
    "files": "SourceFile",
    "code": "SourceFile",
}
_WORDS = re.compile(r"[\w-]+")
_LIMIT = 8

_COUNT_TARGETS: dict[str, tuple[EntityType, str]] = {
    "projects": ("Project", "projects"),
    "repositories": ("Repository", "repositories"),
    "repos": ("Repository", "repositories"),
    "source repositories": ("Repository", "repositories"),
    "source repos": ("Repository", "repositories"),
}
_COUNT_QUESTION = re.compile(
    r"(?:how many |(?:what is the |show me the )?(?:count|number) of )"
    r"(projects|(?:source )?(?:repositories|repos))(?: (?:are there|are stored|are|exist))?"
    r"(?: in (?:the |this |our )?(?:architecture )?graph)?"
)
_INVENTORY_QUESTION = re.compile(
    r"(?:show(?: me)?(?: what)?|list|what|which) (?:are )?(?:the )?(?:concrete )?"
    r"(?:node|entity) types(?: (?:are there|are|exist))?"
    r"(?: in (?:the |this |our )?(?:architecture )?graph)?"
)


def _claim_key(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".").strip()


def _add_claim(
    claims: dict[str, AnswerFinding],
    text: str,
    citations: list[AnswerCitation],
) -> None:
    key = _claim_key(text)
    if not key:
        return
    finding = claims.get(key)
    if finding is None:
        finding = AnswerFinding(text=text, citations=citations[:1])
        claims[key] = finding
    existing = {(item.destination, item.key) for item in finding.citations}
    for citation in citations:
        identity = (citation.destination, citation.key)
        if identity not in existing:
            finding.citations.append(citation)
            existing.add(identity)


class AskGraphService:
    def __init__(self, repository: MemoryRepository, details: ReviewDetailService) -> None:
        self.repository = repository
        self.details = details

    async def answer(
        self, query: AskGraphQuery, project: ProjectOverview | None = None
    ) -> AskGraphAnswer:
        as_of = query.as_of or datetime.now(UTC).date()
        question = " ".join(query.question.casefold().rstrip("?.!").split())
        count_match = _COUNT_QUESTION.fullmatch(question)
        if count_match or _INVENTORY_QUESTION.fullmatch(question):
            target = _COUNT_TARGETS[count_match[1]] if count_match else None
            return await self._inventory(as_of, target, project)
        words = set(_WORDS.findall(query.question.casefold()))
        wanted_links = {_LINK_INTENTS[word] for word in words if word in _LINK_INTENTS}
        topics = words - _IGNORE - _LINK_INTENTS.keys()
        terms = sorted(topics or words - _IGNORE)
        if not terms:
            return AskGraphAnswer(
                as_of=as_of,
                summary="The question needs a topic to search.",
                findings=[],
                conflicts=[],
                gaps=["Name a decision, service, or architecture topic."],
            )
        search = self.repository.search_question_documents
        if project is not None:
            search = partial(search, document_keys=[doc.key for doc in project.documents])
        records = await anyio.to_thread.run_sync(
            search,
            " ".join(terms),
            as_of,
            _LIMIT,
        )
        if not records:
            return AskGraphAnswer(
                as_of=as_of,
                summary="I found no applicable records matching this question.",
                findings=[],
                conflicts=[],
                gaps=["The corpus may lack records for this topic or for the selected date."],
            )

        rationale_requested = bool(words & {"why", "rationale"})
        answers: dict[str, AnswerFinding] = {}
        context: dict[str, AnswerFinding] = {}
        code_links: dict[str, AnswerFinding] = {}
        conflicts: dict[tuple[str, ...], ADRConflict] = {}
        gaps: list[str] = []
        for record in records:
            if record.key is None:
                continue
            cite = AnswerCitation(key=record.key, title=record.title)
            gaps.extend(self._evidence_gaps(record, as_of))
            if isinstance(record, ArchitecturalDecisionRecord):
                if rationale_requested:
                    _add_claim(answers, record.rationale, [cite])
                    _add_claim(context, record.decision, [cite])
                else:
                    _add_claim(answers, record.decision, [cite])
                if record.conflict_scope is None:
                    gaps.append(f"{record.title} lacks a scope for automated conflict checks.")
                else:
                    pairs = await anyio.to_thread.run_sync(
                        self.repository.find_adr_conflicts,
                        ADRConflictQuery(key=record.key, as_of=as_of, limit=10),
                    )
                    for pair in pairs:
                        identity = tuple(sorted((pair.original.key, pair.competing.key)))
                        conflicts.setdefault(identity, pair)
            else:
                _add_claim(context, self._excerpt(record.content, terms), [cite])

            if wanted_links:
                detail = await self.details.get_record(record.key)
                for link in detail.links if detail else ():
                    if link.entity_type in wanted_links:
                        relation = " · ".join(step.replace("_", " ") for step in link.relationships)
                        _add_claim(
                            code_links,
                            f"{link.name} ({relation}).",
                            [
                                AnswerCitation(
                                    key=link.key,
                                    title=link.name,
                                    destination=link.destination,
                                ),
                                cite,
                            ],
                        )

        if wanted_links:
            gaps.append(
                "Code and test links reflect the current graph. "
                "Their historical validity is unknown."
                if code_links
                else "I found no matching code or test links from the retrieved records."
            )

        # Conflict counterparts may be outside the retrieval limit. Include both sides.
        for pair in conflicts.values():
            for record in (pair.original, pair.competing):
                gaps.extend(self._evidence_gaps(record, as_of))
                cite = AnswerCitation(key=record.key, title=record.title)
                if rationale_requested:
                    _add_claim(answers, record.rationale, [cite])
                    _add_claim(context, record.decision, [cite])
                else:
                    _add_claim(answers, record.decision, [cite])

        # A matching note corroborates the decision instead of creating a second claim.
        for key in answers.keys() & context.keys():
            _add_claim(answers, context[key].text, context.pop(key).citations)

        findings = [*code_links.values(), *answers.values(), *context.values()]
        if conflicts:
            summary = "The records support competing decisions for the selected date."
        elif not findings:
            summary = "I found matching records, but none contained a usable answer."
        else:
            summary = ""
        return AskGraphAnswer(
            as_of=as_of,
            summary=summary,
            findings=findings,
            conflicts=list(conflicts.values()),
            gaps=list(dict.fromkeys(gaps)),
        )

    async def _inventory(
        self, as_of: date, target: tuple[EntityType, str] | None,
        project: ProjectOverview | None = None,
    ) -> AskGraphAnswer:
        gaps: list[str] = []
        architecture = self.details.architecture
        if architecture is None:
            summary = "The architecture graph is unavailable."
        elif as_of != datetime.now(UTC).date():
            summary = "A point-in-time node count is unavailable for the selected date."
            gaps.append("Architecture nodes do not record a historical inventory.")
        else:
            if project is not None:
                source_only = target is not None and target[0] == "Repository"
                counts = Counter(
                    entity.entity_type for entity in project.entities
                    if not (
                        source_only and entity.entity_type == "Repository"
                        and entity.role != "source"
                    )
                )
                counts["Project"] = 1
                subject = project.name
            else:
                subject = "The architecture graph"
                counts = await anyio.to_thread.run_sync(
                    partial(
                        architecture.count_by_entity_type,
                        source_repositories_only=target is not None and target[0] == "Repository",
                    )
                )
            if target:
                entity_type, plural = target
                total = counts.get(entity_type, 0)
                label = entity_type.lower() if total == 1 else plural
                if entity_type == "Repository":
                    label = f"source {label}"
                summary = f"{subject} contains {total} {label}."
            elif counts:
                totals = ", ".join(f"{kind} ({counts[kind]})" for kind in sorted(counts))
                summary = f"Stored architecture node types and counts: {totals}."
            else:
                summary = "The architecture graph has no stored nodes."
        return AskGraphAnswer(as_of=as_of, summary=summary, findings=[], conflicts=[], gaps=gaps)

    @staticmethod
    def _evidence_gaps(record: MemoryDocument | ADRConflictRecord, as_of: date) -> list[str]:
        gaps = []
        if record.effective_date is None:
            gaps.append(f"{record.title} has no recorded effective date.")
        if not any(item.checked_at.date() <= as_of for item in record.validation_history):
            gaps.append(f"{record.title} has no cited validation recorded by the selected date.")
        return gaps

    @staticmethod
    def _excerpt(content: str, terms: list[str]) -> str:
        passages = [
            part.strip() for part in re.split(r"\n+|(?<=[.!?])\s+", content) if part.strip()
        ]
        if not passages:
            return ""
        passage = max(passages, key=lambda part: sum(term in part.casefold() for term in terms))
        return passage if len(passage) <= 600 else passage[:597].rstrip() + "…"
