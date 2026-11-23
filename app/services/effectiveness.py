"""Project and repository evidence rollups with explicit denominators."""
from collections import Counter
from datetime import UTC, datetime
from statistics import mean
from typing import Protocol

import anyio

from app.domain.architecture import Project
from app.domain.effectiveness import (
    DevelopmentOutcome,
    EffectivenessQuery,
    EffectivenessReport,
    Estimate,
    OutcomeReceipt,
    Rate,
)
from app.domain.exceptions import ArchitectureGraphError
from app.domain.models import ADRConflictListQuery
from app.repositories.architecture_protocol import ArchitectureRepository
from app.repositories.protocol import MemoryRepository


class EffectivenessGraph(ArchitectureRepository, Protocol):
    def save_development_outcome(self, outcome: DevelopmentOutcome, actor: str) -> OutcomeReceipt: ...
    def effectiveness_graph(self, project, allowed, repositories, repository) -> dict: ...
    def effectiveness_events(self, project, repository, start, end) -> list[dict]: ...


class EffectivenessMemory(MemoryRepository, Protocol):
    def effectiveness_records(self, keys, start, end) -> list[dict]: ...
    def effectiveness_resolutions(self, keys, start, end) -> int: ...


def rate(numerator: int, denominator: int) -> Rate:
    return Rate(numerator=numerator, denominator=denominator,
                percent=round(100 * numerator / denominator, 1) if denominator else None)


def economics(events: list[dict], reports: list[DevelopmentOutcome], price: float | None) -> dict[str, Estimate]:
    retrievals = [row for row in events if row["event_type"] == "retrieval.usage"]
    compared = [row for row in retrievals if row.get("savings_tokens") is not None]
    saved = sum(row["savings_tokens"] for row in compared)
    baseline = sum(row["context_tokens"] + row["savings_tokens"] for row in compared)
    # A model-independent estimator has a 0.5 confidence ceiling. Coverage scales it.
    confidence = round(0.5 * len(compared) / len(retrievals), 2) if compared else None
    result = {
        "token_savings": Estimate(value=saved if compared else None, confidence=confidence, samples=len(compared),
                                 methodology="Sum of comparable full-document baseline minus delivered context estimates."),
        "context_reduction_percent": Estimate(
            value=round(100 * saved / baseline, 1) if baseline else None,
            confidence=confidence if baseline else None, samples=len(compared),
            methodology="Aggregate net token savings divided by the comparable full-context baseline."),
        "model_cost_avoided": Estimate(
            value=round(saved * price / 1_000_000, 6) if compared and price is not None else None,
            confidence=confidence if price is not None else None, samples=len(compared),
            methodology="Net input tokens saved times the supplied USD price per million input tokens."),
    }
    hours = [item for report in reports for item in report.avoided_effort]
    for category in ("rework", "review", "onboarding", "engineering"):
        samples = [item for item in hours if item.category == category or category == "engineering"]
        total = sum(item.hours for item in samples)
        result[f"{category}_hours_avoided"] = Estimate(
            value=round(total, 2) if samples else None,
            confidence=round(sum(item.confidence * item.hours for item in samples) / total, 2) if total else None,
            samples=len(samples), methodology=(
                "Sum of cited, non-overlapping estimates supplied by writers. Confidence is weighted by estimated hours."
            ),
        )
    return result


def _agent_check_outcomes(reports: list[DevelopmentOutcome], events: list[dict]) -> tuple[list[bool], list[int]]:
    families = {}
    for row in events:
        if row["event_type"] != "test.run.finished":
            continue
        run = row["payload"]
        family, _, attempt = run["run_id"].partition(":")
        if attempt.isdigit():
            families.setdefault((run["repository_key"], run["commit"]), {}).setdefault(family, {})[int(attempt)] = run
    first_pass, retries = [], []
    for report in reports:
        if report.actor != "agent":
            continue
        options = [
            runs for runs in families.get((report.repository_key, report.change_id), {}).values()
            if 1 in runs and runs[1]["status"] in {"passed", "failed"}
        ]
        run = min(options, key=lambda attempts: attempts[1]["finished_at"]) if options else None
        if report.first_pass_success is not None:
            first_pass.append(report.first_pass_success)
        elif run:
            first_pass.append(run[1]["status"] == "passed" and run[1].get("build_status") != "failed")
        if report.retries is not None:
            retries.append(report.retries)
        elif run:
            retries.append(sum(attempt > 1 and row["status"] != "cancelled" for attempt, row in run.items()))
    return first_pass, retries


class EffectivenessService:
    def __init__(self, graph: EffectivenessGraph, memory: EffectivenessMemory):
        self.graph = graph
        self.memory = memory

    async def report(self, outcome: DevelopmentOutcome, actor: str) -> OutcomeReceipt:
        return await anyio.to_thread.run_sync(self.graph.save_development_outcome, outcome, actor)

    async def repositories(self, project_key: str) -> dict[str, str]:
        return await anyio.to_thread.run_sync(self._repositories, project_key)

    def _repositories(self, project_key: str) -> dict[str, str]:
        if not isinstance(self.graph.read_entity(project_key), Project):
            raise ArchitectureGraphError("Project not found")
        return {repo.key: repo.name for repo in self.graph.project_source_repositories(project_key)}

    async def summary(self, query: EffectivenessQuery) -> EffectivenessReport:
        return await anyio.to_thread.run_sync(self._summary, query)

    def _summary(self, query: EffectivenessQuery) -> EffectivenessReport:
        project = self.graph.read_entity(query.project_key)
        if not isinstance(project, Project):
            raise ArchitectureGraphError("Project not found")
        repositories = self._repositories(project.key)
        repository = repositories.get(query.repository_key)
        if query.repository_key and not repository:
            raise ArchitectureGraphError("Repository is not linked to this project")
        scope = self.graph.project_scope(project.key)
        state = self.graph.effectiveness_graph(
            project.key, [node.key for node in scope.entities],
            [repository] if repository else list(repositories.values()), repository,
        )
        nodes, links = state["nodes"], state["links"]
        types = Counter(node["type"] for node in nodes)
        by_type = {
            kind: {node["key"] for node in nodes if node["type"] == kind}
            for kind in ("ADR", "Constraint", "Service", "TestSuite")
        }
        active = {node["key"] for node in nodes if node["type"] == "Constraint"
                  and node["enforcement"] in {"unenforced", "partially_enforced", "enforced"}}
        document_keys = {node["memory_key"] for node in nodes if node["memory_key"]}
        for doc in self.memory.project_documents(
            project.key, [repository] if repository else list(repositories.values()), sorted(document_keys), "", None,
        ):
            # Project-only attachments lack repository attribution. A repo gets linked records
            # and records explicitly matched by repository metadata.
            if not repository or doc.key in document_keys or doc.reason.startswith("Source repository: "):
                document_keys.add(doc.key)
        records = self.memory.effectiveness_records(sorted(document_keys), query.start, query.end)
        adr_keys = sorted(row["key"] for row in records if row["kind"] == "adr")
        memory_keys = {node["memory_key"] for node in nodes if node["type"] == "ADR" and node["memory_key"]}
        adr_count = len(set(adr_keys) | memory_keys) + sum(
            node["type"] == "ADR" and not node["memory_key"] for node in nodes
        )

        constraints_with_tests, tests_with_constraints, services_with_adrs = set(), set(), set()
        for link in links:
            source, target = link["source"], link["target"]
            if link["type"] == "validated_by" and source in by_type["Constraint"] and target in by_type["TestSuite"]:
                if source in active:
                    constraints_with_tests.add(source)
                tests_with_constraints.add(target)
            if link["type"] == "implements" and source in by_type["Service"] and target in by_type["ADR"]:
                services_with_adrs.add(source)

        events = self.graph.effectiveness_events(
            project.key, query.repository_key, query.start.isoformat() if query.start else None,
            query.end.isoformat() if query.end else None,
        )
        reports = [DevelopmentOutcome.model_validate(row["outcome"]) for row in events
                   if row["event_type"] == "development.outcome"]
        change_counts = Counter(report.actor for report in reports)
        checked, retry_data = _agent_check_outcomes(reports, events)
        task_results = {}
        task_duration = {}
        for row in events:
            if row["event_type"] in {"task.completed", "task.upserted"} and row["outcome"] == "applied":
                if row["event_type"] == "task.completed" or row["payload"].get("status") in {"done", "cancelled"}:
                    when = datetime.fromisoformat(row["occurred_at"])
                    key = row["task_key"]
                    if key not in task_results or when >= task_results[key][0]:
                        task_results[key] = (
                            when, "abandoned" if row["payload"].get("status") == "cancelled" else "completed",
                        )
        for report in sorted(reports, key=lambda report: report.occurred_at):
            if report.task_key:
                if report.task_key not in task_results or report.occurred_at >= task_results[report.task_key][0]:
                    task_results[report.task_key] = (report.occurred_at, report.task_outcome)
                if report.task_started_at and report.task_outcome == "completed":
                    task_duration[report.task_key] = (report.occurred_at - report.task_started_at).total_seconds() / 3600
        finished = Counter(status for _, status in task_results.values())
        elapsed = [hours for key, hours in task_duration.items() if task_results[key][1] == "completed"]

        unresolved, offset = 0, 0
        while adr_keys:
            batch = self.memory.list_adr_conflicts(
                ADRConflictListQuery(limit=100, offset=offset), document_keys=adr_keys,
            )
            unresolved += sum(
                (query.end is None or pair.overlap_start is None or pair.overlap_start <= query.end)
                and (query.start is None or pair.overlap_end is None or pair.overlap_end > query.start)
                for pair in batch
            )
            if len(batch) < 100:
                break
            offset += 100
        resolved = self.memory.effectiveness_resolutions(adr_keys, query.start, query.end) if adr_keys else 0
        scheduled = [row for row in records if row["due"]]
        today = datetime.now(UTC).date().isoformat()
        current = sum(row["due"] >= today for row in scheduled)
        documented = sum(row["documented"] for row in records) + sum(node["documented"] for node in nodes)
        automatic_obs = sum(node["type"] == "Observation" and node["automatic"] for node in nodes)
        inferred_stored = sum(edge["inferred"] for edge in links)
        suggestions = sum(node["suggestions"] for node in nodes if node["type"] == "Observation")

        return EffectivenessReport(
            project_key=project.key, project_name=project.name, repository_key=query.repository_key,
            repository_name=repository, repositories=repositories, start=query.start, end=query.end,
            inventory={"adrs": adr_count, "constraints": types["Constraint"], "goals": types["Goal"],
                       "observations": types["Observation"], "entities": len(nodes), "links": len(links)},
            delivery={
                "tasks_completed": finished["completed"], "tasks_abandoned": finished["abandoned"],
                "current_completed_tasks": sum(node["type"] == "Task" and node["status"] == "done" for node in nodes),
                "current_abandoned_tasks": sum(node["type"] == "Task" and node["status"] == "cancelled" for node in nodes),
                "agent_changes": change_counts["agent"], "human_changes": change_counts["human"],
                "first_pass_percent": rate(sum(checked), len(checked)).percent,
                "first_pass_samples": len(checked),
                "retries": sum(retry_data) if retry_data else None,
                "retry_percent": rate(sum(value > 0 for value in retry_data), len(retry_data)).percent,
                "retry_samples": len(retry_data), "average_task_completion_hours": round(mean(elapsed), 2) if elapsed else None,
                "timed_tasks": len(elapsed),
            },
            architecture={
                "adr_conflicts_detected": unresolved + resolved, "unresolved_adr_conflicts": unresolved,
                "adr_conflicts_resolved": resolved, "constraint_violations": sum(len(r.violated_constraints) for r in reports),
                "constraint_coverage": rate(len(constraints_with_tests), len(active)),
                "services_linked_to_adrs": len(services_with_adrs), "tests_linked_to_constraints": len(tests_with_constraints),
            },
            capture={
                "automatic_observations": automatic_obs, "manual_observations": types["Observation"] - automatic_obs,
                "inferred_links": inferred_stored + suggestions, "pending_link_suggestions": suggestions,
                "explicit_links": len(links) - inferred_stored, "stale_entities": state["stale"],
                "revalidated_entities": sum(row["validated"] for row in records),
            },
            health={
                "memory_score": rate(documented, adr_count + types["Constraint"] + types["Goal"]),
                "traceability_score": rate(len(services_with_adrs) + len(constraints_with_tests),
                                           len(by_type["Service"]) + len(active)),
                "validation_score": rate(current, len(scheduled)),
            },
            economics=economics(events, reports, query.input_price_per_million),
            methodology={
                "inventory": "Current graph snapshot. Repository scope includes indexed entities and directly linked evidence. Historical selectors apply to dated activity.",
                "delivery": "Unique changes explicitly reported as agent or human. Agent first-pass and retry rates use reported outcomes or the earliest imported CI run with a first attempt for the exact commit and repository. CI retries count imported non-cancelled reruns. Task finishes combine applied tracker events and reported task outcomes; duration requires an explicit start.",
                "architecture": "Detected conflicts include unresolved pairs overlapping the selected period plus pairs resolved during it. Historical detection time is unavailable. Coverage is active constraints with a validating test.",
                "capture": "Automatic observation counts use capture provenance. Inferred links include pending suggestions and lower-confidence static links. Explicit links are stored edges without an inference marker. Revalidation counts distinct records with dated evidence.",
                "health": "Memory measures documented ADRs, constraints, and goals. Traceability measures ADR-linked services and test-covered active constraints. Validation measures scheduled records whose review deadline has not passed.",
                "economics": "Token confidence has a 0.5 ceiling for model-independent estimates and is scaled by baseline coverage. Effort confidence comes from cited writer estimates. Missing evidence produces a blank metric.",
            },
        )
