from __future__ import annotations

import pytest

from app.domain.architecture import ApiEndpoint, Class, Function, Repository, Service
from app.domain.architecture import TestSuite as Suite
from app.domain.exceptions import ArchitectureGraphError
from app.services.python_architecture import PythonArchitectureIndexer

SOURCES = {
    "src/assets/repositories.py": """
class AssetRepository:
    def list(self):
        return self.database.collection("assets")
""",
    "src/assets/service.py": """
from .repositories import AssetRepository as Storage

class AssetService:
    def __init__(self, storage: Storage):
        self.storage = storage

    async def list(self):
        return await self.storage.list()
""",
    "src/assets/api.py": """
import fastapi as http
from .service import AssetService

router = http.APIRouter(prefix="/v1")

@router.get("/assets")
async def list_assets(service: AssetService):
    return await service.list()
""",
    "tests/test_assets.py": """
from assets.service import AssetService

def test_asset_service():
    assert AssetService
""",
}


def test_python_index_extracts_entities_and_resolves_internal_references():
    bundle = PythonArchitectureIndexer().build("assets", SOURCES)
    entities = {(node.entity_type, node.name): node for node in bundle.entities}
    relationships = {
        (link.source_key, link.target_key, link.relationship_type) for link in bundle.links
    }
    service_class = entities["Class", "AssetService"]
    service = entities["Service", "AssetService"]
    repository = entities["Repository", "AssetRepository"]
    handler = entities["Function", "list_assets"]
    endpoint = entities["ApiEndpoint", "GET /v1/assets"]
    assert isinstance(service_class, Class)
    assert isinstance(service, Service)
    assert isinstance(repository, Repository)
    assert isinstance(handler, Function) and handler.is_async
    assert isinstance(endpoint, ApiEndpoint) and endpoint.path == "/v1/assets"
    assert isinstance(entities["TestSuite", "tests.test_assets"], Suite)
    assert (service_class.key, service.key, "implements") in relationships
    assert (service.key, repository.key, "uses") in relationships
    assert (
        repository.key,
        entities["DatabaseCollection", "assets"].key,
        "persists_to",
    ) in relationships
    assert (handler.key, endpoint.key, "implements") in relationships
    assert (handler.key, service_class.key, "uses") in relationships
    assert (
        entities["Module", "assets.service"].key,
        entities["Module", "assets.repositories"].key,
        "uses",
    ) in relationships
    assert all(link.source == "static-analysis" for link in bundle.links)
    assert len({node.key for node in bundle.entities}) == len(bundle.entities)


def test_python_index_uses_repository_scoped_keys_and_qualified_method_names():
    indexer = PythonArchitectureIndexer()
    sources = {"service.py": "class A:\n def run(self): pass\nclass B:\n def run(self): pass\n"}
    first = indexer.build("first", sources)
    second = indexer.build("second", sources)
    assert {node.key for node in first.entities}.isdisjoint(node.key for node in second.entities)
    assert first == indexer.build("first", sources)
    assert {fn.qualified_name for fn in first.entities if isinstance(fn, Function)} == {
        "service.A.run",
        "service.B.run",
    }


@pytest.mark.parametrize("path, source", [("../escape.py", "pass"), ("broken.py", "def broken(")])
def test_python_index_rejects_invalid_snapshots(path, source):
    with pytest.raises(ArchitectureGraphError):
        PythonArchitectureIndexer().build("test", {path: source})


def test_indexer_captures_literal_constraint_declarations():
    index = PythonArchitectureIndexer().build("Example", {
        "rules.py": """
rule = ConstraintInput(
    key="verified-identity", name="Verify identity", rule="Require verified tokens",
    scope="public API", authority="Platform security", enforcement_status="enforced",
)
dynamic = ConstraintInput(key=identifier, name="Dynamic", rule=get_rule(), scope="API", authority="Team")
""",
    })
    rule, = [entry for entry in index.entities if entry.entity_type == "Constraint"]
    assert rule.name == "Verify identity"
    assert rule.authority == "Platform security"
    assert rule.enforcement_status == "proposed"
    assert rule.metadata["declared_enforcement_status"] == "enforced"
    assert any(link.target_key == rule.key and link.relationship_type == "defines" for link in index.links)
