import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from pydantic import ValidationError

from app.domain.exceptions import ArchitectureGraphError
from app.domain.knowledge_snapshot import KnowledgeSnapshot, compare_knowledge
from app.domain.models import ArchitecturalDecisionRecord
from app.domain.project_guidance import PublishedGuidance
from app.repositories import knowledge_git
from scripts import knowledge_snapshot

REVISION = "a" * 40


def adr(key: str, **changes) -> ArchitecturalDecisionRecord:
    return ArchitecturalDecisionRecord.model_validate({
        "key": key,
        "title": "Data lifecycle",
        "date": "2026-06-01",
        "effective_date": "2026-06-02",
        "status": "accepted",
        "content": "Stored content",
        "decision": "Preserve approved records",
        "rationale": "Auditability",
        "consequences": "Keep older revisions",
        "metadata": {"evidence": ["review/42"]},
        **changes,
    })


class KnowledgeSnapshotTests(TestCase):
    def test_round_trip_preserves_content_and_approval(self):
        guidance = PublishedGuidance(
            text="# Project guidance\n\nKeep approved decisions.",
            edited_by="writer", edited_at="2026-06-01T12:00:00Z",
            approved_by="admin", published_at="2026-06-02T12:00:00Z", version=4,
        )
        record = adr("b", source="review", source_id="42", embedding=[1.0])
        exported = KnowledgeSnapshot.from_database(
            "project", "repo", REVISION, [record, adr("a")], guidance,
        )
        payload = exported.portable_json()
        restored = KnowledgeSnapshot.model_validate_json(json.dumps(payload))
        self.assertEqual(restored.portable_json(), payload)
        self.assertEqual([item.key for item in restored.adrs], ["a", "b"])
        self.assertNotIn("embedding", payload["adrs"][1])
        self.assertEqual(payload["adrs"][1]["metadata"], {"evidence": ["review/42"]})
        self.assertEqual(payload["guidance"]["approved_by"], "admin")

    def test_legacy_snapshot_derives_due_date_and_preserves_existing_schedule(self):
        base = KnowledgeSnapshot.from_database("project", "repo", REVISION, [], None)
        restored = KnowledgeSnapshot.model_validate({
            **base.portable_json(),
            "adrs": [
                adr("general").model_dump(),
                adr("security", tags=[" Security "], last_validated="2026-07-01").model_dump(),
                adr("scheduled", next_review_due="2026-07-04").model_dump(),
            ],
        })
        self.assertEqual(
            {record.key: record.next_review_due.isoformat() for record in restored.adrs},
            {"general": "2026-08-30", "scheduled": "2026-07-04", "security": "2026-07-31"},
        )

    def test_drift_reports_both_sides_and_occupied_keys(self):
        git = KnowledgeSnapshot.from_database(
            "project", "repo", REVISION, [adr("changed"), adr("new"), adr("collision")], None,
        )
        database = KnowledgeSnapshot.from_database(
            "project", "repo", "b" * 40, [adr("changed", content="DB edit"), adr("removed")], None,
        )
        drift = compare_knowledge(git, database, {"adr/collision": {"status": "proposed"}})
        self.assertEqual([item.status for item in drift], [
            "different", "occupied", "git_only", "database_only",
        ])
        self.assertEqual(drift[0].git["content"], "Stored content")
        self.assertEqual(drift[0].database["content"], "DB edit")

    def test_rejects_unapproved_duplicate_and_unknown_records(self):
        snapshot = KnowledgeSnapshot.from_database("project", "repo", REVISION, [adr("a")], None)
        for records in (
            [adr("a", status="proposed").model_dump()],
            [adr("a").model_dump()] * 2,
            [{**adr("a").model_dump(), "unknown": "would be discarded"}],
        ):
            with self.subTest(records=records), self.assertRaises(ValidationError):
                KnowledgeSnapshot.model_validate({**snapshot.portable_json(), "adrs": records})

    def test_cli_check_reads_committed_blob_and_reports_both_revisions(self):
        snapshot = KnowledgeSnapshot.from_database("project", "repo", REVISION, [adr("a")], None)
        commit = "b" * 40
        output = StringIO()
        argv = [
            "knowledge_snapshot", "check", "--root", ".", "--ref", "review",
            "--project", "project", "--repository", "repo",
        ]
        with (
            patch("sys.argv", argv),
            patch.object(knowledge_git, "git", side_effect=[
                (commit + "\n").encode(), json.dumps(snapshot.portable_json()).encode(),
            ]) as git,
            patch.object(knowledge_snapshot, "get_settings"),
            patch.object(knowledge_snapshot, "build_repository") as build_repository,
            redirect_stdout(output),
        ):
            reconcile = build_repository.return_value.reconcile_knowledge
            reconcile.return_value = (compare_knowledge(
                snapshot, KnowledgeSnapshot.from_database("project", "repo", commit, [], None), {},
            ), False)
            self.assertEqual(knowledge_snapshot.main(), 1)

        git.assert_called_with(Path("."), "cat-file", "blob", f"{commit}:knowledge/approved.json")
        reconcile.assert_called_once_with(snapshot, commit, root=Path("."), apply=False)
        report = json.loads(output.getvalue())
        self.assertEqual((report["git_revision"], report["source_revision"]), (commit, REVISION))
        self.assertEqual(report["drift"][0]["status"], "git_only")


    def test_checkout_and_commit_provenance(self):
        with TemporaryDirectory() as directory:
            trusted = Path(directory) / "trusted"
            unrelated = Path(directory) / "unrelated"
            for root in (trusted, unrelated):
                root.mkdir()
                knowledge_git.git(root, "init", "-q")

            def commit(root, message, parents=()):
                tree = knowledge_git.git(root, "write-tree").decode().strip()
                return knowledge_git.git(
                    root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit-tree", tree, *parents, "-m", message,
                ).decode().strip()

            source = commit(trusted, "Source")
            other = commit(trusted, "Unrelated history")

            def snapshot_commit(root, source_revision, parent=None):
                snapshot = KnowledgeSnapshot.from_database(
                    "project", "repo", source_revision, [adr("a")], None,
                )
                path = root / knowledge_git.SNAPSHOT_PATH
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(snapshot.portable_json()))
                knowledge_git.git(root, "add", knowledge_git.SNAPSHOT_PATH)
                parents = ("-p", parent) if parent else ()
                return snapshot, commit(root, "Snapshot", parents)

            valid, revision = snapshot_commit(trusted, source, source)
            knowledge_git.verify_checkout(trusted, str(trusted), revision, valid)
            worktree = Path(directory) / "worktree"
            knowledge_git.git(trusted, "worktree", "add", "--detach", str(worktree), revision)
            knowledge_git.verify_checkout(worktree, str(trusted), revision, valid)

            fake, fake_revision = snapshot_commit(unrelated, source)
            with self.assertRaisesRegex(ArchitectureGraphError, "does not belong"):
                knowledge_git.verify_checkout(unrelated, str(trusted), fake_revision, fake)

            for claimed in (other, REVISION):
                with self.subTest(claimed=claimed):
                    invalid, bad_revision = snapshot_commit(trusted, claimed, source)
                    with self.assertRaisesRegex(ArchitectureGraphError, "ancestry"):
                        knowledge_git.verify_checkout(trusted, str(trusted), bad_revision, invalid)

            with self.assertRaisesRegex(ArchitectureGraphError, "committed blob"):
                knowledge_git.verify_checkout(
                    trusted, str(trusted), revision,
                    KnowledgeSnapshot.from_database("project", "repo", source, [], None),
                )
