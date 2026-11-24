"""Local Git provenance checks for approved knowledge snapshots."""

import os
import subprocess
from pathlib import Path

from app.domain.exceptions import ArchitectureGraphError
from app.domain.knowledge_snapshot import KnowledgeSnapshot

SNAPSHOT_PATH = "knowledge/approved.json"


def git(root: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_GRAFT_FILE"] = os.devnull
    env["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(root), *args],
        env=env, capture_output=True, check=True,
    ).stdout


def resolve_commit(root: Path, ref: str) -> str:
    return git(
        root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
    ).decode("ascii").strip()


def read_snapshot(root: Path, revision: str) -> KnowledgeSnapshot:
    return KnowledgeSnapshot.model_validate_json(
        git(root, "cat-file", "blob", f"{revision}:{SNAPSHOT_PATH}")
    )


def verify_checkout(
    root: Path, trusted_root: object, revision: str, snapshot: KnowledgeSnapshot | None,
) -> None:
    if not isinstance(trusted_root, str) or not Path(trusted_root).is_absolute():
        raise ArchitectureGraphError("The linked repository requires an absolute git_checkout path")
    trusted = Path(trusted_root)

    def common_dir(path: Path) -> Path:
        return Path(os.fsdecode(git(
            path, "rev-parse", "--path-format=absolute", "--git-common-dir",
        ).removesuffix(b"\n"))).resolve()

    try:
        if common_dir(root) != common_dir(trusted):
            raise ArchitectureGraphError("The selected checkout does not belong to the linked repository")
        if resolve_commit(trusted, revision) != revision:
            raise ArchitectureGraphError("The selected revision must be an exact Git commit ID")
        if snapshot is not None:
            source = snapshot.source_revision
            if resolve_commit(trusted, source) != source:
                raise ArchitectureGraphError("Snapshot source_revision must be an exact Git commit ID")
            git(trusted, "merge-base", "--is-ancestor", source, revision)
            if read_snapshot(trusted, revision) != snapshot:
                raise ArchitectureGraphError("Snapshot does not match the linked repository's committed blob")
    except subprocess.CalledProcessError as exc:
        raise ArchitectureGraphError(
            "Cannot verify checkout or commit ancestry for the linked repository"
        ) from exc
