from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.auth.project_access import ProjectAccessDenied, access_for_claims, current_project_access
from app.config.settings import Settings
from app.domain.architecture import ArchitectureSearch, ArchitectureTrace, Project, Service
from app.domain.models import MemorySearchRequest
from app.domain.review_details import ProjectDocument, ProjectEntity, ProjectScope
from app.repositories.scoped import ScopedRepositories


def claims(role, grants=None):
    return {"subject": "person", "raw": {"roles": [role], "mgmcp_projects": grants}}


def test_project_grants_require_authenticated_roles():
    readonly = access_for_claims(claims("reader", {"alpha": "write"}), "mgmcp_projects")
    assert readonly.grants == {"alpha": "read"}
    with pytest.raises(ProjectAccessDenied):
        readonly.require("alpha", write=True)
    assert access_for_claims(claims("writer", {"beta": "write", "bad/key": "read"}), "mgmcp_projects").grants == {
        "beta": "write",
    }
    assert access_for_claims(claims("admin"), "mgmcp_projects").unrestricted
    assert not access_for_claims({"raw": {"roles": ["admin"]}}, "mgmcp_projects").unrestricted
    with pytest.raises(ValueError, match="requires authentication"):
        Settings(_env_file=None, auth_required=False, project_authorization_required=True)


@pytest.fixture
def scoped():
    def scope(project, *, strict_scope, include_details):
        assert strict_scope
        return ProjectScope(
            entities=[ProjectEntity(key=f"{project}-service", name=project, entity_type="Service", reason="linked")],
            dependencies=[ProjectEntity(key="other", name="Foreign", entity_type="Service", reason="uses")],
            repository_names=[project],
        )

    graph = SimpleNamespace(
        read_entity=lambda key: Project(key=key, name=key) if key in {"alpha", "beta"} else (
            Service(key=key, name=key) if key in {"alpha-service", "beta-service"} else None
        ),
        project_scope=scope, project_access_keys=lambda: ["alpha", "beta"],
        search=Mock(return_value=[]), trace=Mock(return_value=[]),
        query_usage=Mock(return_value=([], [])), put_entity=Mock(),
    )
    memory = SimpleNamespace(
        architecture=graph, project_documents=lambda project, names, *_: [
            ProjectDocument(key=f"{project}-record", title=project, kind="memory", reason="project"),
            ProjectDocument(key="shared", title="shared", kind="memory", reason="project"),
        ] if not names else [],
        search_documents=Mock(return_value=[]), read_document=Mock(), revalidate_memory=Mock(),
    )
    return ScopedRepositories(memory)


def test_scoped_repositories_filter_before_query_and_default_to_deny(scoped):
    token = current_project_access.set(access_for_claims(claims("writer", {"alpha": "write"}), "mgmcp_projects"))
    try:
        scoped.memory.search_documents(MemorySearchRequest(query="example", document_keys=["alpha-record", "beta-record"]))
        assert scoped.memory_store.search_documents.call_args.args[0].document_keys == ["alpha-record"]
        scoped.graph.search(ArchitectureSearch())
        assert scoped.graph_store.search.call_args.kwargs["allowed_entity_keys"] == ["alpha", "alpha-service"]
        scoped.graph.trace(ArchitectureTrace(key="alpha-service", depth=4))
        assert scoped.graph_store.trace.call_args.kwargs["allowed_entity_keys"] == ["alpha", "alpha-service"]
        assert scoped.graph.project_scope("alpha").dependencies == []
        assert scoped.memory.read_document("beta-record") is None
        assert not scoped.memory_store.read_document.called
        assert scoped.graph.read_entity("beta") is None
        scoped.graph.query_usage("lifetime", None, None)
        assert scoped.graph_store.query_usage.call_args.kwargs["allowed_projects"] == ["alpha"]
        with pytest.raises(ProjectAccessDenied):
            scoped.graph.trace(ArchitectureTrace(key="beta-service"))
        with pytest.raises(ProjectAccessDenied):
            scoped.graph.put_entity(Project(key="beta", name="Changed"))
        with pytest.raises(ProjectAccessDenied):
            scoped.memory.revalidate_memory("shared", object())
        assert not scoped.memory_store.revalidate_memory.called
        assert not scoped.graph_store.put_entity.called
    finally:
        current_project_access.reset(token)
    assert scoped.graph.read_entity("alpha") is None
