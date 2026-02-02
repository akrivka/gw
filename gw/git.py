from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from .models import Worktree


class GitError(RuntimeError):
    pass


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def get_repo_root(cwd: Path) -> Path | None:
    try:
        common_dir = _run_git(["-C", str(cwd), "rev-parse", "--git-common-dir"])
    except GitError:
        return None
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = (cwd / common_path).resolve()
    return common_path.parent


def _short_branch(ref: str | None) -> str | None:
    if ref and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return ref


def list_worktrees(repo_root: Path) -> list[Worktree]:
    output = _run_git(["-C", str(repo_root), "worktree", "list", "--porcelain"])
    entries: list[Worktree] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                entries.append(
                    Worktree(
                        path=Path(current.get("worktree", "")),
                        branch=_short_branch(current.get("branch")),
                        head=current.get("HEAD"),
                    )
                )
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(
            Worktree(
                path=Path(current.get("worktree", "")),
                branch=_short_branch(current.get("branch")),
                head=current.get("HEAD"),
            )
        )
    filtered: list[Worktree] = []
    for wt in entries:
        if wt.branch is None and wt.path.resolve() == repo_root.resolve():
            continue
        filtered.append(wt)
    return filtered


def get_current_worktree(repo_root: Path, cwd: Path) -> Worktree | None:
    worktrees = list_worktrees(repo_root)
    cwd_resolved = cwd.resolve()
    for wt in worktrees:
        try:
            if cwd_resolved.is_relative_to(wt.path.resolve()):
                return wt
        except AttributeError:
            if str(cwd_resolved).startswith(str(wt.path.resolve()) + os.sep):
                return wt
    return None


def get_default_branch(repo_root: Path) -> str:
    try:
        ref = _run_git(["-C", str(repo_root), "symbolic-ref", "refs/remotes/origin/HEAD"])
        return ref.rsplit("/", 1)[-1]
    except GitError:
        for candidate in ["main", "master"]:
            try:
                _run_git(["-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{candidate}"])
                return candidate
            except GitError:
                continue
    return "main"


def sync_repo(repo_root: Path) -> None:
    _run_git(["-C", str(repo_root), "fetch", "--all", "--prune"])


def get_last_commit_ts(worktree_path: Path) -> int:
    try:
        out = _run_git(["-C", str(worktree_path), "log", "-1", "--format=%ct"])
        return int(out.strip()) if out else 0
    except GitError:
        return 0


def get_upstream(repo_root: Path, branch: str) -> str | None:
    try:
        upstream = _run_git(
            ["-C", str(repo_root), "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"]
        )
        return upstream.strip()
    except GitError:
        return None


def get_ahead_behind(repo_root: Path, branch: str, upstream: str) -> tuple[int, int]:
    out = _run_git(
        [
            "-C",
            str(repo_root),
            "rev-list",
            "--left-right",
            "--count",
            f"{branch}...{upstream}",
        ]
    )
    left, _, right = out.partition("\t")
    return int(left), int(right)


def is_ancestor(repo_root: Path, branch: str, target: str) -> bool:
    try:
        _run_git(["-C", str(repo_root), "merge-base", "--is-ancestor", branch, target])
        return True
    except GitError:
        return False


def create_worktree(repo_root: Path, branch: str, path: Path) -> None:
    if branch_exists(repo_root, branch):
        raise GitError(f"branch '{branch}' already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["-C", str(repo_root), "worktree", "add", "-b", branch, str(path)])


def remove_worktree(repo_root: Path, path: Path) -> None:
    _run_git(["-C", str(repo_root), "worktree", "remove", "-f", str(path)])


def branch_exists(repo_root: Path, branch: str) -> bool:
    try:
        _run_git(["-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{branch}"])
        return True
    except GitError:
        return False


def rename_branch(repo_root: Path, old: str, new: str) -> None:
    _run_git(["-C", str(repo_root), "branch", "-m", old, new])


def move_worktree(repo_root: Path, old_path: Path, new_path: Path) -> None:
    new_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["-C", str(repo_root), "worktree", "move", str(old_path), str(new_path)])


def branches_merged_into(repo_root: Path, target: str, branches: Iterable[str]) -> list[str]:
    merged: list[str] = []
    for branch in branches:
        if is_ancestor(repo_root, branch, target):
            merged.append(branch)
    return merged
