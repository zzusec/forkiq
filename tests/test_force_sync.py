"""Real Git validates leased alignment and preservation of overwritten history."""
import unittest
from unittest import mock
import test_restore
from app import force_sync


class ForceSyncTests(unittest.TestCase):
    git = test_restore.RestoreTests.git
    row = test_restore.RestoreTests.row
    local_runner = test_restore.RestoreTests.local_runner

    def setUp(self):
        test_restore.RestoreTests.setUp(self)
        self.db.run("UPDATE repositories SET mode='force' WHERE id=?", (self.repo_id,))
        self.target_head = self.target
        original_get_ref = self.client.get_ref
        self.client.get_ref = lambda name, branch: self.target_head if name == 'up/demo' else original_get_ref(name, branch)
        runner = mock.patch.object(force_sync, '_run_git', side_effect=self.local_runner)
        runner.start(); self.addCleanup(runner.stop)
        fetch = mock.patch.object(force_sync, '_fetch_repository', side_effect=lambda client, name, sha, folder:
            self.local_runner(['fetch', '--no-tags', 'https://github.com/me/demo.git', sha], folder))
        fetch.start(); self.addCleanup(fetch.stop)

    def align(self):
        return force_sync.align_upstream(self.db, self.client, self.row(), self.current, self.target_head)

    def test_rewinds_to_upstream_with_backup_and_exact_lease(self):
        self.assertEqual(self.align(), 'synced')
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.target)
        backup = self.db.one('SELECT * FROM repository_backups WHERE fork_sha=?', (self.current,))
        self.assertTrue(force_sync.verified_backup(self.db, backup))
        push = next(c for c in self.commands if c[0] == 'push')
        self.assertIn('--force-with-lease=refs/heads/main:' + self.current, push)
        self.assertEqual(self.row()['mode'], 'force')

    def test_backup_failure_never_pushes(self):
        with mock.patch.object(force_sync, 'ensure_backup', return_value={'status': 'error'}):
            self.assertEqual(self.align(), 'paused')
        self.assertFalse(any(c[0] == 'push' for c in self.commands))
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.current)

    def test_mode_change_during_backup_cancels_alignment(self):
        original = force_sync.ensure_backup
        def backup(*args):
            result = original(*args)
            self.db.run("UPDATE repositories SET mode='protected',enabled=0,status='paused' WHERE id=?", (self.repo_id,))
            return result
        with mock.patch.object(force_sync, 'ensure_backup', side_effect=backup):
            self.assertEqual(self.align(), 'paused')
        self.assertFalse(any(c[0] == 'push' for c in self.commands))
        self.assertEqual(self.row()['mode'], 'protected')

    def test_lease_rejects_concurrent_remote_update(self):
        def concurrent_push():
            self.git('-C', str(self.work), 'commit', '--allow-empty', '-qm', 'concurrent user commit')
            self.git('-C', str(self.work), 'push', 'origin', 'main')
        self.push_hook = concurrent_push
        self.assertEqual(self.align(), 'paused')
        self.assertEqual(self.row()['enabled'], 0)
        self.assertIn('结果未确认', self.row()['last_error'])

    def test_timeout_after_actual_push_is_paused_not_retried(self):
        runner = self.local_runner
        def timeout(args, *a, **kw):
            result = runner(args, *a, **kw)
            if args[0] == 'push':
                raise TimeoutError()
            return result
        with mock.patch.object(force_sync, '_run_git', side_effect=timeout):
            self.assertEqual(self.align(), 'paused')
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.target)
        self.assertEqual(self.row()['enabled'], 0)

    def test_engine_routes_force_mode_through_deletion_guard_before_alignment(self):
        from app import engine
        from test_engine import node
        observation = node('N1', 'me/demo', 'up/demo', self.current, self.target)
        with mock.patch.object(engine, '_deletion_guard', return_value=False) as guard, \
             mock.patch.object(engine, 'align_upstream', return_value='synced') as align:
            self.assertEqual(engine.sync_one(self.db, self.client, self.row(), observation), 'needs_attention')
            guard.assert_called_once_with(self.db, self.client, self.row(), self.current, self.target)
            align.assert_not_called()
        with mock.patch.object(engine, '_deletion_guard', return_value=True) as guard, \
             mock.patch.object(engine, 'align_upstream', return_value='synced') as align:
            self.assertEqual(engine.sync_one(self.db, self.client, self.row(), observation), 'synced')
            guard.assert_called_once_with(self.db, self.client, self.row(), self.current, self.target)
            align.assert_called_once()
        self.db.run("UPDATE repositories SET mode='protected',enabled=0 WHERE id=?", (self.repo_id,))
        with mock.patch.object(engine, 'align_upstream') as align:
            self.assertEqual(engine.sync_one(self.db, self.client, self.row(), observation), 'paused')
            align.assert_not_called()

    def test_alignment_transport_can_align_to_empty_upstream_tree_after_review(self):
        remote = '--git-dir=' + str(self.remote)
        empty = self.git(remote, 'hash-object', '-w', '-t', 'tree', '/dev/null')
        self.target_head = self.git(remote, '-c', 'user.name=Upstream', '-c', 'user.email=up@example.invalid',
                                    'commit-tree', empty, '-p', self.current, '-m', 'empty upstream')
        self.git(remote, 'update-ref', 'refs/heads/upstream', self.target_head)
        self.assertEqual(self.align(), 'synced')
        self.assertEqual(self.git(remote, 'ls-tree', 'main'), '')
        self.assertTrue(self.db.one('SELECT id FROM repository_backups WHERE fork_sha=?', (self.current,)))
