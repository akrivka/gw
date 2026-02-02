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
    pr_number: int | None = None
    pr_title: str | None = None
    pr_state: str | None = None
    pr_url: str | None = None
    pr_base: str | None = None
    changes_added: int | None = None
    changes_deleted: int | None = None
    changes_target: str | None = None


@dataclass
class DoctorItem:
    label: str
    actions: list[str]
    kind: str
    selected: int = 0
    path: Path | None = None
    branch: str | None = None
