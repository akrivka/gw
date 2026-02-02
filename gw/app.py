from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

from . import git
from .cache import Cache
from .hooks import run_post_worktree_creation
from .models import WorktreeStatus
from .ui import pick_worktree, render_table


class App:
    def __init__(self, cwd: Path) -> None:
        repo_root = git.get_repo_root(cwd)
        if repo_root is None:
            raise SystemExit("Not inside a git worktree")
        self.repo_root = repo_root
        self.cache = Cache(repo_root, Path.home() / ".cache" / "gw")
        self.cwd = cwd

    def list_worktrees(self, show_cached: bool = True) -> None:
        cached = self.cache.load_worktrees()
        if show_cached and cached:
            print("Cached:")
            print(render_table(sorted(cached, key=lambda s: s.last_commit_ts, reverse=True)))
            print("\nRefreshing...\n")
        statuses = self._refresh_statuses()
        print("Updated:")
        print(render_table(statuses))

    def info(self) -> None:
        current = git.get_current_worktree(self.repo_root, self.cwd)
        if current is None:
            print("Not inside a worktree")
            return
        statuses = self._refresh_statuses()
        status = next((s for s in statuses if s.path == current.path), None)
        if status is None:
            print(f"{current.path}")
            return
        print(render_table([status]))

    def pick_and_print_path(self) -> None:
        statuses = self._refresh_statuses()
        statuses.sort(key=lambda s: s.last_commit_ts, reverse=True)
        selection = pick_worktree(statuses)
        if selection:
            print(selection.path)

    def new_worktree(self, branch: str) -> None:
        path = self.repo_root / branch
        git.create_worktree(self.repo_root, branch, path)
        run_post_worktree_creation(self.repo_root, path)
        self._refresh_statuses()
        print(path)

    def delete_worktree(self, target: str | None) -> None:
        if target == ".":
            current = git.get_current_worktree(self.repo_root, self.cwd)
            if current is None:
                print("Not inside a worktree")
                return
            self._delete_paths([current.path])
            print(self._main_path())
            return
        statuses = self._refresh_statuses()
        statuses.sort(key=lambda s: s.last_commit_ts, reverse=True)
        selection = pick_worktree(statuses)
        if not selection:
            return
        self._delete_paths([selection.path])
        if self._is_current_path(selection.path):
            print(self._main_path())

    def delete_merged(self) -> None:
        statuses = self._refresh_statuses()
        branches = [s.branch for s in statuses if s.branch]
        default_branch = git.get_default_branch(self.repo_root)
        target = f"origin/{default_branch}"
        try:
            merged = set(git.branches_merged_into(self.repo_root, target, branches))
        except git.GitError as exc:
            print(f"Unable to resolve {target}: {exc}")
            return
        to_delete = [s.path for s in statuses if s.branch in merged and s.branch != default_branch]
        self._delete_paths(to_delete)
        if any(self._is_current_path(p) for p in to_delete):
            print(self._main_path())

    def delete_no_upstream(self) -> None:
        statuses = self._refresh_statuses()
        default_branch = git.get_default_branch(self.repo_root)
        to_delete = [s.path for s in statuses if not s.upstream and s.branch != default_branch]
        self._delete_paths(to_delete)
        if any(self._is_current_path(p) for p in to_delete):
            print(self._main_path())

    def rename(self, old: str | None, new: str) -> None:
        statuses = self._refresh_statuses()
        if old is None:
            current = git.get_current_worktree(self.repo_root, self.cwd)
            if current is None or not current.branch:
                print("Not inside a branch worktree")
                return
            old_branch = current.branch
            old_path = current.path
        else:
            status = next((s for s in statuses if s.branch == old), None)
            if status is None:
                print(f"Unknown branch '{old}'")
                return
            old_branch = status.branch or old
            old_path = status.path
        new_path = self.repo_root / new
        git.rename_branch(self.repo_root, old_branch, new)
        git.move_worktree(self.repo_root, old_path, new_path)
        self._refresh_statuses()
        if self._is_current_path(old_path):
            print(new_path)

    def _refresh_statuses(self) -> list[WorktreeStatus]:
        try:
            git.sync_repo(self.repo_root)
        except git.GitError as exc:
            print(f"git fetch failed: {exc}", file=sys.stderr)
        github_repo = git.get_github_repo(self.repo_root)
        pr_by_branch: dict[str, dict[str, str | int]] = {}
        if github_repo:
            pr_by_branch = git.list_pull_requests(self.repo_root, github_repo)
        worktrees = git.list_worktrees(self.repo_root)
        default_branch = git.get_default_branch(self.repo_root)
        statuses: list[WorktreeStatus] = []
        for wt in worktrees:
            last_commit = git.get_last_commit_ts(wt.path)
            upstream = git.get_upstream(self.repo_root, wt.branch) if wt.branch else None
            ahead = behind = None
            if wt.branch and upstream:
                try:
                    ahead, behind = git.get_ahead_behind(self.repo_root, wt.branch, upstream)
                except git.GitError:
                    ahead = behind = None
            pr_info = pr_by_branch.get(wt.branch or "") if wt.branch else None
            pr_base = pr_info.get("base") if pr_info else None
            changes_added = changes_deleted = None
            changes_target = None
            if wt.branch:
                target_label = pr_base or (
                    "main" if git.resolve_ref(self.repo_root, "main") else default_branch
                )
                target_ref = (
                    git.resolve_ref(self.repo_root, target_label)
                    or git.resolve_ref(self.repo_root, f"origin/{target_label}")
                    or git.resolve_ref(self.repo_root, default_branch)
                )
                if target_ref:
                    try:
                        changes_added, changes_deleted = git.get_diff_stats(
                            self.repo_root, target_ref, wt.branch
                        )
                        changes_target = target_label
                    except git.GitError:
                        changes_added = changes_deleted = None
            statuses.append(
                WorktreeStatus(
                    path=wt.path,
                    branch=wt.branch,
                    last_commit_ts=last_commit,
                    upstream=upstream,
                    ahead=ahead,
                    behind=behind,
                    pr_number=pr_info.get("number") if pr_info else None,
                    pr_title=pr_info.get("title") if pr_info else None,
                    pr_state=pr_info.get("state") if pr_info else None,
                    pr_url=pr_info.get("url") if pr_info else None,
                    pr_base=pr_base,
                    changes_added=changes_added,
                    changes_deleted=changes_deleted,
                    changes_target=changes_target,
                )
            )
        self.cache.upsert_worktrees(statuses)
        return statuses

    def _delete_paths(self, paths: Iterable[Path]) -> None:
        paths = [p for p in paths if p.exists()]
        if not paths:
            print("Nothing to delete")
            return
        print("Deleting:")
        for path in paths:
            print(f"  {path}")
        from .ui import confirm

        if not confirm("Proceed?"):
            return
        for path in paths:
            try:
                git.remove_worktree(self.repo_root, path)
            except git.GitError as exc:
                print(f"Failed to delete {path}: {exc}", file=sys.stderr)

    def _main_path(self) -> Path:
        return self.repo_root / git.get_default_branch(self.repo_root)

    def _is_current_path(self, path: Path) -> bool:
        try:
            return self.cwd.resolve().is_relative_to(path.resolve())
        except AttributeError:
            return str(self.cwd.resolve()).startswith(str(path.resolve()))
