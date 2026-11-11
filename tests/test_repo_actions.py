"""Bulk controls must preserve per-repository protection and transaction safety."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import DB, insert_repository
from app.repo_actions import bulk_update


class BulkActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.db.close)
        self.safe = self.add("sync")
        self.protected = self.add("custom", mode="protected", enabled=0, status="paused")
        self.busy = self.add("restoring", restore_backup_id=42, enabled=0, status="paused")
        self.paused = self.add("paused", enabled=0, status="paused")
        self.other = self.add("unselected")

    def add(self, name, **fields):
        id = insert_repository(self.db, owner="me", name=name, full_name="me/" + name,
                               upstream_full_name="up/project")
        if fields:
            self.db.run("UPDATE repositories SET " + ",".join(k + "=?" for k in fields)
                        + " WHERE id=?", (*fields.values(), id))
        return id

    def row(self, id):
        return self.db.one("SELECT * FROM repositories WHERE id=?", (id,))

    def run_action(self, action, *ids):
        return bulk_update(self.db, [str(id) for id in ids], action)

    def test_resume_skips_protected_restore_and_running_and_deduplicates(self):
        result = self.run_action("resume", self.paused, self.paused, self.protected, self.busy, self.safe, 999999)
        self.assertEqual(result["changed"], [self.paused])
        self.assertEqual(len(result["skipped"]), 4)
        self.assertEqual(self.row(self.paused)["sync_requested"], 1)
        self.assertEqual(self.row(self.protected)["enabled"], 0)
        self.assertEqual(self.row(self.protected)["mode"], "protected")
        self.assertEqual(self.row(self.busy)["restore_backup_id"], 42)
        self.assertEqual(self.row(self.other)["sync_requested"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM events")["n"], 1)
        self.assertIn("跳过 4 个", result["notice"])

    def test_sync_does_not_implicitly_resume_or_unlock(self):
        result = self.run_action("sync", self.safe, self.paused, self.protected, self.busy)
        self.assertEqual(result["changed"], [self.safe])
        self.assertEqual(self.row(self.safe)["sync_requested"], 1)
        for id in (self.paused, self.protected, self.busy):
            self.assertEqual(self.row(id)["sync_requested"], 0)
        self.assertEqual(self.run_action("sync", self.safe)["changed"], [])

    def test_pause_cancels_queued_request_but_preserves_mode(self):
        self.db.run("UPDATE repositories SET sync_requested=1,deletion_approval='old' WHERE id=?", (self.safe,))
        self.run_action("pause", self.safe, self.protected)
        self.assertEqual(self.row(self.safe)["enabled"], 0)
        self.assertEqual(self.row(self.safe)["sync_requested"], 0)
        self.assertIsNone(self.row(self.safe)["deletion_approval"])
        self.assertEqual(self.row(self.protected)["mode"], "protected")

    def test_protect_disables_sync_and_cannot_be_undone_by_bulk_resume(self):
        self.db.run("UPDATE repositories SET sync_requested=1,self_update_interval=1,deletion_approval='old'")
        result = self.run_action("protect", self.safe, self.paused, self.busy)
        self.assertEqual(set(result["changed"]), {self.safe, self.paused})
        for id in result["changed"]:
            r = self.row(id)
            self.assertEqual((r["mode"], r["enabled"], r["status"], r["sync_requested"]), ("protected", 0, "paused", 0))
            self.assertIsNone(r["self_update_interval"])
            self.assertIsNone(r["deletion_approval"])
        self.assertEqual(self.run_action("resume", self.safe, self.paused)["changed"], [])

    def test_remove_preserves_snapshots_and_never_removes_restoring_repo(self):
        self.db.run("INSERT INTO repository_backups(repo_id,full_name,fork_sha,status,created_at,updated_at) "
                    "VALUES(?,?,'old-sha','ready','before','before')", (self.safe, "me/sync"))
        result = self.run_action("remove", self.safe, self.protected, self.busy)
        self.assertEqual(set(result["changed"]), {self.safe, self.protected})
        self.assertIsNotNone(self.row(self.busy))
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM repository_backups")["n"], 1)
        self.assertIn("GitHub 仓库和服务器备份未删除", result["notice"])

    def test_bad_input_is_rejected_without_partial_updates(self):
        for ids, action in (([], "pause"), ([str(self.safe), "bad"], "pause"),
                            ([str(self.safe)] * 501, "pause"), (["9" * 100], "pause"),
                            (["-1"], "pause"), (["0"], "pause"), (["١"], "pause"),
                            ([str(self.safe)], "mode")):
            with self.subTest(ids=ids[:2], action=action):
                with self.assertRaises(ValueError):
                    bulk_update(self.db, ids, action)
                self.assertEqual(self.row(self.safe)["enabled"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM events")["n"], 0)

    def test_database_failure_rolls_back_entire_batch_and_events(self):
        self.db.run(f"CREATE TEMP TRIGGER fail_second BEFORE UPDATE ON repositories WHEN OLD.id={self.other} "
                    "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_action("pause", self.safe, self.other)
        self.assertEqual(self.row(self.safe)["enabled"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM events")["n"], 0)


if __name__ == "__main__":
    unittest.main()
