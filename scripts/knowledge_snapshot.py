"""Move approved project knowledge between the local database and Git."""

import argparse
import json
import subprocess
from pathlib import Path

from arango.exceptions import ArangoError
from pydantic import ValidationError

from app.config.settings import get_settings
from app.domain.exceptions import ArchitectureGraphError
from app.repositories.factory import build_repository
from app.repositories.knowledge_git import read_snapshot, resolve_commit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["export", "check", "import", "check-batch", "import-batch"]
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--project", required=True)
    parser.add_argument("--repository", help="Linked source Repository node key")
    parser.add_argument(
        "--snapshot",
        action="append",
        nargs=3,
        metavar=("ROOT", "REF", "REPOSITORY"),
        help="Repository checkout, Git ref, and linked Repository key. Repeat for batch operations.",
    )
    args = parser.parse_args()

    try:
        if args.action in {"check-batch", "import-batch"}:
            if not args.snapshot or args.repository:
                parser.error("Batch operations require one --snapshot per repository")
            inputs = []
            for checkout, ref, key in args.snapshot:
                root = Path(checkout)
                revision = resolve_commit(root, ref)
                incoming = read_snapshot(root, revision)
                if (incoming.project_key, incoming.repository_key) != (args.project, key):
                    parser.error(
                        "Snapshot ownership does not match the selected project and repository"
                    )
                inputs.append((incoming, revision, root))
            repository = build_repository(get_settings())
            drift, imported = repository.reconcile_knowledge_batch(
                inputs,
                apply=args.action == "import-batch",
            )
            print(
                json.dumps(
                    {
                        "project_key": args.project,
                        "imported": imported,
                        "snapshots": [
                            {"repository_key": item.repository_key, "git_revision": revision}
                            for item, revision, _ in inputs
                        ],
                        "drift": [item.model_dump(mode="json") for item in drift],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return int(bool(drift) and not imported)
        if not args.repository or args.snapshot:
            parser.error("Single-repository operations require --repository")
        revision = resolve_commit(args.root, args.ref)
        incoming = None if args.action == "export" else read_snapshot(args.root, revision)
        if incoming and (incoming.project_key, incoming.repository_key) != (
            args.project,
            args.repository,
        ):
            parser.error("Snapshot ownership does not match the selected project and repository")

        repository = build_repository(get_settings())
        if incoming is None:
            snapshot = repository.export_knowledge(
                args.project,
                args.repository,
                revision,
                root=args.root,
            )
            print(
                json.dumps(snapshot.portable_json(), ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0

        drift, imported = repository.reconcile_knowledge(
            incoming,
            revision,
            root=args.root,
            apply=args.action == "import",
        )
        print(
            json.dumps(
                {
                    "git_revision": revision,
                    "source_revision": incoming.source_revision,
                    "project_key": incoming.project_key,
                    "repository_key": incoming.repository_key,
                    "imported": imported,
                    "drift": [item.model_dump(mode="json") for item in drift],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return int(bool(drift) and not imported)
    except subprocess.CalledProcessError as exc:
        parser.error(exc.stderr.decode(errors="replace").strip())
    except (ArchitectureGraphError, ArangoError, OSError, ValidationError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
