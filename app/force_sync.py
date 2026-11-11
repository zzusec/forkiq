"""Explicit upstream alignment with a verified backup and exact HEAD lease."""
import tempfile
from pathlib import Path

from .backups import (_NAME, _SHA, _run_git, _fetch_repository,
                      _authenticated_git_env, ensure_backup, verified_backup)
from .db import utcnow, log_event


def align_upstream(db, client, row, fork_sha, target_sha):
    stage = 'prepare'
    try:
        if not all(isinstance(s, str) and _SHA.fullmatch(s) for s in (fork_sha, target_sha)):
            raise ValueError('invalid commit')
        for name in (row['full_name'], row['upstream_full_name']):
            if not _NAME.fullmatch(name) or name.split('/')[-1] in ('.', '..'):
                raise ValueError('invalid repository')
        ref = 'refs/heads/' + row['branch']
        with tempfile.TemporaryDirectory(prefix='forkiq-align-') as work:
            work = Path(work)
            _run_git(['check-ref-format', ref], work)
            _run_git(['init', '--bare'], work)
            _fetch_repository(client, row['upstream_full_name'], target_sha, work)
            assert _run_git(['rev-parse', '--verify', target_sha + '^{commit}'], work, capture=True) == target_sha
            backup = ensure_backup(db, client, row, fork_sha)
            if (backup.get('full_name') != row['full_name'] or backup.get('fork_sha') != fork_sha
                    or not verified_backup(db, backup)):
                raise ValueError('backup unavailable')
            live = db.one('SELECT * FROM repositories WHERE id=?', (row['id'],))
            if (not live or live['mode'] != 'force' or not live['enabled'] or live['status'] == 'paused'
                    or live['restore_backup_id'] is not None
                    or any(live[k] != row[k] for k in ('branch', 'full_name', 'upstream_full_name'))):
                return 'paused'
            if (client.get_ref(row['full_name'], row['branch']) != fork_sha
                    or client.get_ref(row['upstream_full_name'], row['branch']) != target_sha):
                raise ValueError('HEAD changed')
            stage = 'push'
            _run_git(['push', '--porcelain', '--no-verify', '--no-follow-tags', '--recurse-submodules=no',
                      f'--force-with-lease={ref}:{fork_sha}', '--',
                      'https://github.com/' + row['full_name'] + '.git', target_sha + ':' + ref],
                     work, env=_authenticated_git_env(client))
            if client.get_ref(row['full_name'], row['branch']) != target_sha:
                raise ValueError('unconfirmed push')
        now = utcnow()
        db.run("UPDATE repositories SET status='synced',fork_sha=?,upstream_sha=?,fork_tag=NULL,"
               "fork_commit_at=NULL,fork_ahead=0,upstream_ahead=0,last_error=NULL,last_check_at=?,"
               "last_success_at=?,updated_at=?,deletion_review=NULL,deletion_approval=NULL,"
               "diverged_at=NULL,diverged_upstream_sha=NULL WHERE id=? AND mode='force' AND enabled=1",
               (target_sha, target_sha, now, now, now, row['id']))
        log_event(db, row['id'], 'synced', f"{row['full_name']} 已备份 {fork_sha[:12]}，强制对齐上游 {target_sha[:12]}")
        return 'synced'
    except Exception:
        message = ('强制同步结果未确认：分支可能已变化、推送被拒或网络超时，请核对后恢复。'
                   if stage == 'push' else '强制同步准备失败：请检查备份、上游提交、分支状态和访问权限后恢复。')
        db.run("UPDATE repositories SET enabled=0,status='paused',sync_requested=0,last_error=?,updated_at=? "
               "WHERE id=? AND mode='force'", (message, utcnow(), row['id']))
        log_event(db, row['id'], 'blocked', f"{row['full_name']} {message}")
        return 'paused'
