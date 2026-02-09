"""Git subprocess operations."""

import os
import subprocess
from pathlib import Path
from typing import Sequence

from gw.models import AheadBehind, DiffStat, ParsedWorktree


class GitError(Exception):
    """Git command failed."""

    def __init__(self, cmd: Sequence[str], stderr: str) -> None:
        self.cmd = cmd
        self.stderr = stderr
        super().__init__(f"git {' '.join(cmd)}: {stderr}")


def run(args: Sequence[str], cwd: Path | None = None) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def try_run(args: Sequence[str], cwd: Path | None = None) -> str | None:
    """Run a git command, returning None on failure."""
    try:
        return run(args, cwd=cwd)
    except subprocess.CalledProcessError:
        return None


def get_repo_root() -> Path:
    """Get the root directory of the repository."""
    common_dir = Path(run(["rev-parse", "--git-common-dir"])).resolve()
    if common_dir.name == ".git":
        return common_dir.parent
    return common_dir


def is_bare_repo(repo_root: Path) -> bool:
    """Check if the repository is bare."""
    return run(["rev-parse", "--is-bare-repository"], cwd=repo_root) == "true"


def get_default_branch(repo_root: Path) -> str:
    """Get the default branch name (falls back to 'main')."""
    ref = try_run(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=repo_root)
    if ref:
        return ref.split("/", 1)[1]
    return "main"


def prune_worktrees(repo_root: Path) -> None:
    """Prune stale worktree references."""
    try_run(["worktree", "prune"], cwd=repo_root)


def parse_worktrees(repo_root: Path | None = None) -> list[ParsedWorktree]:
    """Parse the output of git worktree list --porcelain."""
    output = run(["worktree", "list", "--porcelain"], cwd=repo_root)
    worktrees: list[ParsedWorktree] = []
    current_path = ""
    current_branch = ""
    current_head = ""
    current_is_bare = False

    for line in output.splitlines():
        if line.startswith("worktree "):
            if current_path and not current_is_bare and (current_branch or current_head):
                worktrees.append(
                    ParsedWorktree(
                        path=Path(current_path), branch=current_branch, head=current_head
                    )
                )
            current_path = line.split(" ", 1)[1]
            current_branch = ""
            current_head = ""
            current_is_bare = False
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1]
            current_branch = ref.removeprefix("refs/heads/")
        elif line.startswith("HEAD "):
            current_head = line.split(" ", 1)[1]
        elif line.startswith("detached"):
            current_branch = "(detached)"
        elif line.startswith("bare"):
            current_is_bare = True

    if current_path and not current_is_bare and (current_branch or current_head):
        worktrees.append(
            ParsedWorktree(path=Path(current_path), branch=current_branch, head=current_head)
        )

    return worktrees


def count_ahead_behind(repo_root: Path, left: str, right: str) -> AheadBehind:
    """Count commits ahead and behind between two refs."""
    out = try_run(["rev-list", "--left-right", "--count", f"{left}...{right}"], cwd=repo_root)
    if not out:
        return AheadBehind(0, 0)
    parts = out.split()
    if len(parts) != 2:
        return AheadBehind(0, 0)
    return AheadBehind(ahead=int(parts[0]), behind=int(parts[1]))


def diff_counts(worktree_path: Path) -> DiffStat:
    """Get diff statistics for a worktree."""
    if not worktree_path.is_dir():
        return DiffStat(0, 0, False)

    status = try_run(["status", "--porcelain"], cwd=worktree_path) or ""
    dirty = bool(status.strip())
    additions = 0
    deletions = 0

    numstat = try_run(["diff", "--numstat"], cwd=worktree_path) or ""
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            additions += int(parts[0])
            deletions += int(parts[1])

    untracked = [line for line in status.splitlines() if line.startswith("?? ")]
    additions += len(untracked)

    return DiffStat(additions=additions, deletions=deletions, dirty=dirty)


def get_last_commit_ts(repo_root: Path, target: str) -> int:
    """Get the timestamp of the last commit on a ref."""
    last_commit = try_run(["log", "-1", "--format=%ct", target], cwd=repo_root)
    return int(last_commit) if last_commit and last_commit.isdigit() else 0


def get_upstream(repo_root: Path, ref_name: str) -> str | None:
    """Get the upstream tracking branch for a ref."""
    return try_run(["rev-parse", "--abbrev-ref", f"{ref_name}@{{upstream}}"], cwd=repo_root)


def list_local_branches(repo_root: Path) -> list[str]:
    """List all local branch names."""
    out = run(["for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=repo_root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def branch_exists(repo_root: Path, branch: str) -> bool:
    """Check if a local branch exists."""
    return try_run(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=repo_root) is not None


def remote_branch_exists(repo_root: Path, branch: str) -> bool:
    """Check if a remote branch exists on origin."""
    out = try_run(["ls-remote", "--heads", "origin", branch], cwd=repo_root)
    return bool(out and out.strip())


def is_valid_branch_name(repo_root: Path, name: str) -> bool:
    """Check if a string is a valid git branch name."""
    return try_run(["check-ref-format", "--branch", name], cwd=repo_root) is not None


def has_unpushed_commits(repo_root: Path, branch: str) -> bool:
    """Check if a branch has commits not pushed to upstream."""
    upstream = get_upstream(repo_root, branch)
    if not upstream:
        return True
    ab = count_ahead_behind(repo_root, branch, upstream)
    return ab.ahead > 0


def has_uncommitted_changes(repo_root: Path) -> bool:
    """Check if the working tree has uncommitted or untracked changes."""
    status = run(["status", "--porcelain"], cwd=repo_root)
    return bool(status.strip())


def fetch_prune(repo_root: Path) -> None:
    """Fetch from origin with prune."""
    try_run(["fetch", "--prune"], cwd=repo_root)


def worktree_add(repo_root: Path, path: Path, branch: str, base: str | None = None) -> None:
    """Add a new worktree."""
    ensure_worktree_parent(path)
    if base:
        run(["worktree", "add", "-b", branch, str(path), base], cwd=repo_root)
    else:
        run(["worktree", "add", str(path), branch], cwd=repo_root)


def worktree_remove(repo_root: Path, path: Path) -> None:
    """Remove a worktree."""
    run(["worktree", "remove", "--force", str(path)], cwd=repo_root)


def worktree_move(repo_root: Path, src: Path, dest: Path) -> None:
    """Move a worktree to a new path."""
    ensure_worktree_parent(dest)
    run(["worktree", "move", str(src), str(dest)], cwd=repo_root)


def branch_delete(repo_root: Path, branch: str) -> None:
    """Delete a local branch."""
    run(["branch", "-D", branch], cwd=repo_root)


def branch_rename(repo_root: Path, old_name: str, new_name: str) -> None:
    """Rename a local branch."""
    run(["branch", "-m", old_name, new_name], cwd=repo_root)


def branch_set_upstream(repo_root: Path, branch: str, upstream: str) -> None:
    """Set the upstream tracking branch."""
    run(["branch", "--set-upstream-to", upstream, branch], cwd=repo_root)


def fetch_branch(repo_root: Path, branch: str) -> None:
    """Fetch a specific branch from origin."""
    run(["fetch", "origin", f"{branch}:{branch}"], cwd=repo_root)


def pull(worktree_path: Path) -> None:
    """Pull in a worktree."""
    run(["pull"], cwd=worktree_path)


def push(worktree_path: Path) -> None:
    """Push from a worktree."""
    run(["push"], cwd=worktree_path)


def push_set_upstream(worktree_path: Path, branch: str) -> None:
    """Push and set upstream tracking branch."""
    run(["push", "-u", "origin", branch], cwd=worktree_path)


def set_bare(repo_root: Path) -> None:
    """Set the repository to bare mode."""
    run(["config", "core.bare", "true"], cwd=repo_root)


def ensure_worktree_parent(path: Path) -> None:
    """Ensure the parent directory of a worktree path exists."""
    parent = path.parent
    if parent:
        parent.mkdir(parents=True, exist_ok=True)


def worktree_branch_map(repo_root: Path) -> dict[str, Path]:
    """Get a mapping of branch names to worktree paths."""
    mapping: dict[str, Path] = {}
    for wt in parse_worktrees(repo_root):
        if wt.branch and wt.branch != "(detached)":
            mapping[wt.branch] = wt.path
    return mapping


def get_entries_to_preserve(repo_root: Path, worktree_paths: list[Path]) -> set[str]:
    """Get directory entries that should be preserved during init."""
    keep = {".git", ".gw"}
    for path in worktree_paths:
        abs_path = path.resolve()
        if abs_path == repo_root.resolve():
            continue
        try:
            common = os.path.commonpath([repo_root, abs_path])
        except ValueError:
            continue
        if common != str(repo_root.resolve()):
            continue
        rel = abs_path.relative_to(repo_root)
        top = rel.parts[0] if rel.parts else ""
        if top:
            keep.add(top)
    return keep
