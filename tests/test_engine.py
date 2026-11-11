"""ForkIQ tests: engine state machine + web flows with a stub GitHub client."""
import importlib
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import engine
from app.db import DB, insert_repository
from app.github import RefNotFastForward, RefNotFound, RefObjectNotFound


def node(node_id, fork_full, upstream_full, fork_sha, upstream_sha, branch="main",
         is_fork=True, archived=False, fork_branch=None, parent_branch=None):
    return {
        "id": node_id, "nameWithOwner": fork_full, "isFork": is_fork, "isArchived": archived,
        "defaultBranchRef": {"name": fork_branch or branch,
                             "target": {"oid": fork_sha,
                                        "committedDate": "2026-01-02T03:04:05Z"}
                             if fork_sha else None},
        "refs": {"nodes": [{"name": "v1.2.3"}]},
        "parent": {"nameWithOwner": upstream_full,
                   "defaultBranchRef": {"name": parent_branch or branch,
                                        "target": {"oid": upstream_sha,
                                                   "committedDate": "2026-02-03T04:05:06Z"}
                                        if upstream_sha else None},
                   "refs": {"nodes": [{"name": "v9.9.9"}]}}
        if upstream_full else None,
    }


class StubGitHub:
    """Scriptable stand-in for GitHub; records every mutating call."""

    def __init__(self):
        self.nodes = {}          # node_id -> graphql node
        self.refs = {}           # full_name -> sha
        self.installation = []   # repo dicts for discover
        self.calls = []
        self.ref_error = {}      # full_name -> exception
        self.forced_refs = []    # (full_name, sha) moved with force
        self.get_ref_error = {}  # full_name -> exception (transient read failure)
        self.compare_result = {"ahead_by": 0, "behind_by": 0}
        self.compare_error = False
        self.merge_result = {"merge_type": "merge", "message": "merged"}
        self.merge_error = {}    # full_name -> exception
        self.merge_new_sha = "f" * 40
        self.workflows = []      # list_workflows payload
        self.workflow_files = {}  # path -> text
        self.latest_runs = {}    # workflow_id -> run dict or None
        self.dispatch_error = {}  # workflow_id -> exception
        self.enable_error = {}   # workflow_id -> exception
        self.dispatches = []
        self.enabled_workflows = []
        self.repo_payloads = {}  # full_name -> get_repo payload
        self.upstream_pushed_at = "2020-01-01T00:00:00Z"   # stale by default
        self.actions_enabled_flag = True
        self.file_trees = {}
        self.tree_errors = {}

    # engine API surface
    def graphql(self, query, variables):
        self.calls.append(("graphql", tuple(variables["ids"])))
        return {"nodes": [self.nodes.get(i) for i in variables["ids"]]}

    def update_ref(self, full_name, branch, sha, force=False):
        self.calls.append(("update_ref", full_name, branch, sha))
        if force:
            self.forced_refs.append((full_name, sha))
        if full_name in self.ref_error:
            error = self.ref_error[full_name]
            if isinstance(error, list):
                pending = error
                error = pending.pop(0)
                if not pending:
                    del self.ref_error[full_name]
            else:
                del self.ref_error[full_name]
            raise error
        self.refs[full_name] = sha
        return sha

    def get_ref(self, full_name, branch):
        self.calls.append(("get_ref", full_name))
        if full_name in self.get_ref_error:
            raise self.get_ref_error[full_name]
        if full_name not in self.refs:
            for item in self.nodes.values():
                for candidate in (item, item.get("parent") or {}):
                    ref = candidate.get("defaultBranchRef") or {}
                    if candidate.get("nameWithOwner") == full_name and ref.get("name") == branch:
                        return ref["target"]["oid"]
            raise RefNotFound(f"{full_name} 没有分支 {branch}")
        return self.refs[full_name]

    def compare(self, fork_full, base, head):
        self.calls.append(("compare", fork_full))
        if self.compare_error:
            raise RuntimeError("probe boom")
        if self.compare_result is None:
            return None
        return {"merge_base_commit": {"sha": "a" * 40}, **self.compare_result}

    def file_paths(self, full_name, sha):
        self.calls.append(("file_paths", full_name, sha))
        if (full_name, sha) in self.tree_errors:
            raise self.tree_errors[full_name, sha]
        return set(self.file_trees.get((full_name, sha), {"README.md", "LICENSE", "src/main.py"}))

    def merge_commit(self, full_name, branch, target_sha):
        self.calls.append(("merge_commit", full_name, branch, target_sha))
        if full_name in self.merge_error:
            raise self.merge_error.pop(full_name)
        self.refs[full_name] = self.merge_new_sha
        return {"sha": self.merge_new_sha, **self.merge_result}

    def list_workflows(self, full_name):
        self.calls.append(("list_workflows", full_name))
        return [dict(w) for w in self.workflows]

    def get_repo(self, full_name):
        self.calls.append(("get_repo", full_name))
        if full_name in self.repo_payloads:
            return self.repo_payloads[full_name]
        return {"pushed_at": self.upstream_pushed_at}

    def actions_enabled(self, full_name):
        self.calls.append(("actions_enabled", full_name))
        return self.actions_enabled_flag

    def set_actions_enabled(self, full_name, enabled):
        self.calls.append(("set_actions_enabled", full_name, enabled))
        self.actions_enabled_flag = bool(enabled)

    def get_file_text(self, full_name, path):
        self.calls.append(("get_file_text", path))
        return self.workflow_files.get(path, "")

    def latest_workflow_run(self, full_name, workflow_id):
        self.calls.append(("latest_workflow_run", workflow_id))
        return self.latest_runs.get(workflow_id)

    def dispatch_workflow(self, full_name, workflow_id, ref):
        self.calls.append(("dispatch_workflow", workflow_id, ref))
        if workflow_id in self.dispatch_error:
            raise self.dispatch_error.pop(workflow_id)
        self.dispatches.append((workflow_id, ref))
        return True

    def enable_workflow(self, full_name, workflow_id):
        self.calls.append(("enable_workflow", workflow_id))
        if workflow_id in self.enable_error:
            raise self.enable_error.pop(workflow_id)
        self.enabled_workflows.append(workflow_id)
        for workflow in self.workflows:
            if workflow["id"] == workflow_id:
                workflow["state"] = "active"
        return True

    def installation_repos(self):
        self.calls.append(("installation_repos",))
        return self.installation


class EngineTests(unittest.TestCase):
    def setUp(self):
        backup = mock.patch("app.engine.ensure_backup", return_value={"status": "ready"})
        backup.start()
        self.addCleanup(backup.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "test.db")
        self.client = StubGitHub()
        self.repo_id = insert_repository(
            self.db, owner="me", name="demo", full_name="me/demo",
            upstream_full_name="up/demo", node_id="N1")

    def row(self):
        return self.db.one("SELECT * FROM repositories WHERE id=?", (self.repo_id,))

    def status(self):
        return self.row()["status"]

    def test_identical_marks_synced(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "a" * 40)
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertFalse(any(c[0] == "update_ref" for c in self.client.calls))

    def test_cycle_records_tags_and_commit_dates(self):
        # The version columns come from the same batched GraphQL node: the
        # newest tag per side plus each default-branch head's commit date.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.refs["me/demo"] = "a" * 40
        engine.run_cycle(self.db, self.client)
        row = self.row()
        self.assertEqual(row["fork_tag"], "v1.2.3")
        self.assertEqual(row["upstream_tag"], "v9.9.9")
        self.assertEqual(row["fork_commit_at"], "2026-01-02T03:04:05Z")
        self.assertEqual(row["upstream_commit_at"], "2026-02-03T04:05:06Z")

    def test_cycle_records_versions_without_tags(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "a" * 40)
        self.client.nodes["N1"]["refs"] = {"nodes": []}
        self.client.nodes["N1"]["parent"]["refs"] = {"nodes": []}
        engine.run_cycle(self.db, self.client)
        row = self.row()
        self.assertIsNone(row["fork_tag"])
        self.assertIsNone(row["upstream_tag"])

    def test_behind_fast_forwards_with_force_false_and_verifies(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.refs["me/demo"] = "a" * 40
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.row()["fork_sha"], "b" * 40)
        self.assertIn(("update_ref", "me/demo", "main", "b" * 40), self.client.calls)
        self.assertIn(("get_ref", "me/demo"), self.client.calls)

    def test_mirror_force_aligns_when_the_fork_holds_its_own_commits(self):
        # A mirror must equal upstream: when a fast-forward is refused because
        # the fork carries commits upstream does not have, the ref is moved onto
        # upstream's head instead of freezing the repository as diverged.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RefNotFastForward("422")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.row()["fork_sha"], "b" * 40)
        self.assertEqual(self.client.refs["me/demo"], "b" * 40)
        self.assertEqual(self.client.forced_refs, [("me/demo", "b" * 40)])
        self.assertEqual(self.row()["fork_ahead"], 0)
        self.assertEqual(self.row()["upstream_ahead"], 0)
        self.assertIsNone(self.row()["last_error"])
        events = [row["message"] for row in self.db.all(
            "SELECT message FROM events WHERE kind='synced' ORDER BY id")]
        self.assertTrue(any("强制对齐" in message for message in events), events)

    def test_mirror_aligns_again_when_upstream_rewrites_history(self):
        # No common ancestor means upstream rewrote its history; the mirror
        # still follows it rather than keeping the old commits.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RefNotFastForward("422")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        # Upstream rewrites again: the same cycle re-aligns without freezing.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "b" * 40, "c" * 40)
        self.client.ref_error["me/demo"] = RefNotFastForward("422")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.client.refs["me/demo"], "c" * 40)
        self.assertEqual(self.client.forced_refs[-1], ("me/demo", "c" * 40))
        self.assertEqual(self.row()["diverged_at"], None)
        self.assertEqual(self.row()["diverged_upstream_sha"], None)

    def test_mirror_does_not_touch_an_aligned_repository(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "b" * 40, "b" * 40)
        self.client.refs["me/demo"] = "b" * 40
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertFalse(any(c[0] == "update_ref" for c in self.client.calls))
        self.assertEqual(self.client.forced_refs, [])

    def test_mirror_force_alignment_after_a_lost_race(self):
        # The fork is strictly behind (someone else advanced upstream between the
        # read and the push); the mirror still ends up equal to upstream.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RefNotFastForward("422")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.client.refs["me/demo"], "b" * 40)

    def test_missing_object_refreshes_upstream_once_and_verifies_new_target(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RefObjectNotFound("Object does not exist")
        self.client.refs["up/demo"] = "c" * 40
        engine.run_cycle(self.db, self.client)
        row = self.row()
        self.assertEqual(row["status"], "synced")
        self.assertEqual((row["fork_sha"], row["upstream_sha"]), ("c" * 40, "c" * 40))
        self.assertIsNone(row["last_error"])
        self.assertEqual(self.client.forced_refs, [])
        self.assertEqual([call for call in self.client.calls if call[0] == "update_ref"], [
            ("update_ref", "me/demo", "main", "b" * 40),
            ("update_ref", "me/demo", "main", "c" * 40)])
        self.assertEqual([call for call in self.client.calls if call[0] == "get_ref"], [
            ("get_ref", "me/demo"), ("get_ref", "me/demo"), ("get_ref", "me/demo"), ("get_ref", "up/demo"),
            ("get_ref", "me/demo"),
            ("get_ref", "me/demo"), ("get_ref", "me/demo"), ("get_ref", "me/demo")])
        for field in ("fork_tag", "upstream_tag", "fork_commit_at", "upstream_commit_at"):
            self.assertIsNone(row[field], field)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='error'")["n"], 0)

    def test_missing_object_can_recover_without_upstream_moving(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RefObjectNotFound("Object does not exist")
        self.client.refs["up/demo"] = "b" * 40
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.row()["upstream_sha"], "b" * 40)
        self.assertEqual(self.row()["upstream_tag"], "v9.9.9")
        self.assertEqual(self.client.forced_refs, [])

    def test_missing_object_during_force_alignment_restarts_with_safe_update(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = [
            RefNotFastForward("Update is not a fast forward"),
            RefObjectNotFound("Object does not exist")]
        self.client.refs["up/demo"] = "c" * 40
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.row()["fork_sha"], "c" * 40)
        self.assertEqual(self.client.forced_refs, [("me/demo", "b" * 40)])

    def test_refreshed_target_is_forced_only_after_explicit_non_fast_forward(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = [
            RefObjectNotFound("Object does not exist"),
            RefNotFastForward("Update is not a fast forward")]
        self.client.refs["up/demo"] = "c" * 40
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.client.forced_refs, [("me/demo", "c" * 40)])
        self.assertEqual(self.row()["fork_sha"], "c" * 40)

    def test_persistent_missing_object_is_bounded_and_keeps_error_backoff(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        error = "update_ref me/demo:main -> 422: Object does not exist"
        self.client.ref_error["me/demo"] = [RefObjectNotFound(error), RefObjectNotFound(error)]
        self.client.refs["up/demo"] = "b" * 40
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        self.assertEqual(self.row()["last_error"], error)
        self.assertEqual(self.client.forced_refs, [])
        self.assertEqual(sum(call[0] == "update_ref" for call in self.client.calls), 2)
        engine.run_cycle(self.db, self.client)
        self.assertEqual(sum(call[0] == "update_ref" for call in self.client.calls), 2)
        self.db.run("UPDATE repositories SET last_check_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                    (self.repo_id,))
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")

    def test_other_ref_validation_error_never_forces_or_refreshes(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RuntimeError("update_ref me/demo:main -> 422: Validation failed")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        self.assertEqual(self.client.forced_refs, [])
        self.assertNotIn(("get_ref", "up/demo"), self.client.calls)
        self.assertEqual(sum(call[0] == "update_ref" for call in self.client.calls), 1)

    def test_recovered_update_must_still_pass_readback_verification(self):
        from unittest import mock

        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RefObjectNotFound("Object does not exist")
        with mock.patch.object(self.client, "get_ref", side_effect=[
                "a" * 40, "a" * 40, "a" * 40, "c" * 40,
                "a" * 40, "a" * 40, "a" * 40, "d" * 40]):
            engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        self.assertIn("不一致", self.row()["last_error"])
        self.assertIsNone(self.row()["last_success_at"])

    def test_manual_mode_never_uses_mirror_missing_object_recovery(self):
        self.db.run("UPDATE repositories SET mode='manual' WHERE id=?", (self.repo_id,))
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 1, "behind_by": 1}
        self.client.merge_error["me/demo"] = RefObjectNotFound("Object does not exist")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        self.assertFalse(any(call[0] == "update_ref" for call in self.client.calls))
        self.assertNotIn(("get_ref", "up/demo"), self.client.calls)
        self.assertEqual(self.client.forced_refs, [])

    def test_missing_object_recovery_does_not_escalate_workflow_permissions(self):
        from app.github import Forbidden

        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = [
            RefObjectNotFound("Object does not exist"),
            Forbidden("Resource not accessible by integration")]
        self.client.refs["up/demo"] = "b" * 40
        self.client.compare_result = {"ahead_by": 1, "behind_by": 0,
                                      "files": [{"filename": ".github/workflows/ci.yml"}]}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        self.assertIn("Workflows 权限", self.row()["last_error"])
        self.assertEqual(self.client.forced_refs, [])
        self.assertFalse(any(call[0].startswith("set_") for call in self.client.calls))

    def test_missing_object_recovery_preserves_auth_and_rate_limit_boundaries(self):
        from app.github import AuthError, RateLimited

        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        for error in (AuthError("token expired"), RateLimited("rate limit exceeded")):
            with self.subTest(error=type(error).__name__):
                self.client.ref_error["me/demo"] = RefObjectNotFound("Object does not exist")
                self.client.get_ref_error["up/demo"] = error
                with self.assertRaises(type(error)):
                    engine.run_cycle(self.db, self.client)
        self.assertEqual(self.client.forced_refs, [])

    def test_same_commit_records_zero_counts(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "a" * 40)
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["upstream_ahead"], 0)
        self.assertEqual(self.row()["fork_ahead"], 0)

    def test_fast_forward_records_zero_counts(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.refs["me/demo"] = "a" * 40
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["upstream_ahead"], 0)
        self.assertEqual(self.row()["fork_ahead"], 0)

    def test_forbidden_with_workflow_changes_reports_actionable_message(self):
        from app.github import Forbidden

        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = Forbidden("Resource not accessible by integration")
        self.client.compare_result = {
            "ahead_by": 2, "behind_by": 0,
            "files": [{"filename": ".github/workflows/ci.yml"}, {"filename": "README.md"}]}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        self.assertIn("Workflows 权限", self.row()["last_error"])

    def test_forbidden_with_unique_commits_marks_diverged_not_error(self):
        # GitHub's workflow check fires before the FF check, so diverged forks
        # can surface as 403; compare with unique commits must classify diverged.
        from app.github import Forbidden

        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = Forbidden("Resource not accessible by integration")
        self.client.compare_result = {"ahead_by": 5, "behind_by": 2,
                                      "files": [{"filename": "README.md"}]}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "diverged")
        self.assertIn("2 个独有提交", self.row()["last_error"])

    def test_forbidden_with_no_common_ancestor_marks_diverged(self):
        from app.github import Forbidden

        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = Forbidden("Resource not accessible by integration")
        self.client.compare_result = None
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "diverged")
        self.assertIn("无共同祖先", self.row()["last_error"])

    def test_error_repo_retried_only_after_backoff(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RuntimeError("boom")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        attempts = sum(1 for c in self.client.calls if c[0] == "update_ref")
        engine.run_cycle(self.db, self.client)  # inside backoff window: no retry
        self.assertEqual(sum(1 for c in self.client.calls if c[0] == "update_ref"), attempts)
        # age the last check beyond the backoff window -> retried
        self.db.run("UPDATE repositories SET last_check_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                    (self.repo_id,))
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")

    def test_sync_requested_forces_retry_and_clears_flag(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RuntimeError("boom")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        # manual sync request bypasses the error backoff
        self.db.run("UPDATE repositories SET sync_requested=1 WHERE id=?", (self.repo_id,))
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.row()["sync_requested"], 0)

    def test_repeated_error_cycle_does_not_duplicate_events(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.ref_error["me/demo"] = RuntimeError("boom")
        engine.run_cycle(self.db, self.client)
        events = self.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='error'")["n"]
        self.db.run("UPDATE repositories SET last_check_at='2020-01-01T00:00:00+00:00' WHERE id=?",
                    (self.repo_id,))
        self.client.ref_error["me/demo"] = RuntimeError("boom")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='error'")["n"], events)

    def test_default_branch_change_needs_attention_and_never_pushes(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40,
                                       fork_branch="trunk", parent_branch="trunk")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "needs_attention")
        self.assertFalse(any(c[0] == "update_ref" for c in self.client.calls))

    def test_uniform_branch_rename_records_new_branch_for_resume(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40,
                                       fork_branch="trunk", parent_branch="trunk")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["branch"], "trunk")
        # resume (as the panel does) -> next cycle syncs on the new branch
        self.db.run("UPDATE repositories SET status='pending' WHERE id=?", (self.repo_id,))
        self.client.refs["me/demo"] = "a" * 40
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertIn(("update_ref", "me/demo", "trunk", "b" * 40), self.client.calls)

    def test_mismatched_branch_rename_keeps_old_branch(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40,
                                       fork_branch="trunk", parent_branch="master")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "needs_attention")
        self.assertEqual(self.row()["branch"], "main")

    def test_parent_removed_blocks(self):
        self.client.nodes["N1"] = node("N1", "me/demo", None, "a" * 40, None)
        engine.run_cycle(self.db, self.client)
        # Upstream deleted: the fork is kept (never removed) and flagged for
        # attention, with its history intact.
        self.assertEqual(self.status(), "needs_attention")
        self.assertEqual(self.row()["upstream_gone"], 1)
        self.assertIn("上游仓库不可见", self.row()["last_error"])

    def test_not_a_fork_blocks(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40, is_fork=False)
        engine.run_cycle(self.db, self.client)
        # Upstream deleted and the fork promoted to standalone: keep managing it.
        self.assertEqual(self.status(), "needs_attention")
        self.assertEqual(self.row()["upstream_gone"], 1)
        self.assertIn("上游仓库已删除", self.row()["last_error"])
        self.assertFalse(any(c[0] == "update_ref" for c in self.client.calls))

    def test_deleted_upstream_fork_is_never_removed_and_recovers(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40, is_fork=False)
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "needs_attention")
        # Repeated cycles keep the row and stay quiet (no duplicate events).
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM repositories")["n"], 1)
        self.assertEqual(self.status(), "needs_attention")
        # Upstream reappears (re-forked): the flag clears and sync resumes.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "a" * 40)
        self.db.run("UPDATE repositories SET status='pending', sync_requested=1 WHERE id=?",
                    (self.repo_id,))
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["upstream_gone"], 0)
        self.assertEqual(self.status(), "synced")

    def test_discover_keeps_fork_whose_upstream_was_deleted(self):
        # Upstream deleted: the fork stays in the installation but `fork` is now
        # false. discover must NOT mark it missing or drop it.
        self.client.installation = [
            {"full_name": "me/demo", "name": "demo", "fork": False, "archived": False,
             "default_branch": "main", "node_id": "N1", "owner": {"login": "me"}},
        ]
        engine.discover(self.db, self.client)
        row = self.row()
        self.assertNotEqual(row["status"], "missing")
        self.assertEqual(row["upstream_gone"], 1)

    def test_discover_marks_inaccessible_fork_missing_without_claiming_deletion(self):
        self.client.installation = []
        engine.discover(self.db, self.client)
        self.assertEqual(self.status(), "missing")
        self.assertIn("授权范围", self.row()["last_error"])
        self.assertIn("可能已删除", self.row()["last_error"])

    def test_upstream_renamed_needs_attention(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "other/demo", "a" * 40, "b" * 40)
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "needs_attention")
        self.assertFalse(any(c[0] == "update_ref" for c in self.client.calls))

    def test_missing_node_marks_missing(self):
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "missing")
        # repeated cycles must not re-log the same missing event
        engine.run_cycle(self.db, self.client)
        self.assertEqual(
            self.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='blocked'")["n"], 1)

    def test_paused_repo_is_untouched(self):
        self.db.run("UPDATE repositories SET status='paused', enabled=0 WHERE id=?", (self.repo_id,))
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "paused")
        self.assertFalse(any(c[0] == "update_ref" for c in self.client.calls))

    def test_one_repo_error_does_not_block_others(self):
        second = insert_repository(self.db, owner="me", name="ok", full_name="me/ok",
                                   upstream_full_name="up/ok", node_id="N2")
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.refs["me/demo"] = "a" * 40
        self.client.nodes["N2"] = node("N2", "me/ok", "up/ok", "c" * 40, "d" * 40)
        self.client.refs["me/ok"] = "c" * 40
        self.client.ref_error["me/ok"] = RuntimeError("boom")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.db.one("SELECT status FROM repositories WHERE id=?", (second,))["status"],
                         "error")

    def test_discover_imports_new_forks_and_marks_missing(self):
        self.client.installation = [
            {"full_name": "me/newfork", "name": "newfork", "fork": True, "archived": False,
             "default_branch": "main", "node_id": "N9", "owner": {"login": "me"},
             "parent": {"full_name": "up/newfork"}},
            {"full_name": "me/notfork", "name": "notfork", "fork": False, "archived": False,
             "default_branch": "main", "node_id": "N10", "owner": {"login": "me"}, "parent": None},
            {"full_name": "someone/other", "name": "other", "fork": True, "archived": False,
             "default_branch": "main", "node_id": "N11", "owner": {"login": "someone"},
             "parent": {"full_name": "up/other"}},
        ]
        # REST /installation/repositories omits `parent`; discover resolves it via
        # GraphQL nodes(ids:) instead.
        self.client.nodes["N9"] = node("N9", "me/newfork", "up/newfork", "a" * 40, "b" * 40)
        self.client.nodes["N11"] = node("N11", "someone/other", "up/other", "c" * 40, "d" * 40)
        added = engine.discover(self.db, self.client)
        self.assertEqual(added, 1)
        row = self.db.one("SELECT * FROM repositories WHERE full_name='me/newfork'")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["upstream_full_name"], "up/newfork")
        self.assertEqual(row["node_id"], "N9")
        # existing repo (me/demo) not in installation list -> missing
        self.assertEqual(self.status(), "missing")

    def test_parse_fork_url_accepts_common_forms(self):
        for url, expected in [
            ("https://github.com/me/demo", ("me", "demo")),
            ("https://github.com/me/demo.git", ("me", "demo")),
            ("https://github.com/me/demo/tree/main", ("me", "demo")),
            ("https://github.com/me/demo/", ("me", "demo")),
        ]:
            self.assertEqual(engine.parse_fork_url(url), expected)
        with self.assertRaises(ValueError):
            engine.parse_fork_url("https://gitlab.com/me/demo")

    def test_add_manual_rejects_non_fork_and_duplicate(self):
        self.client.repo_payloads = {
            "me/plain": {"fork": False, "parent": None, "node_id": "N20", "default_branch": "main",
                         "archived": False},
            "me/fork2": {"fork": True, "parent": {"full_name": "up/fork2"}, "node_id": "N21",
                         "default_branch": "main", "archived": False},
        }
        with self.assertRaises(ValueError):
            engine.add_manual(self.db, self.client, "https://github.com/me/plain")
        self.assertEqual(engine.add_manual(self.db, self.client, "https://github.com/me/fork2"),
                         self.db.one("SELECT id FROM repositories WHERE full_name='me/fork2'")["id"])
        self.assertEqual(
            self.db.one("SELECT node_id FROM repositories WHERE full_name='me/fork2'")["node_id"],
            "N21")
        with self.assertRaises(ValueError):
            engine.add_manual(self.db, self.client, "https://github.com/me/fork2")

    def test_app_client_requires_only_app_id_and_pem(self):
        # Before installation exists, detect must still get an app-level client.
        self.assertIsNone(engine.app_client_from_settings(self.db))
        self.db.set_setting("github_app_id", "1")
        self.db.set_setting("github_pem_path", "/some.pem")
        client = engine.app_client_from_settings(self.db)
        self.assertIsNotNone(client)
        self.assertIsNone(client.installation_id)


class ManualModeTests(unittest.TestCase):
    """手工 fork mode: keep fork-unique commits, merge upstream in."""

    def setUp(self):
        backup = mock.patch("app.engine.ensure_backup", return_value={"status": "ready"})
        backup.start()
        self.addCleanup(backup.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "test.db")
        self.client = StubGitHub()
        self.repo_id = insert_repository(
            self.db, owner="me", name="demo", full_name="me/demo",
            upstream_full_name="up/demo", node_id="N1")
        self.db.run("UPDATE repositories SET mode='manual' WHERE id=?", (self.repo_id,))

    def row(self):
        return self.db.one("SELECT * FROM repositories WHERE id=?", (self.repo_id,))

    def status(self):
        return self.row()["status"]

    def test_fork_ahead_of_upstream_merges_and_keeps_unique_commits(self):
        # Fork has its own commits (behind_by>0) and upstream advanced: mirror
        # mode would freeze as diverged; manual mode must merge instead.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 5, "behind_by": 3}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertIn(("merge_commit", "me/demo", "main", "b" * 40), self.client.calls)
        self.assertEqual(self.row()["last_merged_upstream_sha"], "b" * 40)
        self.assertEqual(self.row()["fork_sha"], "f" * 40)

    def test_upstream_advance_merges_even_when_fork_has_unique_commits(self):
        self.db.run("UPDATE repositories SET last_merged_upstream_sha=? WHERE id=?",
                    ("b" * 40, self.repo_id))
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "c" * 40, "d" * 40)
        self.client.compare_result = {"ahead_by": 1, "behind_by": 3}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertIn(("merge_commit", "me/demo", "main", "d" * 40), self.client.calls)

    def test_already_contains_upstream_is_quiet(self):
        # compare says upstream head is an ancestor (ahead_by==0): record the
        # marker without calling merge-upstream again.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "c" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 0, "behind_by": 2}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertFalse(any(c[0] == "merge_commit" for c in self.client.calls))
        self.assertEqual(self.row()["last_merged_upstream_sha"], "b" * 40)
        # The fork's own commits are counted so the list can say why the two
        # version lines differ while nothing needs syncing.
        self.assertEqual(self.row()["upstream_ahead"], 0)
        self.assertEqual(self.row()["fork_ahead"], 2)

    def test_merge_records_counts_including_the_merge_commit(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 5, "behind_by": 3}
        self.client.merge_result = {"merge_type": "merge", "message": "merged"}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        row = self.row()
        # Upstream is inside the fork now, and the merge commit itself is one
        # more commit the fork has and upstream does not.
        self.assertEqual(row["upstream_ahead"], 0)
        self.assertEqual(row["fork_ahead"], 4)

    def test_fast_forward_merge_leaves_no_own_commits(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 5, "behind_by": 0}
        self.client.merge_result = {"merge_type": "fast-forward", "message": "ok"}
        engine.run_cycle(self.db, self.client)
        row = self.row()
        self.assertEqual(row["upstream_ahead"], 0)
        self.assertEqual(row["fork_ahead"], 0)

    def test_stable_fork_backfills_counts_once(self):
        """加列之前就已稳定的 fork：一次 compare 补上计数，之后不再探测。"""
        self.db.run("UPDATE repositories SET status='synced', fork_sha=?, upstream_sha=?, "
                    "last_merged_upstream_sha=? WHERE id=?",
                    ("c" * 40, "b" * 40, "b" * 40, self.repo_id))
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "c" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 0, "behind_by": 15}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["fork_ahead"], 15)
        self.assertEqual(self.status(), "synced")
        probes = sum(1 for c in self.client.calls if c[0] == "compare")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(sum(1 for c in self.client.calls if c[0] == "compare"), probes)

    def test_failed_count_probe_is_not_retried_every_cycle(self):
        self.db.run("UPDATE repositories SET status='synced', fork_sha=?, upstream_sha=?, "
                    "last_merged_upstream_sha=? WHERE id=?",
                    ("c" * 40, "b" * 40, "b" * 40, self.repo_id))
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "c" * 40, "b" * 40)
        self.client.compare_error = True
        engine.run_cycle(self.db, self.client)
        self.assertIsNone(self.row()["fork_ahead"])
        probes = sum(1 for c in self.client.calls if c[0] == "compare")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(sum(1 for c in self.client.calls if c[0] == "compare"), probes)
        self.assertEqual(self.status(), "synced")

    def test_same_sha_is_quiet(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "a" * 40)
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertFalse(any(c[0] == "merge_commit" for c in self.client.calls))
        self.assertFalse(any(c[0] == "compare" for c in self.client.calls))

    def test_merge_conflict_diverged_and_retries_when_upstream_advances(self):
        from app.github import MergeConflict

        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 5, "behind_by": 3}
        self.client.merge_error["me/demo"] = MergeConflict("409 conflict")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "diverged")
        attempts = sum(1 for c in self.client.calls if c[0] == "merge_commit")
        engine.run_cycle(self.db, self.client)  # same upstream: sticky, no retry
        self.assertEqual(sum(1 for c in self.client.calls if c[0] == "merge_commit"), attempts)
        # upstream advances -> retried
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "c" * 40)
        self.client.merge_error["me/demo"] = MergeConflict("409 conflict")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(sum(1 for c in self.client.calls if c[0] == "merge_commit"), attempts + 1)
        # manual sync forces a retry regardless
        self.db.run("UPDATE repositories SET sync_requested=1 WHERE id=?", (self.repo_id,))
        self.client.merge_error["me/demo"] = MergeConflict("409 conflict")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(sum(1 for c in self.client.calls if c[0] == "merge_commit"), attempts + 2)

    def test_workflow_forbidden_reports_permission_message(self):
        from app.github import Forbidden

        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 5, "behind_by": 3}
        self.client.merge_error["me/demo"] = Forbidden(
            "refusing to allow a GitHub App to create or update workflow without workflows permission")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        self.assertIn("Workflows 权限", self.row()["last_error"])

    def test_manual_sync_button_forces_merge_even_when_marker_matches(self):
        self.db.run("UPDATE repositories SET last_merged_upstream_sha=?, sync_requested=1 WHERE id=?",
                    ("b" * 40, self.repo_id))
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "c" * 40, "b" * 40)
        self.client.compare_result = {"ahead_by": 1, "behind_by": 2}
        engine.run_cycle(self.db, self.client)
        self.assertIn(("merge_commit", "me/demo", "main", "b" * 40), self.client.calls)

    def test_probe_failure_blocks_merge_when_deletion_scope_is_unknown(self):
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        self.client.compare_error = True
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "needs_attention")
        self.assertFalse(any(call[0] == "merge_commit" for call in self.client.calls))
        self.assertIn("检测未完成", self.row()["last_error"])

    def test_mismatched_branch_uses_same_named_upstream_ref(self):
        # Fork default "rm" vs upstream default "master": merge-upstream sources
        # the upstream branch with the SAME name, so when that ref matches the
        # fork it is already synced (this is the real v2ray layout).
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40,
                                       fork_branch="rm", parent_branch="master")
        self.client.refs["up/demo"] = "a" * 40  # upstream rm == fork rm
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "synced")
        self.assertEqual(self.row()["branch"], "rm")
        self.assertFalse(any(c[0] == "merge_commit" for c in self.client.calls))
        self.assertEqual(self.row()["upstream_sha"], "a" * 40)

    def test_mismatched_branch_without_upstream_ref_is_diagnosed(self):
        # Fork default "master", upstream has no master (real SMSBoom layout):
        # no merge is attempted; the actionable diagnosis is recorded instead.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40,
                                       fork_branch="master", parent_branch="main")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "needs_attention")
        self.assertEqual(self.row()["branch"], "master")
        self.assertIn("上游没有与 fork 默认分支同名的分支 master", self.row()["last_error"])
        self.assertFalse(any(c[0] == "merge_commit" for c in self.client.calls))

    def test_transient_upstream_ref_failure_is_an_error_not_missing_branch(self):
        # A flaky upstream read (network/5xx) must not be reported as "upstream
        # has no same-named branch" (the real zzusec/v2ray false alarm).
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40,
                                       fork_branch="rm", parent_branch="master")
        self.client.get_ref_error["up/demo"] = RuntimeError("HTTP 502")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "error")
        self.assertIn("读取上游分支 rm 失败", self.row()["last_error"])
        self.assertNotIn("没有与 fork 默认分支同名", self.row()["last_error"])

    def test_missing_upstream_ref_uses_dedicated_not_found_error(self):
        # A genuine 404 (branch really absent) is still diagnosed as missing.
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40,
                                       fork_branch="rm", parent_branch="master")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.status(), "needs_attention")
        self.assertIn("上游没有与 fork 默认分支同名的分支 rm", self.row()["last_error"])


class SelfUpdateTests(unittest.TestCase):
    """Self-update keep-alive: dispatch a fork's own workflow when stale."""

    def setUp(self):
        backup = mock.patch("app.engine.ensure_backup", return_value={"status": "ready"})
        backup.start()
        self.addCleanup(backup.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "test.db")
        self.client = StubGitHub()
        self.repo_id = insert_repository(
            self.db, owner="me", name="demo", full_name="me/demo",
            upstream_full_name="up/demo", node_id="N1")
        self.db.run("UPDATE repositories SET mode='manual', self_update_interval=1 WHERE id=?",
                    (self.repo_id,))
        self.client.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "a" * 40)
        self.client.workflows = [
            {"id": 10, "name": "Manual Refresh", "state": "active",
             "path": ".github/workflows/manual.yml"},
            {"id": 11, "name": "Update Data", "state": "active",
             "path": ".github/workflows/update.yml"},
        ]
        self.client.workflow_files[".github/workflows/manual.yml"] = (
            "on:\n  workflow_dispatch:\n")
        self.client.workflow_files[".github/workflows/update.yml"] = (
            "on:\n  schedule:\n    - cron: '0 */6 * * *'\n  workflow_dispatch:\n")

    def row(self):
        return self.db.one("SELECT * FROM repositories WHERE id=?", (self.repo_id,))

    def test_stale_workflow_is_dispatched(self):
        self.client.latest_runs[11] = {"created_at": "2020-01-01T00:00:00Z"}
        engine.run_cycle(self.db, self.client)
        # prefers the scheduled data workflow over CI
        self.assertIn((11, "main"), self.client.dispatches)
        self.assertIn("已触发", self.row()["self_update"])

    def test_fresh_workflow_is_not_dispatched(self):
        from datetime import datetime, timezone
        fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.client.latest_runs[11] = {"created_at": fresh}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.client.dispatches, [])
        self.assertEqual(self.row()["self_update"], "ok")

    def test_dispatch_backoff_and_force(self):
        self.client.latest_runs[11] = {"created_at": "2020-01-01T00:00:00Z"}
        engine.run_cycle(self.db, self.client)
        first = len(self.client.dispatches)
        # within the hour: staleness re-check is skipped entirely
        engine.run_cycle(self.db, self.client)
        self.assertEqual(len(self.client.dispatches), first)
        # manual sync forces a re-check + re-dispatch
        self.db.run("UPDATE repositories SET sync_requested=1 WHERE id=?", (self.repo_id,))
        engine.run_cycle(self.db, self.client)
        self.assertEqual(len(self.client.dispatches), first + 1)

    def test_forbidden_records_permission_hint_without_status_change(self):
        from app.github import Forbidden

        self.client.latest_runs[11] = {"created_at": "2020-01-01T00:00:00Z"}
        self.client.dispatch_error[11] = Forbidden("Resource not accessible by integration")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.row()["status"], "synced")
        self.assertIn("actions:write", self.row()["self_update"])

    def test_disabled_fork_workflow_is_enabled_then_dispatched(self):
        # GitHub disables fork workflows by default: the engine enables the
        # chosen workflow itself once permission allows, then kicks off a run.
        self.client.workflows = [
            {"id": 12, "name": "Update Data", "state": "disabled_fork",
             "path": ".github/workflows/update.yml"}]
        self.client.workflow_files[".github/workflows/update.yml"] = (
            "on:\n  schedule:\n    - cron: '0 */6 * * *'\n  workflow_dispatch:\n")
        self.client.latest_runs[12] = None
        engine.run_cycle(self.db, self.client)
        self.assertIn(("enable_workflow", 12), self.client.calls)
        self.assertIn((12, "main"), self.client.dispatches)
        self.assertIn("已触发", self.row()["self_update"])

    def test_disabled_fork_workflow_enable_forbidden_reports_hint(self):
        from app.github import Forbidden

        self.client.workflows = [
            {"id": 12, "name": "Update Data", "state": "disabled_fork",
             "path": ".github/workflows/update.yml"}]
        self.client.workflow_files[".github/workflows/update.yml"] = (
            "on:\n  schedule:\n    - cron: '0 */6 * * *'\n  workflow_dispatch:\n")
        self.client.enable_error[12] = Forbidden("Resource not accessible by integration")
        engine.run_cycle(self.db, self.client)
        self.assertIn("Actions 读写权限", self.row()["self_update"])
        self.assertEqual(self.client.dispatches, [])

    def test_build_workflows_are_never_dispatched(self):
        # Firmware/build pipelines must not be auto-triggered even when they
        # are the only workflows present.
        self.client.workflows = [
            {"id": 20, "name": "编译主程序", "state": "active",
             "path": ".github/workflows/compile.yml"},
            {"id": 21, "name": "Immortalwrt", "state": "active",
             "path": ".github/workflows/Immortalwrt.yml"}]
        self.client.workflow_files[".github/workflows/compile.yml"] = (
            "on:\n  workflow_dispatch:\n  schedule:\n    - cron: '0 0 * * *'\n")
        self.client.workflow_files[".github/workflows/Immortalwrt.yml"] = (
            "on:\n  workflow_dispatch:\n")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.client.dispatches, [])
        self.assertIn("数据型 workflow", self.row()["self_update"])

    def test_workflow_without_dispatch_trigger_is_ineligible(self):
        # A schedule-only workflow cannot be manually kicked, so it is skipped
        # even though its name suggests data production.
        self.client.workflows = [
            {"id": 22, "name": "Update Data", "state": "active",
             "path": ".github/workflows/update.yml"}]
        self.client.workflow_files[".github/workflows/update.yml"] = (
            "on:\n  schedule:\n    - cron: '0 */6 * * *'\n")
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.client.dispatches, [])
        self.assertIn("数据型 workflow", self.row()["self_update"])

    def test_rejected_workflow_is_skipped_next_time(self):
        from app.github import DispatchRejected

        self.client.latest_runs[11] = {"created_at": "2020-01-01T00:00:00Z"}
        self.client.dispatch_error[11] = DispatchRejected("422: bad ref", status=422)
        engine.run_cycle(self.db, self.client)
        self.assertIn("被拒", self.row()["self_update"])
        # next check skips the rejected workflow and falls back to CI
        self.db.run("UPDATE repositories SET sync_requested=1 WHERE id=?", (self.repo_id,))
        self.db.set_setting(f"self_check:{self.repo_id}", "")
        engine.run_cycle(self.db, self.client)
        self.assertIn((10, "main"), self.client.dispatches)

    def test_self_update_disabled_by_default(self):
        self.db.run("UPDATE repositories SET self_update_interval=NULL WHERE id=?",
                    (self.repo_id,))
        engine.run_cycle(self.db, self.client)
        self.assertFalse(any(c[0] == "list_workflows" for c in self.client.calls))

    def test_standby_while_upstream_is_active(self):
        # While upstream still produces commits the fork stays on standby:
        # no enable, no dispatch, and the note explains why.
        from datetime import datetime, timezone
        fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.client.upstream_pushed_at = fresh
        self.client.latest_runs[11] = {"created_at": "2020-01-01T00:00:00Z"}
        engine.run_cycle(self.db, self.client)
        self.assertEqual(self.client.dispatches, [])
        self.assertIn("待命", self.row()["self_update"])
        self.assertFalse(any(c[0] == "list_workflows" for c in self.client.calls))

    def test_takes_over_when_upstream_goes_silent(self):
        # Upstream silent beyond the grace window -> the fork's own workflow
        # is kept alive again (this is the "upstream Actions stopped" case).
        self.client.upstream_pushed_at = "2020-01-01T00:00:00Z"
        self.client.latest_runs[11] = {"created_at": "2020-01-01T00:00:00Z"}
        engine.run_cycle(self.db, self.client)
        self.assertIn((11, "main"), self.client.dispatches)

    def test_disabled_repo_actions_are_enabled_before_dispatch(self):
        # Repos whose Actions were turned off entirely (legacy migration) get
        # repo-level Actions enabled before any workflow handling.
        self.client.actions_enabled_flag = False
        self.client.latest_runs[11] = {"created_at": "2020-01-01T00:00:00Z"}
        engine.run_cycle(self.db, self.client)
        self.assertIn(("set_actions_enabled", "me/demo", True), self.client.calls)
        self.assertIn((11, "main"), self.client.dispatches)


class GraphQLPartialErrorsTests(unittest.TestCase):
    def test_partial_errors_return_usable_data(self):
        # GitHub returns data with nulls plus `errors` when some node ids are
        # unresolvable (blocked repos); that must not fail the whole batch.
        from unittest import mock

        from app.github import GitHub

        client = GitHub()
        payload = {
            "data": {"nodes": [{"id": "N1"}, None]},
            "errors": [{"type": "NOT_FOUND", "path": ["nodes", 1]}],
        }
        with mock.patch.object(client, "installation_token", return_value="tok"), \
             mock.patch.object(client, "_request", return_value=(200, payload, {})):
            data = client.graphql("query($ids:[ID!]!){nodes(ids:$ids){id}}", {"ids": ["N1", "N2"]})
        self.assertEqual(data["nodes"], [{"id": "N1"}, None])

    def test_total_failure_without_data_still_raises(self):
        from unittest import mock

        from app.github import GitHub

        client = GitHub()
        payload = {"data": None, "errors": [{"message": "bad query"}]}
        with mock.patch.object(client, "installation_token", return_value="tok"), \
             mock.patch.object(client, "_request", return_value=(200, payload, {})):
            with self.assertRaises(RuntimeError):
                client.graphql("query($ids:[ID!]!){nodes(ids:$ids){id}}", {"ids": ["N1"]})


class WorkerTickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "w.db")
        self.db.set_setting("github_app_id", "1")
        self.db.set_setting("github_pem_path", "/nonexistent.pem")
        self.db.set_setting("github_installation_id", "42")
        self.db.set_setting("auto_discover", "1")

    def test_worker_waits_for_custom_interval_and_rereads_after_wake(self):
        self.db.set_setting("check_interval", "86400")
        worker = engine.Worker(self.tmp.name, "/nonexistent.pem")
        waits = []
        def wait(timeout):
            waits.append(timeout)
            if len(waits) == 1:
                self.db.set_setting("check_interval", "1800")
                worker.request_sync()
            else:
                worker.stop_flag.set()
        with mock.patch.object(engine, "DB", return_value=self.db), \
             mock.patch.object(worker, "_tick") as tick, \
             mock.patch.object(worker.wake, "wait", side_effect=wait):
            worker.run()
        self.assertEqual(waits, [86400, 1800])
        self.assertEqual(tick.call_count, 2)

    def test_auto_discover_runs_before_sync_and_obeys_hourly_schedule(self):
        from unittest import mock

        order = []

        def record_discover(db, client):
            order.append("discover")
            db.set_setting("last_discover_at", engine.utcnow())

        worker = engine.Worker(self.tmp.name, "/nonexistent.pem")
        with mock.patch.object(engine, "client_from_settings", return_value=object()), \
             mock.patch.object(engine, "discover", side_effect=record_discover), \
             mock.patch.object(engine, "run_cycle", side_effect=lambda db, client: order.append("sync")):
            worker._tick(self.db)
            self.assertEqual(order, ["discover", "sync"])
            worker._tick(self.db)
            self.assertEqual(order, ["discover", "sync", "sync"])
            self.db.set_setting("last_discover_at", "2020-01-01T00:00:00+00:00")
            worker._tick(self.db)
            self.assertEqual(order, ["discover", "sync", "sync", "discover", "sync"])

    def test_manual_discover_works_when_auto_discover_is_disabled(self):
        from unittest import mock

        self.db.set_setting("auto_discover", "0")
        self.db.set_setting("discover_requested", "1")
        self.db.set_setting("last_discover_at", engine.utcnow())
        worker = engine.Worker(self.tmp.name, "/nonexistent.pem")
        with mock.patch.object(engine, "client_from_settings", return_value=object()), \
             mock.patch.object(engine, "discover") as discover, \
             mock.patch.object(engine, "run_cycle") as run_cycle:
            worker._tick(self.db)
            discover.assert_called_once()
            run_cycle.assert_called_once()
            self.assertEqual(self.db.get_setting("discover_requested"), "0")
            worker._tick(self.db)
            discover.assert_called_once()
            self.assertEqual(run_cycle.call_count, 2)

    def test_auth_error_sets_flag_and_is_throttled(self):
        from app.github import AuthError
        from unittest import mock

        class Boom:
            def installation_repos(self):
                raise AuthError("bad token")

        worker = engine.Worker(self.tmp.name, "/nonexistent.pem")
        with mock.patch.object(engine, "client_from_settings", return_value=Boom()):
            worker._tick(self.db)
            self.assertEqual(self.db.get_setting("auth_error"), "bad token")
            self.assertEqual(self.db.get_setting("throttle:auth") is not None, True)
            # second tick within the throttle window records no new event
            before = self.db.one("SELECT COUNT(*) AS n FROM events")["n"]
            worker._tick(self.db)
            self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM events")["n"], before)

    def test_network_error_is_recorded(self):
        from unittest import mock

        class Boom:
            def installation_repos(self):
                raise RuntimeError("network down")

        worker = engine.Worker(self.tmp.name, "/nonexistent.pem")
        with mock.patch.object(engine, "client_from_settings", return_value=Boom()):
            worker._tick(self.db)
        row = self.db.one("SELECT * FROM events WHERE kind='error'")
        self.assertIn("network down", row["message"])


if __name__ == "__main__":
    unittest.main()
