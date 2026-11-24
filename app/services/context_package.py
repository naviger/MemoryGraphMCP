import re
from collections import Counter
from datetime import UTC, date, datetime
from typing import NamedTuple

import anyio

from app.domain.architecture import ArchitectureSearch, ArchitectureTrace, Project
from app.domain.context_usage import (
    ContextConflict,
    ContextEvidence,
    ContextItem,
    ContextPackage,
    ContextQuery,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import ADRConflictQuery, ArchitecturalDecisionRecord
from app.repositories.architecture_protocol import ArchitectureRepository
from app.repositories.protocol import MemoryRepository
from app.services.token_usage import estimate_tokens, words

_ANCHORS = {"Goal", "DomainConcept", "ApiEndpoint"}
_GROUPS = {"ADR": "relevant_adrs", "Constraint": "relevant_constraints",
           "Service": "relevant_services", "TestSuite": "relevant_tests"}
_RELATED = {
    "implements", "implemented_by", "constrains", "constrained_by", "validates",
    "validated_by", "supports_goal", "supported_by", "uses", "used_by", "references",
    "referenced_by", "supersedes", "superseded_by",
}
_STOP = set("what how why is are to of a the for an and which do does can in as on show tell me current tests test service goal".split())


class BuiltContext(NamedTuple):
    package: ContextPackage
    baseline: int | None
    entities: int


def _short(value: str, limit: int = 260) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _conflict_difference(original: str, competing: str) -> dict[str, str]:
    original, competing = (" ".join(text.split()) for text in (original, competing))
    if original == competing:
        return {}
    # Ignore punctuation while locating a word-level difference, but keep the
    # verbatim wording from each decision in the displayed excerpts.
    original_words, competing_words = (
        list(re.finditer(r"\w+", text)) for text in (original, competing)
    )
    shared = next(
        (index for index, (left, right) in enumerate(zip(original_words, competing_words))
         if left.group() != right.group()),
        min(len(original_words), len(competing_words)),
    )
    if shared == len(original_words) == len(competing_words):
        # If punctuation is the only difference, preserve it.
        original_offset = competing_offset = next(
            (index for index, (left, right) in enumerate(zip(original, competing))
             if left != right),
            min(len(original), len(competing)),
        )
    else:
        original_offset = original_words[shared].start() if shared < len(original_words) else len(original)
        competing_offset = competing_words[shared].start() if shared < len(competing_words) else len(competing)
    if max(original_offset, competing_offset) < 200 and _short(original) != _short(competing):
        return {}

    def excerpt(text: str, offset: int) -> str:
        start = max(0, offset - 40)
        prefix = "…" if start else ""
        suffix = " [end of decision]" if offset == len(text) else ""
        return prefix + _short(text[start:] + suffix, 260 - len(prefix))

    return {
        "original_decision_excerpt": excerpt(original, original_offset),
        "competing_decision_excerpt": excerpt(competing, competing_offset),
    }


def _applicable(record: object, day: date) -> bool:
    return isinstance(record, ArchitecturalDecisionRecord) and bool(
        record.key
        and (record.status == "accepted" or record.status == "superseded" and record.expiration_date)
        and (record.effective_date is None or record.effective_date <= day)
        and (record.expiration_date is None or day < record.expiration_date)
    )


class ContextPackageService:
    def __init__(self, graph: ArchitectureRepository, memory: MemoryRepository):
        self.graph = graph
        self.memory = memory

    async def build(self, query: ContextQuery) -> BuiltContext:
        return await anyio.to_thread.run_sync(self._build, query)

    def _build(self, query: ContextQuery) -> BuiltContext:
        day = query.as_of or datetime.now(UTC).date()
        inventory = None
        scoped = None
        document_keys = None
        if query.project_key:
            if not isinstance(self.graph.read_entity(query.project_key), Project):
                raise ArchitectureGraphError("Project not found")
            scope = self.graph.project_scope(query.project_key)
            inventory = scope.entities
            scoped = {item.key for item in inventory}
            linked = sorted({node.memory_key or node.key for node in inventory
                             if node.entity_type == "ADR" or node.memory_key})
            document_keys = [doc.key for doc in self.memory.project_documents(
                query.project_key, scope.repository_names, linked, "", None,
            )]
        terms = set(words(query.question)) - _STOP
        seeds = {}
        for key, kind in ((query.entity, None), (query.adr, "ADR"), (query.goal, "Goal"), (query.service, "Service")):
            if not key:
                continue
            found = [self.graph.read_entity(key)]
            if kind == "ADR" and found[0] is None:
                found = self.graph.find_memory_entities(key)
            node = next((node for node in found if node is not None
                         and (not kind or node.entity_type == kind)
                         and node.index_state != "stale"
                         and (scoped is None or node.key in scoped)), None)
            if node is None:
                raise ArchitectureGraphError("An anchor was not found in the selected scope")
            seeds[node.key] = node
        if not seeds:
            if inventory is None:
                hits = [
                    node for term in sorted(terms, key=lambda word: (-len(word), word))[:8]
                    for node in self.graph.search(ArchitectureSearch(query=term, limit=25))
                ]
            else:
                hits = inventory
            ranked = {}
            for item in hits:
                if item.entity_type in _GROUPS or item.entity_type in _ANCHORS:
                    matches = terms.intersection(words(item.name))
                    if matches:
                        ranked[item.key] = (-len(matches), item.name.casefold())
            for key in sorted(ranked, key=lambda key: (*ranked[key], key))[:4]:
                node = self.graph.read_entity(key)
                if node and node.index_state != "stale":
                    seeds[key] = node

        package = ContextPackage(
            as_of=day,
            summary="Dated architecture evidence for the question.",
        )
        candidates = {key: (node, "Matched graph anchor") for key, node in seeds.items()}
        for seed in seeds.values():
            for path in self.graph.trace(ArchitectureTrace(
                key=seed.key, depth=1, limit=35, prioritize=True, entity_types=list(_GROUPS),
            )):
                node, link = path.entity, path.relationships[0]
                if (link.relationship_type in _RELATED and node.index_state != "stale"
                        and (scoped is None or node.key in scoped)):
                    previous = candidates.get(node.key)
                    if previous is None or previous[1] == "Matched graph anchor":
                        candidates[node.key] = (
                            node, _short(f"{seed.name} {link.relationship_type} {node.name} ({link.source})", 150),
                        )

        graph_items = []
        records = {}
        for node, reason in candidates.values():
            group = _GROUPS.get(node.entity_type)
            if not group:
                continue
            evidence = ContextEvidence(key=node.key, citation=reason)
            item = ContextItem(key=node.key, title=_short(node.name, 100),
                               text=_short(node.description, 150))
            if node.entity_type == "ADR":
                record = self.memory.read_document(node.memory_key or node.key)
                if _applicable(record, day):
                    records.setdefault(record.key, (record, reason))
                continue
            elif node.entity_type == "Constraint":
                if not node.rule or node.enforcement_status in {None, "retired", "proposed"}:
                    continue
                item.text = _short(node.rule)
            label = "Current index" if node.index_state else "Current graph"
            path = f". {node.source_path}" if node.source_path else ""
            evidence.citation = _short(f"{label}: {reason}{path}", 150)
            evidence.code_location = node.code_location()
            graph_items.append((group, item, evidence))

        if terms:
            filters = {"document_keys": document_keys} if document_keys is not None else {}
            for record in self.memory.search_question_documents(
                " ".join(sorted(terms)), day, 8, adrs_only=True, **filters,
            ):
                if _applicable(record, day):
                    records.setdefault(record.key, (record, "Matched document search"))

        ordered = {}
        conflicts = {}
        scope_filter = {"allowed_document_keys": document_keys} if document_keys is not None else {}
        for key, (record, reason) in records.items():
            ordered.setdefault(key, (record, reason))
            if record.conflict_scope is None:
                continue
            for pair in self.memory.find_adr_conflicts(
                ADRConflictQuery(key=key, as_of=day, limit=100), **scope_filter,
            ):
                other_key = pair.competing.key
                identity = tuple(sorted((key, other_key)))
                if identity in conflicts:
                    continue
                other, other_reason = records.get(other_key, (None, "Unresolved ADR conflict"))
                if other is None:
                    other = self.memory.read_document(other_key)
                if _applicable(other, day):
                    ordered.setdefault(other_key, (other, other_reason))
                    conflicts[identity] = ContextConflict(
                        original_key=key, competing_key=other_key,
                        overlap_start=pair.overlap_start, overlap_end=pair.overlap_end,
                        **_conflict_difference(record.decision, other.decision),
                    )

        conflict_keys = {key for identity in conflicts for key in identity}
        adr_items = {}
        baseline_documents = []
        rationale = bool({"why", "rationale"}.intersection(words(query.question)))
        for record, reason in ordered.values():
            full = {"title": record.title, "decision": record.decision, "rationale": record.rationale,
                    "consequences": record.consequences, "content": record.content}
            baseline_documents.append(full)
            excerpt = _short(record.decision)
            if rationale:
                excerpt = _short(record.rationale)
                if record.key in conflict_keys:
                    excerpt = f"{_short(record.decision)}\nRationale: {excerpt}"
            if query.include_full_adrs:
                excerpt = record.content or "\n".join(full.values())
                if record.key in conflict_keys and record.decision not in excerpt:
                    excerpt = f"{record.decision}\n{excerpt}"
            item = ContextItem(key=record.key, title=_short(record.title, 100),
                               destination="records", text=excerpt)
            citation = next((item for validation in reversed(record.validation_history)
                             if validation.checked_at.date() <= day
                             for item in validation.evidence), record.source_id or record.source)
            adr_items[record.key] = ("relevant_adrs", item, ContextEvidence(
                key=record.key, citation=_short(citation or reason, 150),
            ))

        entries = [*adr_items.values(), *graph_items]
        package.budget_excluded = dict(Counter(group for group, _, _ in entries))
        if conflicts:
            package.budget_excluded["conflicts"] = len(conflicts)
        supplied = 0
        supplied_adrs = set()
        for conflict in conflicts.values():
            missing = [key for key in (conflict.original_key, conflict.competing_key)
                       if key not in supplied_adrs]
            if self._fit(package, query.max_context_tokens,
                         *(adr_items[key] for key in missing), ("conflicts", conflict, None)):
                supplied_adrs.update(missing)
        supplied += len(supplied_adrs)
        unrelated_adrs = (entry for key, entry in adr_items.items() if key not in conflict_keys)
        for entry in (*unrelated_adrs, *graph_items):
            supplied += self._fit(package, query.max_context_tokens, entry)
        if not supplied:
            package.summary = ("No matching evidence fit the budget." if entries
                               else "No applicable evidence matched the question.")
        baseline = estimate_tokens(baseline_documents) if baseline_documents else None
        return BuiltContext(package, baseline, supplied)

    @staticmethod
    def _fit(
        package: ContextPackage, budget: int,
        *entries: tuple[str, ContextItem | ContextConflict, ContextEvidence | None],
    ) -> bool:
        for group, item, evidence in entries:
            getattr(package, group).append(item)
            if evidence is not None:
                package.evidence.append(evidence)
            package.budget_excluded[group] -= 1
            if not package.budget_excluded[group]:
                del package.budget_excluded[group]
        if estimate_tokens(package) <= budget:
            return True
        for group, _, evidence in reversed(entries):
            package.budget_excluded[group] = package.budget_excluded.get(group, 0) + 1
            getattr(package, group).pop()
            if evidence is not None:
                package.evidence.pop()
        return False
