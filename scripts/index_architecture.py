from __future__ import annotations

import argparse
import asyncio
import os
import tokenize
from pathlib import Path

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index a Python checkout for Code-to-Architecture Linking"
    )
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--repository", required=True, help="Stable name for the repository snapshot"
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("root must be a directory")
    try:
        sources = load_sources(root)
        if not sources:
            parser.error("No Python files found")
        memory = build_repository(get_settings())
        memory.architecture.ensure_storage_ready()
        service = ArchitectureService(memory.architecture, memory)
        result = asyncio.run(service.index_python(args.repository, sources))
    except (ArchitectureGraphError, OSError, SyntaxError, UnicodeError) as exc:
        parser.error(str(exc))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
