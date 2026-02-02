from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gw.cache import Cache
from gw.git import get_repo_root, list_worktrees
from gw.models import WorktreeStatus


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def _init_repo(root: Path) -> tuple[Path, Path]:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "--bare", ".git"], cwd=root)
    _run(["git", "--git-dir=.git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=root)
    _run(["git", "--git-dir=.git", "worktree", "add", "main"], cwd=root)
    main_path = root / "main"
    _run(["git", "-C", str(main_path), "config", "user.email", "test@example.com"])
    _run(["git", "-C", str(main_path), "config", "user.name", "Test"])
    (main_path / "README.md").write_text("hello")
    _run(["git", "-C", str(main_path), "add", "."])
    _run(["git", "-C", str(main_path), "commit", "-m", "init"])
    return root, main_path


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git missing"
)
def test_repo_root_and_worktrees(tmp_path: Path) -> None:
    root, main_path = _init_repo(tmp_path / "repo")
    _run(["git", "--git-dir=.git", "worktree", "add", "-b", "feature", "feature"], cwd=root)
    repo_root = get_repo_root(main_path)
    assert repo_root == root
    worktrees = list_worktrees(root)
    branches = sorted([wt.branch for wt in worktrees if wt.branch])
    assert branches == ["feature", "main"]


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "repo", tmp_path / "cache")
    statuses = [
        WorktreeStatus(
            path=tmp_path / "repo" / "main",
            branch="main",
            last_commit_ts=123,
            upstream="origin/main",
            ahead=1,
            behind=2,
        )
    ]
    cache.upsert_worktrees(statuses)
    loaded = cache.load_worktrees()
    assert loaded[0].branch == "main"
    assert loaded[0].ahead == 1
