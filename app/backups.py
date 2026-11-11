"""Verified, self-contained Git snapshots stored outside managed repositories."""
import base64
import errno
import fcntl
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .db import utcnow

KEEP = 3
RETRY_SECONDS = 600
COMMAND_TIMEOUT = 600
MIN_FREE_BYTES = 256 * 1024 * 1024
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
_LOCK = threading.RLock()


class BackupError(RuntimeError):
    """A safe error message, deliberately excluding Git output and credentials."""


class BackupTimeout(BackupError):
    """A Git operation timed out; a remote write may need reconciliation."""


def _root(db, create=False):
    root = Path(db.path).absolute().parent / "backups"
    if create:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise BackupError("备份目录异常，请检查服务器存储路径。")
    if create:
        root.chmod(0o700)
    return root


def backup_path(db, record):
    """Never use repository names or caller-controlled paths as a filename."""
    value = record["id"]
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise BackupError("备份编号无效。")
    if not _SHA.fullmatch(str(record["fork_sha"])):
        raise BackupError("备份提交无效。")
    return _root(db) / f"{int(value)}.bundle"


def _digest(path):
    flags = (os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise BackupError("备份文件异常，无法校验。")
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        return digest.hexdigest(), size


def verified_backup(db, record):
    """A ready database row is insufficient without its intact on-disk bundle."""
    try:
        if record["status"] != "ready":
            return False
        path = backup_path(db, record)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != record["size_bytes"]:
            return False
        digest, size = _digest(path)
        return size == record["size_bytes"] and digest == record["sha256"]
    except (OSError, ValueError, TypeError, KeyError, IndexError, BackupError):
        return False


def delete_backup(db, backup_id, repo_id=None):
    """Remove one verified backup file and row while the backup lock is held.

    The bundle is moved to a private temporary name before the database row is
    committed. If the row cannot be removed, it is put back so a failed delete
    never leaves a database entry pointing at a missing snapshot.
    """
    try:
        root = _root(db)
    except OSError:
        raise BackupError("备份目录缺失或无法访问，未删除记录") from None
    with _locked(root):
        record = db.one("SELECT * FROM repository_backups WHERE id=?", (backup_id,))
        if record is None or (repo_id is not None and record["repo_id"] != repo_id):
            raise BackupError("备份不存在")
        path = backup_path(db, record)
        if path.is_symlink() or not path.is_file():
            raise BackupError("备份文件缺失或异常，未删除记录")
        temporary = root / f".deleting-{int(backup_id)}.bundle"
        if temporary.exists() or temporary.is_symlink():
            raise BackupError("另一个备份删除正在处理，请稍后重试")
        os.replace(path, temporary)
        try:
            if repo_id is None:
                deleted = db.run("DELETE FROM repository_backups WHERE id=?", (backup_id,)).rowcount
            else:
                deleted = db.run(
                    "DELETE FROM repository_backups WHERE id=? AND repo_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM repositories WHERE id=? AND restore_backup_id=?)",
                    (backup_id, repo_id, repo_id, backup_id)).rowcount
            if not deleted:
                raise BackupError("备份正在回滚或已被删除，请刷新后重试")
        except Exception:
            os.replace(temporary, path)
            raise
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # The database row is already gone; leave a private orphan for the
            # next maintenance pass rather than reporting a false failure.
            pass
        return dict(record)


@contextmanager
def _locked(root):
    # Serialize retention and publication across both request threads and workers.
    with _LOCK:
        flags = os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(root / ".lock", flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise BackupError("备份锁文件异常，请检查存储权限。")
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BackupError("另一个备份正在进行，请稍后重试。") from None
                    time.sleep(0.1)
            yield
        finally:
            os.close(fd)


def _git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https",
                "GIT_PROTOCOL_FROM_USER": "0"})
    return env


def _run_git(args, cwd, *, env=None, capture=False):
    # Small pack/cache budgets matter on the memory-limited production service.
    settings = ("credential.helper=", "core.hooksPath=/dev/null", "init.templateDir=",
                "protocol.allow=never", "protocol.https.allow=always",
                "http.followRedirects=false", "gc.auto=0", "maintenance.auto=false",
                "fetch.fsckObjects=true", "transfer.fsckObjects=true", "pack.threads=1",
                "pack.windowMemory=16m", "pack.deltaCacheSize=8m", "pack.window=5",
                "core.packedGitWindowSize=8m", "core.packedGitLimit=32m",
                "core.deltaBaseCacheLimit=8m")
    command = ["git"]
    for setting in settings:
        command.extend(("-c", setting))
    command.extend(args)
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(command, cwd=cwd, env=env or _git_env(), stdin=subprocess.DEVNULL,
                                stdout=output if capture else subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            result = proc.wait(timeout=COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            # A fetch may have spawned git-remote-https: stop the entire group.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise BackupTimeout("Git 操作超时，请检查执行结果后重试。") from None
        if result:
            raise BackupError(f"Git 操作失败（{args[0]}），请检查仓库权限和磁盘空间。")
        if capture:
            output.seek(0)
            value = output.read(1025)
            if len(value) > 1024:
                raise BackupError("Git 备份校验结果异常。")
            return value.decode("ascii").strip()
    return ""


def _authenticated_git_env(client):
    """Supply credentials only to a child process, never an argument or file."""
    try:
        token = client.installation_token()
        if not isinstance(token, str) or not token or len(token) > 16384:
            raise ValueError("invalid token")
    except Exception:
        raise BackupError("GitHub 授权失败，请检查 App 安装权限。") from None
    env = _git_env()
    authorization = base64.b64encode(("x-access-token:" + token).encode()).decode("ascii")
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "Authorization: Basic " + authorization})
    return env


def _fetch_repository(client, full_name, sha, target_gitdir):
    """Fetch one immutable commit with its full reachable history, never shallow."""
    _run_git(["fetch", "--no-tags", "--no-recurse-submodules",
              "https://github.com/" + full_name + ".git", sha], target_gitdir,
             env=_authenticated_git_env(client))


def _check_space(root):
    if shutil.disk_usage(root).free < MIN_FREE_BYTES:
        raise BackupError("磁盘可用空间不足 256 MiB，已停止同步。")


def _create_bundle(client, full_name, sha, root, target):
    _check_space(root)
    with tempfile.TemporaryDirectory(prefix=".tmp-", dir=root) as temporary:
        work = Path(temporary)
        source = work / "source.git"
        source.mkdir(mode=0o700)
        _run_git(["init", "--bare"], source)
        _fetch_repository(client, full_name, sha, source)
        _check_space(root)
        actual = _run_git(["rev-parse", "--verify", sha + "^{commit}"], source, capture=True)
        if actual != sha:
            raise BackupError("备份拉取的提交与本地版本不一致。")
        _run_git(["update-ref", "refs/heads/preserved", sha], source)
        bundle = work / "snapshot.bundle"
        _run_git(["bundle", "create", str(bundle), "refs/heads/preserved"], source)
        bundle.chmod(0o600)
        # An empty verifier cannot satisfy prerequisites: incremental bundles fail.
        verifier = work / "verify.git"
        verifier.mkdir(mode=0o700)
        _run_git(["init", "--bare"], verifier)
        _run_git(["bundle", "verify", str(bundle)], verifier)
        heads = _run_git(["bundle", "list-heads", str(bundle)], verifier, capture=True)
        if heads != sha + " refs/heads/preserved":
            raise BackupError("备份未包含预期提交，已停止同步。")
        digest, size = _digest(bundle)
        _check_space(root)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise BackupError("备份目标文件异常，请检查存储路径。")
        with bundle.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(bundle, target)
        directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return digest, size


def _recent_failure(record):
    if record["status"] != "error":
        return False
    try:
        updated = datetime.fromisoformat(record["updated_at"])
        return 0 <= (datetime.now(timezone.utc) - updated).total_seconds() < RETRY_SECONDS
    except (ValueError, TypeError):
        return False


def _prune(db, full_name, newest_id):
    rows = db.all("SELECT * FROM repository_backups WHERE full_name=? AND status='ready' "
                  "ORDER BY (id=?) DESC, created_at DESC, id DESC", (full_name, newest_id))
    for record in rows[KEEP:]:
        path = backup_path(db, record)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise BackupError("旧备份无法安全清理，请检查存储权限。")
        path.unlink(missing_ok=True)
        db.run("DELETE FROM repository_backups WHERE id=?", (record["id"],))


def ensure_backup(db, client, row, fork_sha):
    """Return ready only after a complete verified server-side snapshot exists."""
    record = None
    try:
        full_name = row["full_name"]
        if (not isinstance(full_name, str) or not _NAME.fullmatch(full_name)
                or full_name.split("/", 1)[1] in (".", "..")):
            raise BackupError("备份仓库名称无效。")
        if not isinstance(fork_sha, str) or not _SHA.fullmatch(fork_sha):
            raise BackupError("缺少完整的 40 位本地提交，无法备份。")
        fork_sha = fork_sha.lower()
        root = _root(db, create=True)
        with _locked(root):
            found = db.one("SELECT * FROM repository_backups WHERE full_name=? AND fork_sha=?",
                           (full_name, fork_sha))
            if found:
                record = dict(found)
                if verified_backup(db, record) or _recent_failure(record):
                    return record
            now = utcnow()
            if record is None:
                cursor = db.run("INSERT INTO repository_backups "
                                "(repo_id,full_name,fork_sha,status,created_at,updated_at) "
                                "VALUES(?,?,?,'building',?,?)",
                                (row["id"], full_name, fork_sha, now, now))
                record = dict(db.one("SELECT * FROM repository_backups WHERE id=?", (cursor.lastrowid,)))
            else:
                db.run("UPDATE repository_backups SET status='building',last_error=NULL,updated_at=? "
                       "WHERE id=?", (now, record["id"]))
            path = backup_path(db, record)
            digest, size = _create_bundle(client, full_name, fork_sha, root, path)
            now = utcnow()
            db.run("UPDATE repository_backups SET status='ready',sha256=?,size_bytes=?,last_error=NULL,"
                   "created_at=?,updated_at=? WHERE id=?", (digest, size, now, now, record["id"]))
            record = dict(db.one("SELECT * FROM repository_backups WHERE id=?", (record["id"],)))
            if not verified_backup(db, record):
                raise BackupError("备份完整性校验失败，已停止同步。")
            _prune(db, full_name, record["id"])
            retained = db.one("SELECT * FROM repository_backups WHERE id=?", (record["id"],))
            if retained is None or not verified_backup(db, retained):
                raise BackupError("备份完整性校验失败，已停止同步。")
            return dict(retained)
    except Exception as error:
        if isinstance(error, BackupError):
            message = str(error)
        elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
            message = "磁盘空间不足，备份失败，已停止同步。"
        else:
            # Neither exception text nor Git stderr is safe to persist or display.
            message = "服务器备份失败，请检查 Git、存储权限和磁盘空间。"
        if record is not None:
            try:
                db.run("UPDATE repository_backups SET status='error',last_error=?,updated_at=? WHERE id=?",
                       (message, utcnow(), record["id"]))
                return dict(db.one("SELECT * FROM repository_backups WHERE id=?", (record["id"],)))
            except Exception:
                pass
        return {"id": None, "status": "error", "last_error": message}
