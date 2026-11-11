"""SQLite storage for ForkIQ."""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repositories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT,
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  full_name TEXT NOT NULL UNIQUE,
  upstream_full_name TEXT,
  branch TEXT NOT NULL DEFAULT 'main',
  mode TEXT NOT NULL DEFAULT 'mirror',
  enabled INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending',
  upstream_sha TEXT,
  fork_sha TEXT,
  upstream_tag TEXT,
  fork_tag TEXT,
  upstream_commit_at TEXT,
  fork_commit_at TEXT,
  upstream_gone INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,
  last_check_at TEXT,
  last_error TEXT,
  diverged_at TEXT,
  diverged_upstream_sha TEXT,
  last_merged_upstream_sha TEXT,
  upstream_ahead INTEGER,
  fork_ahead INTEGER,
  self_update TEXT,
  self_update_interval INTEGER,
  actions_disabled INTEGER,
  discovered INTEGER NOT NULL DEFAULT 0,
  is_archived INTEGER NOT NULL DEFAULT 0,
  sync_requested INTEGER NOT NULL DEFAULT 0,
  deletion_protection INTEGER,
  deletion_max_files INTEGER,
  deletion_max_percent REAL,
  deletion_review TEXT,
  deletion_approval TEXT,
  restore_backup_id INTEGER,
  restore_expected_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repository_backups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER,
  full_name TEXT NOT NULL,
  fork_sha TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sha256 TEXT,
  size_bytes INTEGER,
  last_error TEXT,
  UNIQUE(full_name,fork_sha)
);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path):
        self.path = str(path)
        # One connection per thread. Opening a connection and re-issuing
        # `PRAGMA journal_mode=WAL` for every query meant every read waited on
        # the write lock that pragma needs; on the shared production host that
        # turned a 3 ms page into seconds under load.
        self._local = threading.local()
        self.init()

    def connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def init(self):
        # The connection is cached for this thread, so it must stay open here.
        conn = self.connect()
        # Journal mode is a database property, not a per-connection one.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Additive migrations for databases created before these columns.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(repositories)")}
        if "mode" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN mode TEXT NOT NULL DEFAULT 'mirror'")
        if "last_merged_upstream_sha" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN last_merged_upstream_sha TEXT")
        if "self_update" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN self_update TEXT")
        if "self_update_interval" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN self_update_interval INTEGER")
        if "upstream_tag" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN upstream_tag TEXT")
        if "fork_tag" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN fork_tag TEXT")
        if "upstream_commit_at" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN upstream_commit_at TEXT")
        if "fork_commit_at" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN fork_commit_at TEXT")
        if "upstream_gone" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN upstream_gone INTEGER NOT NULL DEFAULT 0")
        if "upstream_ahead" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN upstream_ahead INTEGER")
        if "fork_ahead" not in columns:
            conn.execute("ALTER TABLE repositories ADD COLUMN fork_ahead INTEGER")
        for name, kind in (("deletion_protection", "INTEGER"),
                           ("deletion_max_files", "INTEGER"),
                           ("deletion_max_percent", "REAL"),
                           ("deletion_review", "TEXT"), ("deletion_approval", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE repositories ADD COLUMN {name} {kind}")
        for name, kind in (("restore_backup_id", "INTEGER"), ("restore_expected_sha", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE repositories ADD COLUMN {name} {kind}")
        conn.commit()

    def run(self, sql, params=()):
        conn = self.connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur

    def one(self, sql, params=()):
        return self.connect().execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.connect().execute(sql, params).fetchall()

    def get_setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.run("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def setup_admin(self, password_hash):
        """Atomically claim first-run setup, so two public clients cannot race."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM settings WHERE key='setup_done' AND value='1'").fetchone():
                conn.rollback()
                return False
            conn.execute("INSERT INTO settings(key,value) VALUES('admin_pass_hash',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (password_hash,))
            conn.execute("INSERT INTO settings(key,value) VALUES('setup_done','1') "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise



def log_event(db, repo_id, kind, message):
    db.run("INSERT INTO events(repo_id, at, kind, message) VALUES(?,?,?,?)",
           (repo_id, utcnow(), kind, message))


def insert_repository(db, *, owner, name, full_name, upstream_full_name, branch="main",
                      node_id=None, status="pending", discovered=0, is_archived=0):
    now = utcnow()
    cur = db.run(
        "INSERT INTO repositories(owner,name,full_name,upstream_full_name,branch,node_id,"
        "status,discovered,is_archived,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (owner, name, full_name, upstream_full_name, branch, node_id, status,
         discovered, is_archived, now, now))
    return cur.lastrowid
