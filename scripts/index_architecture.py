from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tokenize
from io import BytesIO
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from app.domain.architecture import CodeRevision

from app.config.settings import get_settings
from app.domain.exceptions import ArchitectureGraphError
from app.repositories.factory import build_repository
from app.services.architecture_service import ArchitectureService
from app.services.python_architecture import MAX_FILES, MAX_SOURCE_BYTES

_IGNORED_DIRECTORIES = {"venv", "node_modules", "__pycache__", "build", "dist", "site-packages"}


def load_sources(root: Path) -> dict[str, str]:
    sources = {}
    size = 0
    for directory, subdirs, files in os.walk(root):
        path = Path(directory)
        subdirs[:] = sorted(
            name
            for name in subdirs
            if not name.startswith(".")
            and name not in _IGNORED_DIRECTORIES
            and not (path / name).is_symlink()
        )
        for name in sorted(files):
            candidate = path / name
            if candidate.suffix != ".py" or candidate.is_symlink() or not candidate.is_file():
                continue
            size += candidate.stat().st_size
            if len(sources) >= MAX_FILES or size > MAX_SOURCE_BYTES:
                raise ArchitectureGraphError("Checkout exceeds the Python index size limits")
            with tokenize.open(candidate) as source:
                sources[candidate.relative_to(root).as_posix()] = source.read()
    return sources


def load_commit_sources(root: Path, ref: str) -> tuple[str, dict[str, str]]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(root), *args], timeout=60)

    commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    sources = {}
    size = 0
    for entry in git("ls-tree", "-rz", commit).split(b"\0"):
        if not entry:
            continue
        header, path_bytes = entry.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        path = PurePosixPath(path_bytes.decode("utf-8"))
        if (kind != "blob" or mode not in {"100644", "100755"} or path.suffix != ".py"
                or any(part.startswith(".") or part in _IGNORED_DIRECTORIES for part in path.parts[:-1])):
            continue
        size += int(git("cat-file", "-s", oid))
        if len(sources) >= MAX_FILES or size > MAX_SOURCE_BYTES:
            raise ArchitectureGraphError("Commit exceeds the Python index size limits")
        data = git("cat-file", "blob", oid)
        encoding, _ = tokenize.detect_encoding(BytesIO(data).readline)
        sources[path.as_posix()] = data.decode(encoding)
    return commit, sources


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index a Python checkout for Code-to-Architecture Linking"
    )
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--repository", required=True, help="Display name for the indexed repository"
    )
    parser.add_argument("--repository-key", help="Key of the existing source Repository")
    parser.add_argument("--commit", help="Index committed Git objects at this ref (for example HEAD)")
    parser.add_argument("--github-repository", help="GitHub owner/repo for commit permalinks")
    args = parser.parse_args()
    if args.commit and not args.repository_key:
        parser.error("--commit requires --repository-key")
    if args.github_repository and not args.commit:
        parser.error("--github-repository requires --commit")
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("root must be a directory")
    try:
        revision = None
        if args.commit:
            sha, sources = load_commit_sources(root, args.commit)
            revision = CodeRevision(
                repository_key=args.repository_key, commit=sha,
                github_repository=args.github_repository,
            )
        else:
            sources = load_sources(root)
        if not sources:
            parser.error("No Python files found")
        memory = build_repository(get_settings())
        memory.architecture.ensure_storage_ready()
        service = ArchitectureService(memory.architecture, memory)
        result = asyncio.run(service.index_python(
            args.repository, sources, repository_key=args.repository_key, revision=revision,
        ))
    except (ArchitectureGraphError, OSError, SyntaxError, UnicodeError, ValidationError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
