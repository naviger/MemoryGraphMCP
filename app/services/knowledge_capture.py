"""Project-scoped capture. Inferred links remain review evidence."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from app.domain.architecture import Constraint, Goal, Observation, Project, architecture_key
from app.domain.exceptions import ArchitectureGraphError
from app.domain.knowledge_capture import (
    CaptureEvent,
    CaptureSuggestion,
    ChangeReflectionInput,
    CommitCapture,
)
from app.repositories.architecture_protocol import ArchitectureRepository
from app.repositories.protocol import MemoryRepository

_ADR_ID = re.compile(r"\bADR[-_ ]?\d{1,6}\b", re.I)
_DECLARATIONS = (
    ("service", re.compile(r"^\s*class\s+\w+Service\b")),
    ("repository", re.compile(r"^\s*class\s+\w+(?:Repository|Repo)\b")),
    ("endpoint", re.compile(r"^\s*@\w+(?:\.\w+)*\.(?:get|post|put|patch|delete|route|api_route)\(")),
    ("test", re.compile(r"^\s*(?:async\s+)?def\s+test_\w+\b")),
    ("constraint", re.compile(r"\bConstraint(?:Input)?\s*\(")),
)
_COLLECTION = re.compile(r'''\b(?:db|database)\.collection\(["']([\w-]+)["']\)''')
_FILE = re.compile(r"^\+\+\+ b/(.+)$")


def diff_additions(diff: str) -> dict[str, str]:
    chunks: dict[str, list[str]] = {}
    path = None
    for line in diff.splitlines():
        if line.startswith(("diff --git ", "+++ /dev/null")):
            path = None
        match = _FILE.match(line)
        if match:
            path = match[1]
            chunks.setdefault(path, [])
        elif path and line.startswith("+") and not line.startswith("+++ "):
            chunks[path].append(line[1:])
    return {path: "\n".join(lines) for path, lines in chunks.items()}


def _fingerprint(*values: object) -> str:
    payload = json.dumps(values, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


class KnowledgeCaptureService:
    def __init__(self, graph: ArchitectureRepository, memory: MemoryRepository):
        self.graph = graph
        self.memory = memory

    def store(self, event: CaptureEvent, *, key: str | None = None, actor: str | None = None) -> Observation:
        if not isinstance(self.graph.read_entity(event.project_key), Project):
            raise ArchitectureGraphError("Capture requires an existing project")
        key = key or architecture_key("capture", event.project_key, event.source, event.event_id)
        existing = self.graph.read_entity(key)
        if isinstance(existing, Observation):
            if existing.metadata.get("capture_project") != event.project_key:
                raise ArchitectureGraphError("Capture identity is already in use")
            return existing
        related = list(dict.fromkeys([event.project_key, *event.related_keys]))[:100]
        return self.graph.add_observation(Observation(
            key=key, name=event.summary[:120], source=event.source, summary=event.summary,
            description=event.summary,
            confidence=0.9 if event.evidence else 0.7, timestamp=event.timestamp,
            related_entity_keys=related, recorded_by=actor,
            evidence=event.evidence, suggestions=event.suggestions,
            metadata={"capture_project": event.project_key, "event_id": event.event_id},
        ), replay=True)

    def commit(self, project: str, repository: str, commit: CommitCapture) -> Observation:
        return self.change(
            project, "commit", f"{repository}:{commit.sha}", commit.timestamp, commit.diff,
            explicit=[repository], paths=commit.changed_files, repository_key=repository,
            why=commit.subject, evidence=[f"git commit {commit.sha}"],
            partial=commit.truncated,
        )

    def reflect(self, request: ChangeReflectionInput, actor: str | None) -> Observation:
        return self.change(
            request.project_key, "agent_reflection",
            _fingerprint(actor, request.model_dump()), datetime.now(UTC), request.diff,
            explicit=request.linked_entities, test_results=request.test_results, actor=actor,
            why=request.rationale,
        )

    def change(
        self, project: str, source: str, event_id: str, timestamp: datetime, diff: str, *,
        explicit: list[str], paths: list[str] | None = None, repository_key: str | None = None,
        why: str = "", evidence: list[str] | None = None, test_results: str = "",
        actor: str | None = None, partial: bool = False,
    ) -> Observation:
        if not isinstance(self.graph.read_entity(project), Project):
            raise ArchitectureGraphError("Capture requires an existing project")
        observation_key = architecture_key("capture", project, source, event_id)
        existing = self.graph.read_entity(observation_key)
        if isinstance(existing, Observation):
            return existing

        scope = self.graph.project_scope(project)
        by_key = {node.key: node for node in scope.entities}
        if set(explicit) - (set(by_key) | {project}):
            raise ArchitectureGraphError("Linked entities must belong to the selected project")
        repo = by_key.get(repository_key)
        if repository_key is not None and (repo is None or repo.role != "source"):
            raise ArchitectureGraphError("The capture repository must be a linked source repository")
        additions = diff_additions(diff)
        changed = set(paths or additions)
        # A bare path from a reflection can match more than one repository. Require explicit
        # repository evidence before linking any ambiguous path.
        repositories_by_path: dict[str, set] = {}
        for node in scope.entities:
            if node.source_path in changed:
                repositories_by_path.setdefault(node.source_path, set()).add(node.repository)
        matched = [
            node for node in scope.entities
            if node.source_path in changed and (
                node.repository == repo.name if repo else len(repositories_by_path[node.source_path]) == 1
            )
        ]
        priority = {"SourceFile": 0, "Service": 1, "Repository": 1, "ApiEndpoint": 1,
                    "TestSuite": 1, "Class": 2}
        matched.sort(key=lambda node: (priority.get(node.entity_type, 3), node.key))
        changed_keys = set(explicit) | {node.key for node in matched}
        adrs = [node for node in scope.entities if node.entity_type == "ADR"]
        services = [node for node in scope.entities if node.entity_type == "Service"]
        collections = [node for node in scope.entities if node.entity_type == "DatabaseCollection"]
        suggestions: dict[tuple, CaptureSuggestion] = {}

        def suggest(left: str, right: str, relation: str, confidence: float, citation: str) -> None:
            identity = (left, right, relation)
            if len(suggestions) < 50 and identity not in suggestions:
                suggestions[identity] = CaptureSuggestion(
                    source_key=left, target_key=right, relationship_type=relation,
                    confidence=confidence, evidence=citation[:1200],
                )

        referenced_adrs = set()
        for path, added in additions.items():
            adr_tokens = {item.casefold() for item in _ADR_ID.findall(added)}
            candidates = [node for node in matched if node.source_path == path]
            for adr in adrs:
                identifiers = [adr.memory_key, adr.key, *_ADR_ID.findall(adr.name)]
                if any(identifier and (
                    identifier.casefold() in adr_tokens
                    or re.search(rf"(?<![\w-]){re.escape(identifier)}(?![\w-])", added, re.I)
                ) for identifier in identifiers):
                    referenced_adrs.add(adr.key)
                    suggest(project, adr.key, "references", 0.7, f"{path} cites {adr.name}")
                    for node in candidates:
                        if node.entity_type in {"Class", "Service"}:
                            suggest(node.key, adr.key, "implements", 0.65, f"{path} cites {adr.name}")
            for node in candidates:
                if node.entity_type == "TestSuite":
                    for service in services:
                        if re.search(rf"\b{re.escape(service.name)}\b", added):
                            suggest(node.key, service.key, "validates", 0.7, f"{path} references {service.name}")
                if node.entity_type == "Repository" and node.role == "persistence":
                    names = set(_COLLECTION.findall(added))
                    for collection in collections:
                        if collection.name in names and collection.repository == node.repository:
                            suggest(node.key, collection.key, "persists_to", 0.85,
                                    f"{path} accesses collection {collection.name}")
        associated = changed_keys | referenced_adrs
        found_adrs = [adr for adr in adrs if adr.key in associated or any(
            link.key in changed_keys for link in adr.decision_links
        )]
        for adr in found_adrs:
            suggest(project, adr.key, "references", 0.8 if adr.key not in referenced_adrs else 0.7,
                    f"Project change is associated with {adr.name}")
        associated.update(adr.key for adr in found_adrs)
        goals = []
        confirming = self.graph.capture_goal_support(
            sorted(associated), [node.key for node in scope.entities if node.entity_type == "Goal"],
        )
        constraints = []
        for candidate in scope.entities:
            if candidate.entity_type == "Goal":
                goal = self.graph.read_entity(candidate.key)
                if isinstance(goal, Goal):
                    supporters = associated.intersection([
                        *goal.adr_keys, *goal.service_keys, *goal.task_keys, *confirming.get(goal.key, []),
                    ])
                    if supporters or goal.key in explicit:
                        goals.append(goal)
                        for key in sorted(supporters):
                            # The changed file may support the same goal as its linked service or ADR.
                            for node in matched:
                                if node.entity_type == "SourceFile":
                                    suggest(node.key, goal.key, "supports_goal", 0.55,
                                            f"Changed alongside {by_key[key].name if key in by_key else project}, a goal supporter")
                            suggest(project, goal.key, "supports_goal", 0.55,
                                    f"Change touches {by_key[key].name if key in by_key else project}, a goal supporter")
            if candidate.entity_type == "Constraint":
                constraint = self.graph.read_entity(candidate.key)
                if isinstance(constraint, Constraint) and (
                    candidate.key in explicit or associated.intersection(
                        [*constraint.constrained_entity_keys, *constraint.test_suite_keys]
                    )
                ):
                    constraints.append(constraint)
        detections = sorted({kind for text in additions.values() for line in text.splitlines()
                             for kind, pattern in _DECLARATIONS if pattern.search(line)})
        if any(_ADR_ID.search(text) for text in additions.values()) or referenced_adrs:
            detections.append("ADR reference")
        file_summary = ", ".join(sorted(changed)[:8]) or "no identified files"
        summary = [
            f"What changed? {len(changed)} file(s): {file_summary[:650]}."
            + (f" Detected additions: {', '.join(detections)}." if detections else ""),
            "Why? " + (
                f"Change title or commit message: {why[:500]}" if why and source != "agent_reflection"
                else why[:500] or "No rationale supplied in the change evidence."
            ),
            "Which ADRs? " + (", ".join(node.name for node in found_adrs)[:500] or "None identified."),
            "Which goals? " + (", ".join(node.name for node in goals)[:500] or "None inferred."),
            "Which constraints? " + (", ".join(node.name for node in constraints)[:500] or "None identified."),
        ]
        if test_results:
            summary.append(f"Test results supplied: {test_results[:650]}")
        if partial:
            summary.append("The change evidence was truncated.")
        return self.store(CaptureEvent(
            source=source, event_id=event_id, project_key=project, timestamp=timestamp,
            summary="\n".join(summary), related_keys=[*explicit, *[node.key for node in matched]],
            evidence=[*(evidence or []), *[f"Changed file: {path[:1100]}" for path in sorted(changed)[:20]]],
            suggestions=list(suggestions.values()),
        ), key=observation_key, actor=actor)
