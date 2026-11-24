"""Fail-closed repository adapters for user requests when project grants are enabled."""
from functools import wraps

from app.auth.project_access import ProjectAccess, ProjectAccessDenied, current_project_access
from app.domain.architecture import Project

# These methods bind directly to a project key as their first argument.
_PROJECT_READ = {
    "project_source_repositories", "recent_tracker_activity", "change_test_runs",
    "change_branch_cleanups", "read_fix_workflow", "latest_test_run", "latest_passing_test_run",
    "read_test_run", "read_test_run_for_commit", "list_test_runs", "recurring_test_failures",
    "list_guidance_versions",
}
_TEST_DETAIL = {"latest_test_run", "latest_passing_test_run", "read_test_run"}


class _Scoped:
    def __init__(self, repository, authorization):
        self._repository = repository
        self._authorization = authorization

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        operation = getattr(self._repository, name)
        if not callable(operation):
            raise AttributeError(name)

        @wraps(operation)
        def authorized(*args, **kwargs):
            access = current_project_access.get() or ProjectAccess()
            if access.unrestricted:
                return operation(*args, **kwargs)
            return self._dispatch(name, operation, access, args, kwargs)

        return authorized


class ScopedRepositories:
    def __init__(self, memory):
        self.memory_store = memory
        self.graph_store = memory.architecture
        self.memory = ScopedMemory(memory, self)
        self.graph = ScopedGraph(self.graph_store, self)

    def project_scope(self, access, project, *, include_details=False):
        cache_key = (project, include_details)
        if cache_key not in access.cache:
            node = self.graph_store.read_entity(project)
            if not isinstance(node, Project):
                access.cache[cache_key] = (None, set(), set())
            else:
                scope = self.graph_store.project_scope(
                    project, strict_scope=True, include_details=include_details,
                )
                # Only actual ownership and direct project links authorize reads.
                docs = self.memory_store.project_documents(
                    project, [], [item.memory_key for item in scope.entities if item.memory_key], "", None,
                )
                access.cache[cache_key] = (scope, {project, *(item.key for item in scope.entities)},
                                           {item.key for item in docs})
        return access.cache[cache_key]

    def keys(self, access, index):
        return set().union(*(self.project_scope(access, project)[index] for project in access.grants))

    def require_records(self, access, keys, index, *, write=False):
        if not set(keys) <= self.keys(access, index):
            raise ProjectAccessDenied()
        if write:
            # An object shared by multiple projects requires write access to each.
            for project in self.graph_store.project_access_keys():
                scoped = self.project_scope(access, project)[index]
                if set(keys) & scoped:
                    access.require(project, write=True)


def _filtered(keys, supplied):
    return sorted(keys if supplied is None else keys.intersection(supplied))


def _conflicts(rows, keys):
    return [row for row in rows if row.original.key in keys and row.competing.key in keys]


def _safe_entity(node, entities, documents):
    if node is None:
        return None
    fields = ("related_entity_keys", "constrained_entity_keys", "test_suite_keys", "adr_keys",
              "service_keys", "task_keys", "decision_keys", "risk_keys")
    updates = {name: [key for key in getattr(node, name) if key in entities]
               for name in fields if hasattr(node, name)}
    if getattr(node, "memory_key", None) and node.memory_key not in documents:
        updates["memory_key"] = None
    if getattr(node, "project_key", None) and node.project_key not in entities:
        updates["project_key"] = None
    if getattr(node, "suggestions", None):
        updates["suggestions"] = [link for link in node.suggestions
                                  if link.source_key in entities and link.target_key in entities]
    if node.repository_key not in entities:
        updates["repository_key"] = None
        updates["source_revision"] = None
    # Operational capture metadata can contain identifiers beyond this principal's scope.
    updates["metadata"] = {key: value for key, value in node.metadata.items() if key in {"line", "end_line"}}
    return node.model_copy(update=updates)


class ScopedMemory(_Scoped):
    @property
    def architecture(self):
        return self._authorization.graph

    def _dispatch(self, name, operation, access, args, kwargs):
        docs = self._authorization.keys(access, 2)
        if name in {"search_documents", "semantic_search", "hybrid_search"}:
            request, = args
            return operation(request.model_copy(update={
                "document_keys": _filtered(docs, request.document_keys),
            }))
        if name in {"search_question_documents", "list_overdue_validations"}:
            return operation(*args, **{**kwargs, "document_keys": _filtered(docs, kwargs.get("document_keys"))})
        if name == "list_adr_conflicts":
            keys = _filtered(docs, kwargs.get("document_keys"))
            return operation(*args, document_keys=keys, require_both=True)
        if name == "list_resolved_adr_conflicts":
            return operation(*args, allowed_document_keys=sorted(docs))
        if name == "find_adr_conflicts":
            self._authorization.require_records(access, [args[0].key], 2)
            return operation(
                *args, allowed_document_keys=_filtered(docs, kwargs.get("allowed_document_keys")),
            )
        if name == "find_related_entities":
            request, = args
            return operation(request, allowed_document_keys=sorted(docs))
        if name == "project_documents":
            project, _, linked, query, as_of = args
            access.require(project)
            scoped = self._authorization.project_scope(access, project)[2]
            return operation(project, [], _filtered(scoped, linked), query, as_of)
        if name in {"read_document", "read_adr", "list_adr_supersession_links"}:
            key = args[0]
            if name == "read_document" and key not in docs:
                return None
            self._authorization.require_records(access, [key], 2)
            result = operation(*args, **kwargs)
            return [link for link in result if link.key in docs] if name == "list_adr_supersession_links" else result
        if name == "effectiveness_resolutions":
            return operation(_filtered(docs, args[0]), *args[1:], require_both=True)
        if name in {"read_guidance_decisions", "effectiveness_records"}:
            return operation(_filtered(docs, args[0]), *args[1:], **kwargs)
        if name == "list_documents":
            return [document for key in sorted(docs) if (document := self._repository.read_document(key))]
        if name in {"get_adr_conflict_resolution", "resolve_adr_conflict", "supersede_adr"}:
            self._authorization.require_records(access, args[:2], 2, write=name != "get_adr_conflict_resolution")
            return operation(*args, **kwargs)
        if name in {"revalidate_memory", "set_adr_conflict_scope", "set_adr_status"}:
            self._authorization.require_records(access, args[:1], 2, write=True)
            result = operation(*args, **kwargs)
            if name == "set_adr_status" and result.conflict_check:
                check = result.conflict_check
                visible = _conflicts(check.conflicts, docs)
                result.conflict_check = check.model_copy(update={"conflicts": visible, "has_more": False})
            return result
        raise ProjectAccessDenied()


class ScopedGraph(_Scoped):
    def _dispatch(self, name, operation, access, args, kwargs):
        auth = self._authorization
        if name in _PROJECT_READ:
            access.require(args[0])
            result = operation(*args, **kwargs)
            if name == "project_source_repositories":
                keys = auth.project_scope(access, args[0])[1]
                return [repo for repo in result if repo.key in keys]
            if name in _TEST_DETAIL and result:
                entities = auth.project_scope(access, args[0])[1]
                docs = auth.project_scope(access, args[0])[2]
                return result.model_copy(update={"failures": [
                    failure.model_copy(update={"validations": [
                        link for link in failure.validations if link.key in entities
                        and (link.memory_key is None or link.memory_key in docs)
                    ]}) for failure in result.failures
                ]})
            return result
        if name == "read_guidance_state":
            project, = args
            if project is None:
                # Published global guidance is intentionally deployment-wide.
                return operation(None).model_copy(update={"draft": None})
            access.require(project)
            state = operation(project)
            return state if access.grants[project] == "write" else state.model_copy(update={"draft": None})
        if name == "save_guidance_draft":
            access.require(args[0], write=True)
            return operation(*args, **kwargs)
        if name in {"save_fix_workflow", "save_development_outcome"}:
            access.require(args[0].project_key, write=True)
            if name == "save_development_outcome":
                outcome = args[0]
                records = [outcome.repository_key, *outcome.violated_constraints]
                if outcome.task_key:
                    records.append(outcome.task_key)
                if not set(records) <= auth.project_scope(access, outcome.project_key)[1]:
                    raise ProjectAccessDenied()
            return operation(*args, **kwargs)
        if name in {"ingest_event", "get_event"}:
            access.require(args[1], write=True)
            return operation(*args, **kwargs)
        if name == "project_scope":
            project = args[0]
            access.require(project)
            scope, keys, docs = auth.project_scope(
                access, project, include_details=kwargs.get("include_details", True),
            )
            if scope is None:
                raise ProjectAccessDenied()
            return scope.model_copy(update={"dependencies": [], "entities": [
                node.model_copy(update={
                    "decision_links": [link for link in node.decision_links if link.key in keys],
                    "memory_key": node.memory_key if node.memory_key in docs else None,
                    "repository_key": node.repository_key if node.repository_key in keys else None,
                }) for node in scope.entities
            ]})
        if name == "guidance_scope":
            project = args[0]
            access.require(project)
            _, keys, docs = auth.project_scope(access, project)
            scope = operation(*args, **kwargs)
            return scope.model_copy(update={"entity_keys": _filtered(keys, scope.entity_keys),
                                            "adr_keys": _filtered(docs, scope.adr_keys)})
        if name in {"effectiveness_graph", "effectiveness_events"}:
            access.require(args[0])
            if name == "effectiveness_graph":
                return operation(args[0], _filtered(auth.project_scope(access, args[0])[1], args[1]),
                                 *args[2:], **kwargs, strict_scope=True)
            return operation(*args, **kwargs)
        entities, docs = auth.keys(access, 1), auth.keys(access, 2)
        if name == "read_entity":
            return _safe_entity(operation(*args), entities, docs) if args[0] in entities else None
        if name == "search":
            return [_safe_entity(node, entities, docs) for node in operation(*args, allowed_entity_keys=sorted(entities))]
        if name == "count_by_entity_type":
            return operation(*args, **kwargs, allowed_entity_keys=sorted(entities))
        if name == "trace":
            auth.require_records(access, [args[0].key], 1)
            return [path.model_copy(update={
                "entity": _safe_entity(path.entity, entities, docs),
                "nodes": [_safe_entity(node, entities, docs) for node in path.nodes],
                "relationships": [link.model_copy(update={
                    "source_revision": link.source_revision if link.source_revision
                        and link.source_revision.repository_key in entities else None,
                    "cited_code": [location for location in link.cited_code
                                   if location.revision.repository_key in entities],
                }) for link in path.relationships],
            }) for path in operation(*args, allowed_entity_keys=sorted(entities))]
        if name in {"find_memory_entities", "guidance_constraints"}:
            result = operation(*args, **kwargs)
            return [_safe_entity(node, entities, docs) for node in result if node.key in entities]
        if name in {"add_observation", "save_project_goal"}:
            entry = args[0]
            related = (entry.related_entity_keys if name == "add_observation" else
                       [entry.project_key, *entry.adr_keys, *entry.service_keys, *entry.task_keys])
            roots = set(related).intersection(access.grants)
            if len(roots) != 1:
                raise ProjectAccessDenied()
            project = next(iter(roots))
            access.require(project, write=True)
            if not set(related) <= auth.project_scope(access, project)[1]:
                raise ProjectAccessDenied()
            if self._repository.read_entity(entry.key) is not None:
                auth.require_records(access, [entry.key], 1, write=True)
            return operation(*args, **kwargs)
        if name == "capture_review":
            project, limit, as_of = args
            if project:
                access.require(project)
            return operation(project, limit, as_of, allowed_projects=sorted(access.grants),
                             allowed_entity_keys=sorted(entities))
        if name in {"read_usage", "report_usage"}:
            receipt = self._repository.read_usage(*args[:2])
            if not receipt or receipt.project_key not in access.grants:
                return None
            return receipt if name == "read_usage" else operation(*args, **kwargs)
        if name == "record_usage":
            if args[0].project_key is None:
                return None
            access.require(args[0].project_key)
            return operation(*args, **kwargs)
        if name == "query_usage":
            scope, project, session = args
            if scope == "project":
                access.require(project)
            return operation(scope, project, session, allowed_projects=sorted(access.grants))
        raise ProjectAccessDenied()
