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
                pr_number INTEGER,
                pr_title TEXT,
                pr_state TEXT,
                pr_url TEXT,
                pr_base TEXT,
                changes_added INTEGER,
                changes_deleted INTEGER,
                changes_target TEXT,
                updated_at INTEGER
            )
            """
        )
        self._ensure_columns(
            {
                "pr_number": "INTEGER",
                "pr_title": "TEXT",
                "pr_state": "TEXT",
                "pr_url": "TEXT",
                "pr_base": "TEXT",
                "changes_added": "INTEGER",
                "changes_deleted": "INTEGER",
                "changes_target": "TEXT",
            }
        )
        self._conn.commit()

    def load_worktrees(self) -> list[WorktreeStatus]:
        cur = self._conn.execute(
            """
            SELECT
                path,
                branch,
                last_commit_ts,
                upstream,
                ahead,
                behind,
                pr_number,
                pr_title,
                pr_state,
                pr_url,
                pr_base,
                changes_added,
                changes_deleted,
                changes_target
            FROM worktrees
            """
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
                pr_number=row[6],
                pr_title=row[7],
                pr_state=row[8],
                pr_url=row[9],
                pr_base=row[10],
                changes_added=row[11],
                changes_deleted=row[12],
                changes_target=row[13],
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
                pr_number,
                pr_title,
                pr_state,
                pr_url,
                pr_base,
                changes_added,
                changes_deleted,
                changes_target,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                branch=excluded.branch,
                last_commit_ts=excluded.last_commit_ts,
                upstream=excluded.upstream,
                ahead=excluded.ahead,
                behind=excluded.behind,
                pr_number=excluded.pr_number,
                pr_title=excluded.pr_title,
                pr_state=excluded.pr_state,
                pr_url=excluded.pr_url,
                pr_base=excluded.pr_base,
                changes_added=excluded.changes_added,
                changes_deleted=excluded.changes_deleted,
                changes_target=excluded.changes_target,
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
                    s.pr_number,
                    s.pr_title,
                    s.pr_state,
                    s.pr_url,
                    s.pr_base,
                    s.changes_added,
                    s.changes_deleted,
                    s.changes_target,
                    now,
                )
                for s in statuses
            ],
        )
        self._conn.commit()

    def _ensure_columns(self, columns: dict[str, str]) -> None:
        cur = self._conn.execute("PRAGMA table_info(worktrees)")
        existing = {row[1] for row in cur.fetchall()}
        for name, col_type in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE worktrees ADD COLUMN {name} {col_type}")


def _repo_id(repo_root: Path) -> str:
    return hashlib.sha1(str(repo_root).encode("utf-8")).hexdigest()
