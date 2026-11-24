import re
from collections import Counter
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import NamedTuple

import anyio

from app.domain.architecture import ArchitectureSearch, ArchitectureTrace, Project
from app.domain.context_usage import (
    CheckedContextAnswer,
    CheckedExcerptAnswer,
    ContextClaim,
    ContextClaimCheck,
    ContextConflict,
    ContextEvidence,
    ContextExcerpt,
    ContextItem,
    ContextPackage,
    ContextQuery,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import ADRConflictQuery, ArchitecturalDecisionRecord, MemoryDocument, markdown_adr_status
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
_STOP = set("what how why is are to of a the for an and which do does can in as on show tell me current tests test service goal happens every where behavior record records".split())


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


class DecisionParts(NamedTuple):
    decision: str
    rationale: str
    consequences: str
    code_evidence: str = ""


def decision_parts(record: MemoryDocument, day: date) -> DecisionParts | None:
    if (record.kind != "adr" or not record.key
            or record.effective_date and record.effective_date > day
            or record.expiration_date and day >= record.expiration_date):
        return None
    status = record.status.strip().casefold() if record.status else None
    if isinstance(record, ArchitecturalDecisionRecord):
        if status == "accepted" or status == "superseded" and record.expiration_date:
            return DecisionParts(record.decision, record.rationale, record.consequences)
        return None

    # Older imported ADRs preserved the Markdown document and its status line.
    parts = re.split(r"(?m)^##[ \t]+([^\n]+)\n", record.content.replace("\r\n", "\n"))
    status = status or markdown_adr_status(parts[0])
    if status != "accepted" and not (status == "superseded" and record.expiration_date):
        return None
    sections = {heading.strip().casefold(): body.strip() for heading, body in zip(parts[1::2], parts[2::2])}
    if not sections.get("decision"):
        return None
    return DecisionParts(
        sections["decision"], sections.get("rationale", sections.get("context", "")),
        sections.get("consequences", ""), sections.get("evidence from code", ""),
    )


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n+|(?<=[.!?])\s+", text) if part.strip()]


def relevant_excerpt(text: str, terms: set[str], title: str, *, prefer_link: bool = False) -> str:
    passages = _sentences(text)
    if not passages:
        return ""
    title_terms = terms.intersection(words(title))

    def score(part: str) -> tuple[bool, int, int]:
        matches = terms.intersection(words(part))
        linked = bool(matches and prefer_link and re.search(r"\[[^\]]+\]\([^)\s]+\)", part))
        return linked, len(matches & title_terms), len(matches)

    best = max(passages, key=score)
    return _short(best) if terms.intersection(words(best)) else ""


def _cited_text(package: ContextPackage) -> dict[str, str]:
    excerpts = {
        item.citation_id: item.text
        for group in _GROUPS.values() for item in getattr(package, group)
    }
    for conflict in package.conflicts:
        for key, snippet in ((conflict.original_key, conflict.original_decision_excerpt),
                             (conflict.competing_key, conflict.competing_decision_excerpt)):
            identifier = f"records:{key}"
            if snippet and identifier in excerpts:
                excerpts[identifier] += "\n" + snippet

    return excerpts


def _verbatim_excerpts(source: str) -> list[str]:
    return [
        text for sentence in _sentences(source)
        if (text := re.sub(r"^(?:Rationale|Consequences|Code evidence):\s*", "", sentence))
        and not text.startswith("…") and not text.endswith("…")
    ]


def _quote(text: str) -> str:
    return " ".join(text.split())


def context_excerpts(package: ContextPackage) -> list[ContextExcerpt]:
    """Attach content-addressed IDs to excerpts eligible for exact citation checks."""
    rows: dict[str, ContextExcerpt] = {}
    for citation, source in _cited_text(package).items():
        for text in _verbatim_excerpts(source):
            if len(text) > 2000:
                continue
            identifier = sha256(f"{citation}\0{text}".encode()).hexdigest()[:24]
            rows[identifier] = ContextExcerpt(excerpt_id=identifier, citation_id=citation, text=text)
    return list(rows.values())


def check_cited_claims(package: ContextPackage, claims: list[ContextClaim]) -> list[ContextClaimCheck]:
    excerpts = {
        citation: {_quote(text) for text in _verbatim_excerpts(source)}
        for citation, source in _cited_text(package).items()
    }
    return [
        ContextClaimCheck(
            claim=claim,
            status="missing_citation" if not claim.citations
            else "unknown_citation" if set(claim.citations) - excerpts.keys()
            else "supported" if all(_quote(claim.text) in excerpts[cite] for cite in claim.citations)
            else "unsupported_text",
        )
        for claim in claims
    ]


def finalize_context_answer(
    package: ContextPackage, claims: list[ContextClaim],
) -> CheckedContextAnswer:
    """Separate verified excerpts from candidate claims. Resolve only unique bare keys."""
    groups = (getattr(package, group) for group in _GROUPS.values())
    aliases: dict[str, set[str]] = {}
    for items in groups:
        for item in items:
            aliases.setdefault(item.key, set()).add(item.citation_id)

    def canonical_id(citation: str) -> str:
        matches = aliases.get(citation, set())
        return next(iter(matches)) if len(matches) == 1 else citation

    canonical = [
        ContextClaim(
            text=claim.text,
            citations=list(dict.fromkeys(canonical_id(cite) for cite in claim.citations)),
        )
        for claim in claims
    ]
    checks = check_cited_claims(package, canonical)
    return CheckedContextAnswer(
        findings=[check.claim for check in checks if check.status == "supported"],
        flagged_claims=[check for check in checks if check.status != "supported"],
    )


def finalize_excerpt_answer(package: ContextPackage, excerpt_ids: list[str]) -> CheckedExcerptAnswer:
    available = {excerpt.excerpt_id: excerpt for excerpt in context_excerpts(package)}
    claims = []
    unknown = []
    for identifier in dict.fromkeys(excerpt_ids):
        excerpt = available.get(identifier)
        if excerpt is None:
            unknown.append(identifier)
        else:
            claims.append(ContextClaim(text=excerpt.text, citations=[excerpt.citation_id]))
    checked = finalize_context_answer(package, claims)
    return CheckedExcerptAnswer(
        findings=checked.findings, flagged_claims=checked.flagged_claims, unknown_excerpt_ids=unknown,
    )


class ContextPackageService:
    def __init__(self, graph: ArchitectureRepository, memory: MemoryRepository):
        self.graph = graph
        self.memory = memory

    async def build(self, query: ContextQuery) -> BuiltContext:
        return await anyio.to_thread.run_sync(self._build, query)

    async def check_claims(self, query: ContextQuery, claims: list[ContextClaim]) -> list[ContextClaimCheck]:
        package, _, _ = await self.build(query)
        return check_cited_claims(package, claims)

    async def finalize_answer(
        self, query: ContextQuery, claims: list[ContextClaim],
    ) -> CheckedContextAnswer:
        package, _, _ = await self.build(query)
        return finalize_context_answer(package, claims)

    async def list_excerpts(self, query: ContextQuery) -> list[ContextExcerpt]:
        package, _, _ = await self.build(query)
        return context_excerpts(package)

    async def finalize_excerpts(self, query: ContextQuery, excerpt_ids: list[str]) -> CheckedExcerptAnswer:
        package, _, _ = await self.build(query)
        return finalize_excerpt_answer(package, excerpt_ids)

    def _build(self, query: ContextQuery) -> BuiltContext:
        day = query.as_of or datetime.now(UTC).date()
        inventory = None
        scoped = None
        document_keys = None
        terms = set(words(query.question)) - _STOP
        if query.project_key:
            project = self.graph.read_entity(query.project_key)
            if not isinstance(project, Project):
                raise ArchitectureGraphError("Project not found")
            terms.difference_update(words(project.name))
            scope = self.graph.project_scope(query.project_key, include_details=False)
            inventory = scope.entities
            scoped = {item.key for item in inventory}
            linked = sorted({node.memory_key or node.key for node in inventory
                             if node.entity_type == "ADR" or node.memory_key})
            document_keys = [doc.key for doc in self.memory.project_documents(
                query.project_key, scope.repository_names, linked, "", None,
            )]
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
        implicit = not seeds
        if implicit:
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
                if record is not None and decision_parts(record, day):
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
            lexical = {}
            for record in self.memory.search_question_documents(
                " ".join(sorted(terms)), day, 8, adrs_only=True, **filters,
            ):
                if decision_parts(record, day):
                    lexical.setdefault(record.key, records.get(record.key, (record, "Matched document search")))
            records = {**lexical, **records} if implicit else {**records, **lexical}

        ordered = {}
        conflicts = {}
        scope_filter = {"allowed_document_keys": document_keys} if document_keys is not None else {}
        for key, (record, reason) in records.items():
            ordered.setdefault(key, (record, reason))
            if not isinstance(record, ArchitecturalDecisionRecord) or record.conflict_scope is None:
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
                if isinstance(other, ArchitecturalDecisionRecord) and decision_parts(other, day):
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
            parts = decision_parts(record, day)
            full = {"title": record.title, "decision": parts.decision, "rationale": parts.rationale,
                    "consequences": parts.consequences, "content": record.content}
            baseline_documents.append(full)
            excerpt = _short(parts.decision)
            if rationale and parts.rationale:
                excerpt = _short(parts.rationale)
                if record.key in conflict_keys:
                    excerpt = f"{_short(parts.decision)}\nRationale: {excerpt}"
            if query.include_full_adrs:
                excerpt = record.content or "\n".join(full.values())
                if record.key in conflict_keys and parts.decision not in excerpt:
                    excerpt = f"{parts.decision}\n{excerpt}"
            else:
                for label, value in (("Consequences", parts.consequences), ("Code evidence", parts.code_evidence)):
                    if extra := relevant_excerpt(value, terms, record.title, prefer_link=label == "Code evidence"):
                        excerpt += f"\n{label}: {extra}"
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
