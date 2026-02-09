"""Business logic services for worktree operations."""

import threading
from pathlib import Path

from gw import gh_ops, git_ops
from gw.cache_db import CacheDB, get_db_lock
from gw.models import WorktreeInfo


def make_cache_key(branch: str, head: str) -> str:
    """Generate a cache key for a worktree."""
    if branch and branch != "(detached)":
        return branch
    return f"detached:{head}"


def load_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    """Load all worktrees with cached metadata."""
    default_branch = git_ops.get_default_branch(repo_root)
    items: list[WorktreeInfo] = []

    with get_db_lock():
        with CacheDB(repo_root) as db:
            for wt in git_ops.parse_worktrees(repo_root):
                if not wt.path.is_dir():
                    continue

                ref_name = None if wt.branch == "(detached)" or not wt.branch else wt.branch
                target = wt.head if ref_name is None else ref_name
                last_commit_ts = git_ops.get_last_commit_ts(repo_root, target)

                upstream = None
                if ref_name is not None:
                    upstream = git_ops.get_upstream(repo_root, ref_name)

                if upstream:
                    ab = git_ops.count_ahead_behind(repo_root, ref_name or target, upstream)
                    pull = ab.behind
                    push = ab.ahead
                    has_upstream = True
                else:
                    pull = 0
                    push = 0
                    has_upstream = False

                ab = git_ops.count_ahead_behind(repo_root, ref_name or target, default_branch)
                cache_key = make_cache_key(wt.branch, wt.head)
                cached = db.get_cached_worktree(cache_key)

                pr_number = cached.pr_number if cached else None
                pr_state = cached.pr_state if cached else None
                pr_base = cached.pr_base if cached else None
                pr_url = cached.pr_url if cached else None
                checks_passed = cached.checks_passed if cached else None
                checks_total = cached.checks_total if cached else None
                checks_state = cached.checks_state if cached else None
                additions = cached.additions if cached else 0
                deletions = cached.deletions if cached else 0
                dirty = cached.dirty if cached else False

                db.upsert_path(cache_key, wt.path)

                items.append(
                    WorktreeInfo(
                        path=wt.path,
                        branch=wt.branch or wt.head,
                        head=wt.head,
                        ref_name=ref_name,
                        cache_key=cache_key,
                        last_commit_ts=last_commit_ts,
                        pull=pull,
                        push=push,
                        pull_push_validated=False,
                        has_upstream=has_upstream,
                        behind=ab.behind,
                        ahead=ab.ahead,
                        additions=additions,
                        deletions=deletions,
                        dirty=dirty,
                        pr_number=pr_number,
                        pr_state=pr_state,
                        pr_base=pr_base,
                        pr_url=pr_url,
                        pr_validated=False,
                        checks_passed=checks_passed,
                        checks_total=checks_total,
                        checks_state=checks_state,
                        checks_validated=False,
                        changes_validated=False,
                    )
                )

            items.sort(key=lambda item: item.last_commit_ts, reverse=True)

    return items


def refresh_pull_push(
    repo_root: Path, items: list[WorktreeInfo], state_lock: threading.Lock
) -> None:
    """Refresh pull/push counts from upstream."""
    git_ops.fetch_prune(repo_root)

    with get_db_lock():
        with CacheDB(repo_root) as db:
            with state_lock:
                for item in items:
                    if item.ref_name is None:
                        item.pull = 0
                        item.push = 0
                        item.has_upstream = False
                        item.pull_push_validated = True
                        continue

                    upstream = git_ops.get_upstream(repo_root, item.ref_name)
                    if upstream:
                        ab = git_ops.count_ahead_behind(repo_root, item.ref_name, upstream)
                        item.pull = ab.behind
                        item.push = ab.ahead
                        item.has_upstream = True
                    else:
                        item.pull = 0
                        item.push = 0
                        item.has_upstream = False

                    item.pull_push_validated = True
                    db.upsert_pull_push(item.cache_key, item.path, item.pull, item.push)


def refresh_changes(repo_root: Path, items: list[WorktreeInfo], state_lock: threading.Lock) -> None:
    """Refresh diff statistics for all worktrees."""
    with get_db_lock():
        with CacheDB(repo_root) as db:
            for item in items:
                if not item.path.is_dir():
                    continue

                stats = git_ops.diff_counts(item.path)

                with state_lock:
                    item.additions = stats.additions
                    item.deletions = stats.deletions
                    item.dirty = stats.dirty
                    item.changes_validated = True

                db.upsert_changes(
                    item.cache_key, item.path, stats.additions, stats.deletions, stats.dirty
                )


def refresh_github(repo_root: Path, items: list[WorktreeInfo], state_lock: threading.Lock) -> None:
    """Refresh GitHub PR and check information."""
    for item in items:
        if item.ref_name is None:
            with state_lock:
                item.pr_number = None
                item.pr_state = None
                item.pr_base = None
                item.pr_url = None
                item.pr_validated = True
                item.checks_passed = None
                item.checks_total = None
                item.checks_state = None
                item.checks_validated = True
            continue

        pr_info = gh_ops.get_pr_info(repo_root, item.ref_name)

        if not pr_info:
            with state_lock:
                item.pr_number = None
                item.pr_state = None
                item.pr_base = None
                item.pr_url = None
                item.pr_validated = True
                item.checks_passed = None
                item.checks_total = None
                item.checks_state = None
                item.checks_validated = True

            with get_db_lock():
                with CacheDB(repo_root) as db:
                    db.upsert_pr_and_checks(
                        item.cache_key, item.path, None, None, None, None, None, None, None
                    )
            continue

        checks_info = gh_ops.get_checks_info(repo_root, pr_info.number)

        with state_lock:
            item.pr_number = pr_info.number
            item.pr_state = pr_info.state
            item.pr_base = pr_info.base
            item.pr_url = pr_info.url
            item.pr_validated = True
            item.checks_passed = checks_info.passed if checks_info else None
            item.checks_total = checks_info.total if checks_info else None
            item.checks_state = checks_info.state if checks_info else None
            item.checks_validated = True

        with get_db_lock():
            with CacheDB(repo_root) as db:
                db.upsert_pr_and_checks(
                    item.cache_key,
                    item.path,
                    pr_info.number,
                    pr_info.state,
                    pr_info.base,
                    pr_info.url,
                    checks_info.passed if checks_info else None,
                    checks_info.total if checks_info else None,
                    checks_info.state if checks_info else None,
                )


def refresh_from_upstream(
    repo_root: Path,
    items: list[WorktreeInfo],
    state_lock: threading.Lock,
    gh_available: bool,
) -> None:
    """Refresh all data from upstream sources."""
    refresh_pull_push(repo_root, items, state_lock)
    refresh_changes(repo_root, items, state_lock)

    if gh_available:
        refresh_github(repo_root, items, state_lock)
