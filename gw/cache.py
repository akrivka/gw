from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from .models import WorktreeStatus


class Cache:
    def __init__(self, repo_root: Path, cache_root: Path) -> None:
        self.repo_root = repo_root
        self.cache_root = cache_root
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_root / f"{_repo_id(repo_root)}.sqlite"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worktrees (
                path TEXT PRIMARY KEY,
                branch TEXT,
                last_commit_ts INTEGER,
                upstream TEXT,
                ahead INTEGER,
                behind INTEGER,
                updated_at INTEGER
            )
            """
        )
        self._conn.commit()

    def load_worktrees(self) -> list[WorktreeStatus]:
        cur = self._conn.execute(
            "SELECT path, branch, last_commit_ts, upstream, ahead, behind FROM worktrees"
        )
        rows = cur.fetchall()
        return [
            WorktreeStatus(
                path=Path(row[0]),
                branch=row[1],
                last_commit_ts=row[2] or 0,
                upstream=row[3],
                ahead=row[4],
                behind=row[5],
            )
            for row in rows
        ]

    def upsert_worktrees(self, statuses: Iterable[WorktreeStatus]) -> None:
        now = int(time.time())
        self._conn.executemany(
            """
            INSERT INTO worktrees (
                path,
                branch,
                last_commit_ts,
                upstream,
                ahead,
                behind,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                branch=excluded.branch,
                last_commit_ts=excluded.last_commit_ts,
                upstream=excluded.upstream,
                ahead=excluded.ahead,
                behind=excluded.behind,
                updated_at=excluded.updated_at
            """,
            [
                (
                    str(s.path),
                    s.branch,
                    s.last_commit_ts,
                    s.upstream,
                    s.ahead,
                    s.behind,
                    now,
                )
                for s in statuses
            ],
        )
        self._conn.commit()


def _repo_id(repo_root: Path) -> str:
    return hashlib.sha1(str(repo_root).encode("utf-8")).hexdigest()
