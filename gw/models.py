from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None
    head: str | None


@dataclass(frozen=True)
class WorktreeStatus:
    path: Path
    branch: str | None
    last_commit_ts: int
    upstream: str | None
    ahead: int | None
    behind: int | None
