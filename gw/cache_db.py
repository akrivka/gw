"""SQLite caching for worktree metadata."""

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self


@dataclass
class CachedWorktree:
    """Cached worktree data from the database."""

    pr_number: int | None
    pr_state: str | None
    pr_base: str | None
    pr_url: str | None
    checks_passed: int | None
    checks_total: int | None
    checks_state: str | None
    additions: int
    deletions: int
    dirty: bool


_DB_LOCK = threading.Lock()
_SCHEMA_ENSURED: set[str] = set()


def _get_cache_dir() -> Path:
    """Get or create the cache directory."""
    cache_dir = Path.home() / ".cache" / "gw"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_db_path(repo_root: Path) -> Path:
    """Get the database path for a repository."""
    repo_id = hashlib.sha1(str(repo_root).encode("utf-8")).hexdigest()
    return _get_cache_dir() / f"{repo_id}.sqlite"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure the database schema exists."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worktree_cache (
          branch TEXT PRIMARY KEY,
          path TEXT NOT NULL,
          pr_number INTEGER,
          pr_state TEXT,
          pr_base TEXT,
          pr_url TEXT,
          pr_updated_at INTEGER,
          checks_passed INTEGER,
          checks_total INTEGER,
          checks_state TEXT,
          checks_updated_at INTEGER,
          additions INTEGER,
          deletions INTEGER,
          dirty INTEGER,
          changes_updated_at INTEGER,
          pull INTEGER,
          push INTEGER,
          pullpush_validated_at INTEGER
        )
        """
    )
    conn.commit()


class CacheDB:
    """Context manager for SQLite cache database access."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.db_path = _get_db_path(repo_root)
        self._conn: sqlite3.Connection | None = None
        self._lock_acquired = False

    def __enter__(self) -> Self:
        _DB_LOCK.acquire()
        self._lock_acquired = True
        try:
            self._conn = sqlite3.connect(str(self.db_path), timeout=5)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")

            db_key = str(self.db_path)
            if db_key not in _SCHEMA_ENSURED:
                _ensure_schema(self._conn)
                _SCHEMA_ENSURED.add(db_key)

            return self
        except Exception:
            if self._conn:
                self._conn.close()
                self._conn = None
            if self._lock_acquired:
                _DB_LOCK.release()
                self._lock_acquired = False
            raise

    def __exit__(self, *args: object) -> None:
        try:
            if self._conn:
                self._conn.commit()
                self._conn.close()
                self._conn = None
        finally:
            if self._lock_acquired:
                _DB_LOCK.release()
                self._lock_acquired = False

    @property
    def conn(self) -> sqlite3.Connection:
        """Get the connection (must be inside context)."""
        if self._conn is None:
            raise RuntimeError("CacheDB must be used as a context manager")
        return self._conn

    def get_cached_worktree(self, cache_key: str) -> CachedWorktree | None:
        """Get cached data for a worktree by cache key."""
        row = self.conn.execute(
            """
            SELECT
              pr_number, pr_state, pr_base, pr_url,
              checks_passed, checks_total, checks_state,
              additions, deletions, dirty
            FROM worktree_cache
            WHERE branch = ?
            """,
            (cache_key,),
        ).fetchone()

        if not row:
            return None

        return CachedWorktree(
            pr_number=row["pr_number"],
            pr_state=row["pr_state"],
            pr_base=row["pr_base"],
            pr_url=row["pr_url"],
            checks_passed=row["checks_passed"],
            checks_total=row["checks_total"],
            checks_state=row["checks_state"],
            additions=row["additions"] if row["additions"] is not None else 0,
            deletions=row["deletions"] if row["deletions"] is not None else 0,
            dirty=bool(row["dirty"]) if row["dirty"] is not None else False,
        )

    def upsert_path(self, cache_key: str, path: Path) -> None:
        """Upsert just the path for a worktree."""
        self.conn.execute(
            """
            INSERT INTO worktree_cache (branch, path)
            VALUES (?, ?)
            ON CONFLICT(branch) DO UPDATE SET path = excluded.path
            """,
            (cache_key, str(path)),
        )

    def upsert_pull_push(self, cache_key: str, path: Path, pull: int, push: int) -> None:
        """Upsert pull/push counts for a worktree."""
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO worktree_cache (branch, path, pull, push, pullpush_validated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(branch) DO UPDATE SET
              path = excluded.path,
              pull = excluded.pull,
              push = excluded.push,
              pullpush_validated_at = excluded.pullpush_validated_at
            """,
            (cache_key, str(path), pull, push, now),
        )

    def upsert_changes(
        self, cache_key: str, path: Path, additions: int, deletions: int, dirty: bool
    ) -> None:
        """Upsert change statistics for a worktree."""
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO worktree_cache (branch, path, additions, deletions, dirty, changes_updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(branch) DO UPDATE SET
              path = excluded.path,
              additions = excluded.additions,
              deletions = excluded.deletions,
              dirty = excluded.dirty,
              changes_updated_at = excluded.changes_updated_at
            """,
            (cache_key, str(path), additions, deletions, int(dirty), now),
        )

    def upsert_pr_and_checks(
        self,
        cache_key: str,
        path: Path,
        pr_number: int | None,
        pr_state: str | None,
        pr_base: str | None,
        pr_url: str | None,
        checks_passed: int | None,
        checks_total: int | None,
        checks_state: str | None,
    ) -> None:
        """Upsert both PR and check information for a worktree."""
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO worktree_cache (
              branch, path, pr_number, pr_state, pr_base, pr_url,
              pr_updated_at, checks_passed, checks_total, checks_state, checks_updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(branch) DO UPDATE SET
              path = excluded.path,
              pr_number = excluded.pr_number,
              pr_state = excluded.pr_state,
              pr_base = excluded.pr_base,
              pr_url = excluded.pr_url,
              pr_updated_at = excluded.pr_updated_at,
              checks_passed = excluded.checks_passed,
              checks_total = excluded.checks_total,
              checks_state = excluded.checks_state,
              checks_updated_at = excluded.checks_updated_at
            """,
            (
                cache_key,
                str(path),
                pr_number,
                pr_state,
                pr_base,
                pr_url,
                now,
                checks_passed,
                checks_total,
                checks_state,
                now,
            ),
        )
