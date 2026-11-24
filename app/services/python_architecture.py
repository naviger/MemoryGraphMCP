from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic import ValidationError

from app.domain.architecture import (
    ApiEndpoint,
    ArchitectureEntity,
    ArchitectureIndex,
    ArchitectureLink,
    Class,
    CodeRevision,
    Constraint,
    DatabaseCollection,
    Function,
    Module,
    RelationshipType,
    Repository,
    Service,
    SourceFile,
    TestSuite,
    architecture_key,
)
from app.domain.exceptions import ArchitectureGraphError

MAX_FILES = 2_000
MAX_SOURCE_BYTES = 8_000_000
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace", "websocket"}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


@dataclass
class _Scope:
    owner: str
    parents: tuple[str, ...] = ()
    imports: dict[str, str] = field(default_factory=dict)
    references: set[str] = field(default_factory=set)


@dataclass
class _PythonFile:
    path: str
    module_name: str
    module_key: str
    imports: set[str] = field(default_factory=set)
    scopes: list[_Scope] = field(default_factory=list)
    class_roles: dict[str, str] = field(default_factory=dict)
    symbols: dict[str, str] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)


class _Index:
    def __init__(self, namespace: str, repository_key: str, revision: CodeRevision | None) -> None:
        self.namespace = namespace
        self.repository_key = repository_key
        self.revision = revision
        self.entities: dict[str, ArchitectureEntity] = {}
        self.links: dict[tuple[str, str, RelationshipType], ArchitectureLink] = {}

    def add(self, entity: ArchitectureEntity) -> str:
        self.entities[entity.key] = entity.model_copy(update={
            "repository_key": self.repository_key,
            "source_revision": self.revision,
        })
        return entity.key

    def link(
        self,
        source: str,
        target: str,
        kind: RelationshipType,
        confidence: float = 1.0,
    ) -> None:
        if source != target:
            self.links.setdefault(
                (source, target, kind),
                ArchitectureLink(
                    source_key=source,
                    target_key=target,
                    relationship_type=kind,
                    confidence=confidence,
                    source="static-analysis",
                    source_revision=self.revision,
                ),
            )


class _Declarations(ast.NodeVisitor):
    def __init__(self, index: _Index, file: _PythonFile) -> None:
        self.index = index
        self.file = file
        self.scope = _Scope(file.module_key)
        file.scopes.append(self.scope)

    def _add_symbol(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        parents = (*self.scope.parents, node.name)
        qualified_name = ".".join(filter(None, (self.file.module_name, *parents)))
        shared = {
            "key": architecture_key(
                self.index.namespace,
                self.file.path,
                type(node).__name__.replace("Async", ""),
                *parents,
            ),
            "name": node.name,
            "qualified_name": qualified_name,
            "description": ast.get_docstring(node) or "",
            "source_path": self.file.path,
            "metadata": {"line": node.lineno, "end_line": node.end_lineno},
        }
        entity = (
            Class(**shared)
            if isinstance(node, ast.ClassDef)
            else Function(**shared, is_async=isinstance(node, ast.AsyncFunctionDef))
        )
        self.index.add(entity)
        self.index.link(self.scope.owner, entity.key, "defines")
        self.file.symbols[qualified_name] = entity.key
        return entity, parents

    def _visit_body(self, node: ast.AST, key: str, parents: tuple[str, ...]) -> None:
        previous = self.scope
        scope = _Scope(key, parents, dict(previous.imports))
        self.file.scopes.append(scope)
        self.scope = scope
        self.generic_visit(node)
        self.scope = previous

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        entity, parents = self._add_symbol(node)
        lowered = node.name.casefold()
        if lowered.endswith(("service", "repository", "repo")):
            role_model = Service if lowered.endswith("service") else Repository
            role = role_model(
                key=architecture_key(entity.key, role_model.__name__),
                name=entity.name,
                description=entity.description,
                source_path=entity.source_path,
                metadata=entity.metadata,
            )
            self.index.add(role)
            self.index.link(entity.key, role.key, "implements")
            self.file.class_roles[entity.key] = role.key
        self._visit_body(node, entity.key, parents)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        entity, parents = self._add_symbol(node)
        for decorator in node.decorator_list:
            endpoint = self._endpoint(decorator, entity.key)
            if endpoint is not None:
                self.index.add(endpoint)
                self.index.link(entity.key, endpoint.key, "implements")
        self._visit_body(node, entity.key, parents)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _endpoint(self, decorator: ast.AST, function_key: str) -> ApiEndpoint | None:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            return None
        receiver = _dotted(decorator.func.value)
        method = decorator.func.attr
        if receiver not in self.file.routes or method not in _HTTP_METHODS:
            return None
        path = _literal_string(
            decorator.args[0]
            if decorator.args
            else next((kw.value for kw in decorator.keywords if kw.arg == "path"), None)
        )
        if path is None or not path.startswith("/"):
            return None
        path = self.file.routes[receiver].rstrip("/") + path
        name = f"{method.upper()} {path}"
        return ApiEndpoint(
            key=architecture_key(function_key, method, path),
            name=name,
            method=method.upper(),
            path=path,
            source_path=self.file.path,
            metadata={"line": decorator.lineno, "router": receiver},
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.file.imports.add(alias.name)
            local = alias.asname or alias.name.split(".")[0]
            self.scope.imports[local] = alias.name if alias.asname else local

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            # An __init__ module is itself a package.
            package = self.file.module_name.split(".")
            if not self.file.path.endswith("/__init__.py") and self.file.path != "__init__.py":
                package = package[:-1]
            if node.level - 1 > len(package):
                return
            parent = package[: len(package) - node.level + 1]
            module = ".".join(filter(None, (*parent, module)))
        if module:
            self.file.imports.add(module)
        for alias in node.names:
            if alias.name != "*":
                self.scope.imports[alias.asname or alias.name] = ".".join(
                    filter(None, (module, alias.name))
                )

    def visit_Call(self, node: ast.Call) -> None:
        if (_dotted(node.func) or "").split(".")[-1] == "ConstraintInput":
            self._record_constraint(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "collection":
            receiver = _dotted(node.func.value)
            name = _literal_string(
                node.args[0]
                if node.args
                else next((kw.value for kw in node.keywords if kw.arg == "name"), None)
            )
            if receiver and receiver.split(".")[-1] in {"db", "database"} and name and name.strip():
                self._link_collection(name, node.lineno)
        self.generic_visit(node)

    def _record_constraint(self, node: ast.Call) -> None:
        fields = {item.arg: _literal_string(item.value) for item in node.keywords}
        required = ("key", "name", "rule", "scope", "authority")
        if node.args or not all(fields.get(field) for field in required):
            return
        try:
            entity = Constraint(
                key=architecture_key(self.index.namespace, self.file.path, "constraint", fields["key"]),
                name=fields["name"], rule=fields["rule"], scope=fields["scope"],
                authority=fields["authority"], enforcement_status="proposed",
                source_path=self.file.path,
                metadata={
                    "line": node.lineno, "declared_key": fields["key"],
                    "declared_enforcement_status": fields.get("enforcement_status"),
                },
            )
        except ValidationError:
            return
        self.index.link(self.scope.owner, self.index.add(entity), "defines")

    def _link_collection(self, name: str, line: int) -> None:
        for length in range(len(self.scope.parents), 0, -1):
            qualified = ".".join((self.file.module_name, *self.scope.parents[:length]))
            class_key = self.file.symbols.get(qualified)
            if class_key is None:
                continue
            role_key = self.file.class_roles.get(class_key)
            if role_key is None or not isinstance(self.index.entities[role_key], Repository):
                continue
            collection = self.index.add(
                DatabaseCollection(
                    key=architecture_key(self.index.namespace, "database-collection", name),
                    name=name,
                    metadata={"inferred_from": f"{self.file.path}:{line}"},
                )
            )
            self.index.link(role_key, collection, "persists_to", confidence=0.8)
            break

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = _dotted(node)
        if name and isinstance(node.ctx, ast.Load):
            self.scope.references.add(name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.scope.references.add(node.id)


def _routers(tree: ast.Module) -> dict[str, str]:
    constructors: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "fastapi" and node.level == 0:
            constructors.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in {"FastAPI", "APIRouter"}
            )
        if isinstance(node, ast.Import):
            constructors.update(
                f"{alias.asname or alias.name}.{name}"
                for alias in node.names
                if alias.name == "fastapi"
                for name in ("FastAPI", "APIRouter")
            )
    routers = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Call) and _dotted(value.func) in constructors:
            prefix = _literal_string(
                next((kw.value for kw in value.keywords if kw.arg == "prefix"), None)
            )
            routers.update(
                {target.id: prefix or "" for target in targets if isinstance(target, ast.Name)}
            )
    return routers


def _module_name(path: PurePosixPath) -> str:
    parts = path.with_suffix("").parts
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__" and len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts)


class PythonArchitectureIndexer:
    """Parse a repository snapshot without importing or executing its code."""

    def build(
        self, repository: str, sources: dict[str, str], *, root: Repository | None = None,
        revision: CodeRevision | None = None,
    ) -> ArchitectureIndex:
        repository = repository.strip()
        if not repository or len(repository) > 500:
            raise ArchitectureGraphError(
                "Repository name must contain between 1 and 500 characters"
            )
        if (
            len(sources) > MAX_FILES
            or sum(len(source.encode()) for source in sources.values()) > MAX_SOURCE_BYTES
        ):
            raise ArchitectureGraphError(
                f"Indexing supports up to {MAX_FILES} files and {MAX_SOURCE_BYTES} source bytes"
            )

        root = root or Repository(
            key=architecture_key("python-repository", repository), name=repository, role="source",
        )
        if revision and revision.repository_key != root.key:
            raise ArchitectureGraphError("Revision belongs to a different repository")
        namespace = root.index_namespace or (root.repository if root.index_state else None) or root.key
        index = _Index(namespace, root.key, revision)
        repository_key = index.add(root.model_copy(update={
            "name": repository, "index_namespace": namespace,
        }))
        files: list[_PythonFile] = []
        for relative, source in sorted(sources.items()):
            path = PurePosixPath(relative)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in relative
                or path.as_posix() != relative
                or path.suffix != ".py"
            ):
                raise ArchitectureGraphError(
                    f"Expected a normalized relative Python path: {relative!r}"
                )
            try:
                tree = ast.parse(source, filename=relative)
            except SyntaxError as exc:
                raise ArchitectureGraphError(
                    f"Invalid Python syntax in {relative} at line {exc.lineno}: {exc.msg}"
                ) from exc
            file_key = index.add(
                SourceFile(
                    key=architecture_key(namespace, "file", relative),
                    name=path.name,
                    source_path=relative,
                )
            )
            index.link(repository_key, file_key, "defines")
            name = _module_name(path)
            module_key = index.add(
                Module(
                    key=architecture_key(namespace, "module", relative),
                    name=name,
                    qualified_name=name,
                    description=ast.get_docstring(tree) or "",
                    source_path=relative,
                )
            )
            index.link(file_key, module_key, "defines")
            file = _PythonFile(relative, name, module_key, routes=_routers(tree))
            _Declarations(index, file).visit(tree)
            files.append(file)
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                suite = index.add(
                    TestSuite(
                        key=architecture_key(namespace, "tests", relative),
                        name=name,
                        source_path=relative,
                    )
                )
                index.link(suite, module_key, "defined_in")

        self._connect(index, files)
        return ArchitectureIndex(
            repository=repository,
            repository_key=repository_key,
            namespace=namespace,
            revision=revision,
            entities=list(index.entities.values()),
            links=list(index.links.values()),
        )

    @staticmethod
    def _connect(index: _Index, files: list[_PythonFile]) -> None:
        modules = {file.module_name: file.module_key for file in files}
        symbols = {name: key for file in files for name, key in file.symbols.items()}
        roles = {key: role for file in files for key, role in file.class_roles.items()}
        for file in files:
            module = index.entities[file.module_key]
            index.entities[file.module_key] = module.model_copy(
                update={"imports": sorted(file.imports)}
            )
            module_dependencies = file.imports | {
                name for scope in file.scopes for name in scope.imports.values() if name in modules
            }
            for imported in sorted(module_dependencies):
                target = modules.get(imported)
                if target is None:
                    target = index.add(
                        Module(
                            key=architecture_key(index.namespace, "external-module", imported),
                            name=imported,
                            qualified_name=imported,
                            metadata={"external": True},
                        )
                    )
                index.link(file.module_key, target, "uses")

            root_imports = file.scopes[0].imports
            for scope in file.scopes:
                for reference in sorted(scope.references):
                    head, _, tail = reference.partition(".")
                    imported = scope.imports.get(head, root_imports.get(head))
                    candidates = (
                        ([".".join(filter(None, (imported, tail)))] if imported else [])
                        + [
                            ".".join((file.module_name, *scope.parents[:length], reference))
                            for length in range(len(scope.parents), -1, -1)
                        ]
                        + [reference]
                    )
                    target = next((symbols[name] for name in candidates if name in symbols), None)
                    if target is None:
                        continue
                    index.link(scope.owner, target, "uses", confidence=0.85)
                    for length in range(1, len(scope.parents) + 1):
                        owner_name = ".".join((file.module_name, *scope.parents[:length]))
                        owner = file.symbols.get(owner_name)
                        if owner is not None and isinstance(index.entities[owner], Class):
                            index.link(owner, target, "uses", confidence=0.85)
                            if owner in roles and target in roles:
                                index.link(roles[owner], roles[target], "uses", confidence=0.85)
