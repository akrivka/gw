"""Data models for gw."""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorktreeInfo:
    """Represents a git worktree with all associated metadata."""

    path: Path
    branch: str
    head: str
    ref_name: str | None
    cache_key: str
    last_commit_ts: int
    pull: int
    push: int
    pull_push_validated: bool
    has_upstream: bool
    behind: int
    ahead: int
    additions: int
    deletions: int
    dirty: bool
    pr_number: int | None
    pr_state: str | None
    pr_base: str | None
    pr_url: str | None
    pr_validated: bool
    checks_passed: int | None
    checks_total: int | None
    checks_state: str | None
    checks_validated: bool
    changes_validated: bool

    @property
    def is_detached(self) -> bool:
        """Check if this worktree is in detached HEAD state."""
        return self.ref_name is None


@dataclass
class Cell:
    """A single cell in the TUI table."""

    text: str
    cached: bool = False


@dataclass(frozen=True)
class AheadBehind:
    """Commit counts ahead/behind a reference."""

    ahead: int
    behind: int


@dataclass(frozen=True)
class DiffStat:
    """Statistics from a diff operation."""

    additions: int
    deletions: int
    dirty: bool


@dataclass(frozen=True)
class ParsedWorktree:
    """Raw worktree data from git worktree list."""

    path: Path
    branch: str
    head: str
