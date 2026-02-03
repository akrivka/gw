from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gw.app import App
from gw.cache import Cache
from gw.git import get_repo_root, list_worktrees
from gw.models import WorktreeStatus

GIT_AVAILABLE = subprocess.run(["git", "--version"], capture_output=True).returncode == 0

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


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git missing")
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


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git missing")
def test_app_worktree_helpers(tmp_path: Path) -> None:
    root, main_path = _init_repo(tmp_path / "repo")
    app = App(main_path)
    path = app.create_worktree("feature")
    assert path.exists()
    new_path = app.rename_worktree("feature", "feature-renamed")
    assert new_path.exists()
    assert not path.exists()
    app.delete_worktrees([new_path])
    assert not new_path.exists()
