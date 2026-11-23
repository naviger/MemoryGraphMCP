from datetime import UTC, datetime
from typing import NamedTuple

import anyio

from app.domain.architecture import ArchitectureSearch, ArchitectureTrace, Project
from app.domain.context_usage import ContextEvidence, ContextItem, ContextPackage, ContextQuery
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import ArchitecturalDecisionRecord
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
        if query.project_key:
            if not isinstance(self.graph.read_entity(query.project_key), Project):
                raise ArchitectureGraphError("Project not found")
            inventory = self.graph.project_scope(query.project_key).entities
            scoped = {item.key for item in inventory}
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
            summary="Direct architecture evidence for the question." if seeds
                    else "No graph anchors matched. Name an entity, ADR, goal, or service.",
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

        baseline_documents = {}
        supplied = 0
        for node, reason in candidates.values():
            group = _GROUPS.get(node.entity_type)
            if not group:
                continue
            evidence = ContextEvidence(key=node.key, citation=reason)
            item = ContextItem(key=node.key, title=_short(node.name, 100),
                               text=_short(node.description, 150))
            if node.entity_type == "ADR":
                record = self.memory.read_document(node.memory_key or node.key)
                if not isinstance(record, ArchitecturalDecisionRecord) or record.key in baseline_documents:
                    continue
                if (record.status not in {"accepted", "superseded"}
                        or record.status == "superseded" and not record.expiration_date
                        or record.effective_date and record.effective_date > day
                        or record.expiration_date and day >= record.expiration_date):
                    continue
                full = {"title": record.title, "decision": record.decision, "rationale": record.rationale,
                        "consequences": record.consequences, "content": record.content}
                baseline_documents[record.key] = full
                item.key, item.title, item.destination = record.key, _short(record.title, 100), "records"
                item.text = (record.content or "\n".join(full.values())) if query.include_full_adrs else _short(
                    record.rationale if "why" in words(query.question) else record.decision,
                )
                citation = next((item for validation in reversed(record.validation_history)
                                 for item in validation.evidence), record.source_id or record.source)
                evidence = ContextEvidence(key=record.key, citation=_short(citation or reason, 150))
            elif node.entity_type == "Constraint":
                if not node.rule or node.enforcement_status in {None, "retired", "proposed"}:
                    continue
                item.text = _short(node.rule)
            elif node.source_path:
                evidence.citation = _short(f"{reason}. {node.source_path}", 150)
            items = getattr(package, group)
            items.append(item)
            package.evidence.append(evidence)
            if estimate_tokens(package) > query.max_context_tokens:
                items.pop()
                package.evidence.pop()
            else:
                supplied += 1

        if seeds and not supplied:
            package.summary = "Graph anchors matched, but no applicable evidence fit the scope, date, and token budget."
        baseline = estimate_tokens(list(baseline_documents.values())) if baseline_documents else None
        return BuiltContext(package, baseline, supplied)
