"""Deletion safety contracts across policy, persistent reviews and sync paths."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import engine
from app.db import DB, insert_repository
from app.github import AuthError, RateLimited, RefNotFastForward, RefObjectNotFound
from app.protection import (assess_change, deletion_policy, load_review,
                            parse_limits, review_json, review_token, valid_comparison)
from test_engine import StubGitHub, node


class ProtectionTests(unittest.TestCase):
    fork_sha = "a" * 40
    target_sha = "b" * 40
    next_sha = "c" * 40
    base_sha = "d" * 40

    def setUp(self):
        self.backup = mock.patch("app.engine.ensure_backup", return_value={"status": "ready"})
        self.backup.start()
        self.addCleanup(self.backup.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "forkiq.db")
        self.addCleanup(self.db.close)
        self.repo_id = insert_repository(
            self.db, owner="me", name="demo", full_name="me/demo",
            upstream_full_name="up/demo", node_id="N1")
        self.client = StubGitHub()
        self.client.refs["me/demo"] = self.fork_sha
        self.observe()
        self.client.file_trees["me/demo", self.fork_sha] = {"one", "two", "three", "four"}
        self.client.file_trees["up/demo", self.target_sha] = {"one", "two", "three", "four", "new"}

    def row(self):
        return self.db.one("SELECT * FROM repositories WHERE id=?", (self.repo_id,))

    def observe(self, fork=None, target=None):
        self.client.nodes["N1"] = node(
            "N1", "me/demo", "up/demo", fork or self.fork_sha, target or self.target_sha)

    def configure(self, **fields):
        assignments = ", ".join(name + "=?" for name in fields)
        self.db.run(f"UPDATE repositories SET {assignments} WHERE id=?",
                    (*fields.values(), self.repo_id))

    def dangerous_target(self, sha=None):
        self.client.file_trees["up/demo", sha or self.target_sha] = {"one"}

    def approve(self):
        review = load_review(self.row())
        self.assertIsNotNone(review)
        self.assertFalse(review["error"])
        self.configure(deletion_approval=review_token(review), sync_requested=1)
        return review

    def assert_no_mutations(self):
        mutations = {"update_ref", "merge_commit", "merge_upstream", "dispatch_workflow",
                     "set_actions_enabled", "enable_workflow"}
        self.assertFalse([call for call in self.client.calls if call[0] in mutations])
        self.assertEqual(self.client.refs["me/demo"], self.fork_sha)
        self.assertFalse(self.client.forced_refs)
        self.assertFalse(self.client.dispatches)

    def assert_blocked(self):
        self.assertEqual(self.row()["status"], "needs_attention")
        self.assertIn("异常删除保护", self.row()["last_error"])
        self.assertIn(self.row()["upstream_sha"], self.row()["last_error"])
        self.assert_no_mutations()

    def manual_mode(self):
        self.configure(mode="manual", self_update_interval=1)
        self.client.compare_result = {"ahead_by": 2, "behind_by": 0,
                                      "merge_base_commit": {"sha": self.base_sha}}
        self.client.file_trees["me/demo", self.base_sha] = {"one", "two", "three", "four"}
        self.client.workflows = [{"id": 1, "name": "Update data", "path": ".github/workflows/update.yml",
                                  "state": "active"}]
        self.client.workflow_files[".github/workflows/update.yml"] = "on:\n  workflow_dispatch:\n"

    def test_defaults_enable_protection_for_existing_and_new_repositories(self):
        expected = {"enabled": True, "max_files": 100, "max_percent": 50}
        self.assertEqual(deletion_policy(self.db), expected)
        self.assertEqual(deletion_policy(self.db, self.row()), expected)
        self.assertIsNone(self.row()["deletion_protection"])

    def test_repository_overrides_and_inherited_fields_remain_independent(self):
        self.db.set_setting("deletion_protection", "0")
        self.db.set_setting("deletion_max_files", "12")
        self.db.set_setting("deletion_max_percent", "25.5")
        self.assertEqual(deletion_policy(self.db, self.row()),
                         {"enabled": False, "max_files": 12, "max_percent": 25.5})
        self.configure(deletion_protection=1, deletion_max_files=7)
        self.assertEqual(deletion_policy(self.db, self.row()),
                         {"enabled": True, "max_files": 7, "max_percent": 25.5})
        self.configure(deletion_protection=None, deletion_max_files=None, deletion_max_percent=10)
        self.assertEqual(deletion_policy(self.db, self.row()),
                         {"enabled": False, "max_files": 12, "max_percent": 10})

    def test_invalid_thresholds_are_rejected_instead_of_silently_changed(self):
        for files, percent in ((-1, 50), (1_000_001, 50), ("1.5", 50), (1.5, 50), (True, 50),
                               (10, -1), (10, 101), (10, "nan"), (10, "inf"), (None, 50)):
            with self.subTest(files=files, percent=percent), self.assertRaises(ValueError):
                parse_limits(files, percent)
        self.assertEqual(parse_limits("0", "0"), (0, 0))
        self.assertEqual(parse_limits("1000000", "100"), (1_000_000, 100))

    def test_complete_empty_target_is_blocked_even_when_thresholds_would_allow_every_deletion(self):
        self.configure(deletion_max_files=1_000_000, deletion_max_percent=100)
        self.client.file_trees["up/demo", self.target_sha] = set()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        review = load_review(self.row())
        self.assertEqual(review["target_files"], 0)
        self.assertEqual(review["deleted_files"], 4)
        self.assertEqual(review["deleted_percent"], 100)
        self.assertIn("上游目标文件树为空", review["reasons"])

    def test_empty_target_also_blocks_an_empty_current_repository(self):
        self.client.file_trees["me/demo", self.fork_sha] = set()
        self.client.file_trees["up/demo", self.target_sha] = set()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertEqual(load_review(self.row())["deleted_percent"], 0)

    def test_deletion_count_alone_blocks_when_percentage_is_allowed(self):
        self.configure(deletion_max_files=1, deletion_max_percent=100)
        self.client.file_trees["up/demo", self.target_sha] = {"one", "two"}
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        review = load_review(self.row())
        self.assertEqual(review["deleted_files"], 2)
        self.assertEqual(review["deleted_percent"], 50)
        self.assertEqual(review["reasons"], ["删除文件数超过 1 个"])

    def test_deletion_percentage_alone_blocks_when_count_is_allowed(self):
        self.configure(deletion_max_files=100, deletion_max_percent=49)
        self.client.file_trees["up/demo", self.target_sha] = {"one", "two"}
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertEqual(load_review(self.row())["reasons"], ["删除比例超过 49%"])

    def test_deletions_equal_to_both_thresholds_are_allowed(self):
        self.configure(deletion_max_files=2, deletion_max_percent=50)
        self.client.file_trees["up/demo", self.target_sha] = {"one", "two"}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "synced")
        self.assertEqual(self.client.refs["me/demo"], self.target_sha)

    def test_mirror_blocks_before_both_the_fast_forward_and_force_attempts(self):
        self.dangerous_target()
        self.client.ref_error["me/demo"] = RefNotFastForward("non-fast-forward")
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertIn("me/demo", self.client.ref_error)

    def test_explicit_sync_request_does_not_bypass_the_guard(self):
        self.dangerous_target()
        self.configure(sync_requested=1)
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertEqual(self.row()["sync_requested"], 0)

    def test_manual_merge_with_many_upstream_deletions_is_blocked_without_selfupdate(self):
        self.manual_mode()
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertEqual(load_review(self.row())["deleted_files"], 3)

    def test_manual_mode_does_not_count_fork_only_files_as_upstream_deletions(self):
        self.manual_mode()
        self.configure(deletion_max_files=0, deletion_max_percent=0, self_update_interval=None)
        self.client.file_trees["me/demo", self.fork_sha] |= {"fork-only-1", "fork-only-2", "fork-only-3"}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "synced")
        self.assertIn(("update_ref", "me/demo", "main", self.target_sha), self.client.calls)
        self.assertIn(("file_paths", "me/demo", self.base_sha), self.client.calls)

    def test_manual_deletion_counts_use_the_intersection_with_files_still_in_the_fork(self):
        self.manual_mode()
        self.client.file_trees["me/demo", self.fork_sha] = {"one", "four", "fork-only"}
        self.client.file_trees["up/demo", self.target_sha] = {"one", "two", "three", "new"}
        self.configure(deletion_max_files=0, deletion_max_percent=100)
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        review = load_review(self.row())
        self.assertEqual(review["deleted_files"], 1)
        self.assertAlmostEqual(review["deleted_percent"], 100 / 3)

    def test_unavailable_or_truncated_tree_cannot_trigger_any_remote_mutation(self):
        self.manual_mode()
        for full_name, sha in (("me/demo", self.fork_sha), ("up/demo", self.target_sha),
                               ("me/demo", self.base_sha)):
            with self.subTest(full_name=full_name, sha=sha):
                self.client.calls.clear()
                self.client.tree_errors = {(full_name, sha): RuntimeError("文件树被截断，无法核实")}
                self.configure(status="pending", deletion_review=None, last_error=None)
                engine.run_cycle(self.db, self.client)
                self.assert_blocked()
                review = load_review(self.row())
                self.assertIn("截断", review["error"])
                self.assertIn("检测未完成", self.row()["last_error"])

    def test_missing_common_ancestor_and_failed_compare_fail_closed_in_manual_mode(self):
        self.manual_mode()
        for result, error in ((None, False), ({"ahead_by": 1, "merge_base_commit": {}}, False),
                               ({"ahead_by": 1}, True)):
            with self.subTest(result=result, error=error):
                self.client.calls.clear()
                self.client.compare_result, self.client.compare_error = result, error
                self.configure(mode="manual", enabled=1, status="pending", deletion_review=None, last_error=None)
                engine.run_cycle(self.db, self.client)
                self.assertEqual(self.row()["status"], "paused")
                self.assertEqual(self.row()["mode"], "protected")
                self.assert_no_mutations()
                self.assertIsNone(load_review(self.row()))


    def test_incomplete_compare_counts_cannot_claim_already_synced_or_permit_manual_merges(self):
        self.manual_mode()
        self.client.file_trees["up/demo", self.target_sha] = set()
        cases = ({}, {"ahead_by": 0}, {"behind_by": 0},
                 {"ahead_by": None, "behind_by": 0},
                 {"ahead_by": False, "behind_by": 0},
                 {"ahead_by": -1, "behind_by": 0},
                 {"ahead_by": "0", "behind_by": 0},
                 {"ahead_by": 0.0, "behind_by": 0},
                 {"ahead_by": 1, "behind_by": None},
                 {"ahead_by": 1, "behind_by": False})
        for comparison in cases:
            with self.subTest(comparison=comparison):
                self.client.calls.clear()
                self.client.compare_result = comparison
                self.configure(mode="manual", enabled=1, status="pending", deletion_review=None, last_error=None)
                self.assertFalse(valid_comparison(comparison))
                engine.run_cycle(self.db, self.client)
                self.assertEqual(self.row()["status"], "paused")
                self.assertEqual(self.row()["mode"], "protected")
                self.assert_no_mutations()
                self.assertIsNone(load_review(self.row()))
        self.assertTrue(valid_comparison({"ahead_by": 0, "behind_by": 0}))
        self.assertTrue(valid_comparison({"ahead_by": 2, "behind_by": 0}))


    def test_auth_and_rate_errors_persist_a_visible_review_then_propagate_without_writes(self):
        for error in (AuthError("expired"), RateLimited("API rate limit exceeded")):
            with self.subTest(error=type(error).__name__):
                self.client.tree_errors = {("up/demo", self.target_sha): error}
                self.configure(status="pending", deletion_review=None, last_error=None)
                with self.assertRaises(type(error)):
                    engine.run_cycle(self.db, self.client)
                self.assert_blocked()
                self.assertIn(str(error), load_review(self.row())["error"])

    def test_a_precise_review_confirmation_allows_the_reviewed_target_once(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        review = self.approve()
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "synced")
        self.assertEqual(self.client.refs["me/demo"], self.target_sha)
        self.assertIsNone(self.row()["deletion_approval"])
        self.assertIsNone(load_review(self.row()))
        self.assertEqual(review["identity"]["target_sha"], self.target_sha)
        confirmations = self.db.all("SELECT message FROM events WHERE kind='check'")
        self.assertEqual(sum("一次性确认" in row["message"] for row in confirmations), 1)
        self.client.refs["me/demo"] = self.fork_sha
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()

    def test_guard_consumes_approval_atomically_even_before_a_remote_write(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.approve()
        self.assertTrue(engine._deletion_guard(self.db, self.client, self.row(), self.fork_sha, self.target_sha))
        self.assertIsNone(self.row()["deletion_approval"])
        self.assertFalse(engine._deletion_guard(self.db, self.client, self.row(), self.fork_sha, self.target_sha))
        self.assert_no_mutations()

    def test_a_new_target_invalidates_the_previous_confirmation(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.approve()
        self.dangerous_target(self.next_sha)
        self.observe(target=self.next_sha)
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertEqual(load_review(self.row())["identity"]["target_sha"], self.next_sha)
        self.assertIsNone(self.row()["deletion_approval"])

    def test_changed_fork_baseline_invalidates_confirmation_even_for_the_same_target(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.approve()
        self.client.file_trees["me/demo", self.next_sha] = {"one", "two", "three", "four", "extra"}
        self.client.refs["me/demo"] = self.next_sha
        self.observe(fork=self.next_sha)
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "needs_attention")
        self.assertEqual(self.client.refs["me/demo"], self.next_sha)
        self.assertFalse(any(call[0] in ("update_ref", "merge_commit") for call in self.client.calls))
        self.assertEqual(load_review(self.row())["identity"]["fork_sha"], self.next_sha)
        self.assertIsNone(self.row()["deletion_approval"])

    def test_changed_sync_mode_invalidates_confirmation_for_the_same_commit_pair(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.approve()
        self.manual_mode()
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertEqual(load_review(self.row())["identity"]["mode"], "manual")
        self.assertIsNone(self.row()["deletion_approval"])

    def test_tampered_approval_cannot_override_a_dangerous_review(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.configure(deletion_approval="0" * 64, sync_requested=1)
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertIsNone(self.row()["deletion_approval"])

    def test_policy_change_invalidates_an_approval_even_if_the_same_deletion_is_still_dangerous(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        before = self.approve()
        self.configure(deletion_max_percent=25)
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertNotEqual(review_token(load_review(self.row())), review_token(before))
        self.assertIsNone(self.row()["deletion_approval"])

    def test_legacy_mode_confirmation_fast_forwards_only_the_exact_reviewed_commit(self):
        self.manual_mode()
        self.configure(self_update_interval=None)
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.approve()
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "synced")
        self.assertEqual([call for call in self.client.calls if call[0] == "update_ref"],
                         [("update_ref", "me/demo", "main", self.target_sha)])
        self.assertFalse(any(call[0] in ("merge_commit", "merge_upstream") for call in self.client.calls))
        self.assertIsNone(self.row()["deletion_approval"])

    def test_failed_detection_review_cannot_be_approved_by_hashing_its_error_payload(self):
        self.client.tree_errors["up/demo", self.target_sha] = RuntimeError("incomplete tree")
        engine.run_cycle(self.db, self.client)
        review = load_review(self.row())
        self.assertTrue(review["error"])
        self.configure(deletion_approval=review_token(review), sync_requested=1)
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertIsNone(self.row()["deletion_approval"])

    def test_missing_object_refresh_to_a_new_dangerous_target_is_reviewed_again(self):
        self.dangerous_target(self.next_sha)
        self.client.refs["up/demo"] = self.next_sha
        self.client.ref_error["me/demo"] = RefObjectNotFound("Object does not exist")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "needs_attention")
        self.assertEqual(self.client.refs["me/demo"], self.fork_sha)
        updates = [call for call in self.client.calls if call[0] == "update_ref"]
        self.assertEqual(updates, [("update_ref", "me/demo", "main", self.target_sha)])
        self.assertFalse(self.client.forced_refs)
        self.assertEqual(load_review(self.row())["identity"]["target_sha"], self.next_sha)
        self.assertIn(("file_paths", "up/demo", self.next_sha), self.client.calls)

    def test_approval_of_old_target_does_not_approve_a_refreshed_dangerous_target(self):
        self.dangerous_target()
        self.dangerous_target(self.next_sha)
        engine.run_cycle(self.db, self.client)
        self.approve()
        self.client.refs["up/demo"] = self.next_sha
        self.client.ref_error["me/demo"] = RefObjectNotFound("Object does not exist")
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "needs_attention")
        self.assertEqual(self.client.refs["me/demo"], self.fork_sha)
        self.assertEqual([call for call in self.client.calls if call[0] == "update_ref"],
                         [("update_ref", "me/demo", "main", self.target_sha)])
        self.assertEqual(load_review(self.row())["identity"]["target_sha"], self.next_sha)
        self.assertIsNone(self.row()["deletion_approval"])

    def test_identical_dangerous_snapshot_reuses_tree_counts_without_repeating_events(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        first = load_review(self.row())
        first_events = self.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='blocked'")["n"]
        first_reads = len([call for call in self.client.calls if call[0] == "file_paths"])
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertEqual(load_review(self.row()), first)
        self.assertEqual(len([call for call in self.client.calls if call[0] == "file_paths"]), first_reads)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='blocked'")["n"], first_events)

    def test_threshold_changes_reassess_cached_counts_instead_of_reusing_an_old_decision(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        first_reads = len([call for call in self.client.calls if call[0] == "file_paths"])
        self.configure(deletion_max_files=100, deletion_max_percent=100)
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "synced")
        self.assertEqual(self.client.refs["me/demo"], self.target_sha)
        self.assertEqual(len([call for call in self.client.calls if call[0] == "file_paths"]), first_reads)

    def test_detection_failure_never_reuses_a_stale_confirmation(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.approve()
        self.client.get_ref_error["me/demo"] = RuntimeError("cannot verify current HEAD")
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertIn("cannot verify", load_review(self.row())["error"])
        self.assertIsNone(self.row()["deletion_approval"])

    def test_changing_fork_during_detection_blocks_the_write(self):
        get_ref = self.client.get_ref
        reads = 0

        def moving_ref(full_name, branch):
            nonlocal reads
            reads += full_name == "me/demo"
            return self.next_sha if full_name == "me/demo" and reads > 1 else get_ref(full_name, branch)

        with mock.patch.object(self.client, "get_ref", side_effect=moving_ref):
            engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertIn("检测期间", load_review(self.row())["error"])

    def test_disabling_protection_is_explicit_and_discards_old_approvals(self):
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.approve()
        self.configure(deletion_protection=0)
        self.client.calls.clear()
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "synced")
        self.assertEqual(self.client.refs["me/demo"], self.target_sha)
        self.assertFalse(any(call[0] == "file_paths" for call in self.client.calls))
        self.assertIsNone(self.row()["deletion_approval"])

    def test_repository_enabled_override_protects_a_system_with_protection_disabled(self):
        self.db.set_setting("deletion_protection", "0")
        self.configure(deletion_protection=1)
        self.dangerous_target()
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()

    def test_invalid_policy_fails_closed_with_a_visible_reason(self):
        self.db.set_setting("deletion_protection", "invalid")
        engine.run_cycle(self.db, self.client)
        self.assert_blocked()
        self.assertIn("开关配置无效", load_review(self.row())["error"])

    def test_normal_new_files_continue_to_fast_forward_automatically(self):
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "synced")
        self.assertEqual(self.client.refs["me/demo"], self.target_sha)
        self.assertFalse(self.db.all("SELECT * FROM events WHERE kind='blocked'"))

    def test_review_tokens_are_canonical_and_cover_the_target_policy_and_counts(self):
        review = assess_change(self.client, self.row(), self.fork_sha, self.target_sha,
                               deletion_policy(self.db, self.row()))
        token = review_token(review)
        self.assertEqual(token, review_token(dict(reversed(list(review.items())))))
        for altered in ({**review, "deleted_files": 1},
                        {**review, "identity": {**review["identity"], "target_sha": self.next_sha}},
                        {**review, "policy": {**review["policy"], "max_files": 1}}):
            self.assertNotEqual(token, review_token(altered))
        self.configure(deletion_review=review_json(review))
        self.assertEqual(load_review(self.row()), review)
        for invalid in ("not json", "[]", "null"):
            self.configure(deletion_review=invalid)
            self.assertIsNone(load_review(self.row()))


class LegacyProtectionMigrationTests(unittest.TestCase):
    def test_existing_repository_credentials_and_pauses_survive_additive_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            legacy = sqlite3.connect(path)
            legacy.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO settings VALUES('github_app_id', '123');
                INSERT INTO settings VALUES('check_interval', '300');
                CREATE TABLE repositories(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT, owner TEXT NOT NULL,
                  name TEXT NOT NULL, full_name TEXT NOT NULL UNIQUE, upstream_full_name TEXT,
                  branch TEXT NOT NULL DEFAULT 'main', mode TEXT NOT NULL DEFAULT 'mirror',
                  enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending',
                  upstream_sha TEXT, fork_sha TEXT, upstream_tag TEXT, fork_tag TEXT,
                  upstream_commit_at TEXT, fork_commit_at TEXT, upstream_gone INTEGER NOT NULL DEFAULT 0,
                  last_success_at TEXT, last_check_at TEXT, last_error TEXT, diverged_at TEXT,
                  diverged_upstream_sha TEXT, last_merged_upstream_sha TEXT, upstream_ahead INTEGER,
                  fork_ahead INTEGER, self_update TEXT, self_update_interval INTEGER, actions_disabled INTEGER,
                  discovered INTEGER NOT NULL DEFAULT 0, is_archived INTEGER NOT NULL DEFAULT 0,
                  sync_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO repositories(id, owner, name, full_name, upstream_full_name, mode,
                                         enabled, status, last_error, fork_sha, created_at, updated_at)
                  VALUES(7, 'me', 'saved', 'me/saved', 'up/saved', 'manual', 0, 'paused',
                         'kept error', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'before', 'before');
            """)
            before = legacy.execute("SELECT * FROM repositories WHERE id=7").fetchone()
            names = [item[1] for item in legacy.execute("PRAGMA table_info(repositories)")]
            legacy.commit()
            legacy.close()
            migrated = DB(path)
            try:
                row = migrated.one("SELECT * FROM repositories WHERE id=7")
                self.assertEqual(tuple(row[name] for name in names if name != "mode"),
                                 tuple(value for name, value in zip(names, before) if name != "mode"))
                self.assertEqual(row["mode"], "protected")
                for name in ("deletion_protection", "deletion_max_files", "deletion_max_percent",
                             "deletion_review", "deletion_approval"):
                    self.assertIsNone(row[name])
                self.assertEqual(deletion_policy(migrated, row),
                                 {"enabled": True, "max_files": 100, "max_percent": 50})
                self.assertEqual(migrated.get_setting("github_app_id"), "123")
                self.assertEqual(migrated.get_setting("check_interval"), "300")
                migrated.init()
                self.assertEqual(migrated.one("SELECT status FROM repositories WHERE id=7")["status"], "paused")
            finally:
                migrated.close()


if __name__ == "__main__":
    unittest.main()
