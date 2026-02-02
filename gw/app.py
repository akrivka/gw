from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path

from . import git
from .cache import Cache
from .hooks import run_post_worktree_creation
from .models import WorktreeStatus
from .ui import ANSI_CACHED, pick_worktree, render_table, render_table_lines


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
        if show_cached and cached and sys.stdout.isatty():
            worktrees = git.list_worktrees(self.repo_root)
            if not worktrees:
                print(render_table([]))
                return
            row_order = self._order_worktrees(worktrees, cached)
            cached_by_path = {s.path: s for s in cached}
            initial_rows = [
                cached_by_path.get(wt.path, self._placeholder_status(wt)) for wt in row_order
            ]
            cached_lines = render_table_lines(
                initial_rows,
                row_color=ANSI_CACHED,
                skip_branch_color=True,
            )
            sys.stdout.write("\n".join(cached_lines) + "\n")
            sys.stdout.write("Refreshing local...\n")
            sys.stdout.flush()

            line_count = len(cached_lines)
            row_index_by_path = {wt.path: idx for idx, wt in enumerate(row_order)}
            statuses_by_path: dict[Path, WorktreeStatus] = {
                s.path: s for s in initial_rows if s.path
            }

            def update_row(status: WorktreeStatus) -> None:
                statuses_by_path[status.path] = status
                row_idx = row_index_by_path.get(status.path)
                if row_idx is None:
                    return
                line_idx = row_idx + 2
                line = render_table_lines([status])[2]
                self._rewrite_table_line(line_idx, line, line_count)

            local_statuses = self._refresh_statuses_local_stream(
                row_order,
                update_row,
                statuses_by_path,
            )
            self._write_status_line("Refreshing remote...", line_count)
            statuses_by_path.update(local_statuses)

            remote_statuses = self._refresh_statuses_remote_stream(
                row_order,
                statuses_by_path,
                update_row,
            )
            statuses_by_path.update(remote_statuses)
            self._write_status_line("", line_count)
            final_statuses = [statuses_by_path[wt.path] for wt in row_order if wt.path in statuses_by_path]
            self.cache.upsert_worktrees(final_statuses)
            return

        if show_cached and cached:
            cached_sorted = sorted(cached, key=lambda s: s.last_commit_ts, reverse=True)
            print(render_table(cached_sorted))
        statuses = self._refresh_statuses()
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
        if not worktrees:
            self.cache.upsert_worktrees(statuses)
            return statuses
        max_workers = min(32, (os.cpu_count() or 4) * 4, len(worktrees))
        if len(worktrees) == 1:
            statuses = [self._build_status(worktrees[0], default_branch, pr_by_branch)]
        else:
            results: list[WorktreeStatus | None] = [None] * len(worktrees)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_by_index = {
                    executor.submit(
                        self._build_status,
                        wt,
                        default_branch,
                        pr_by_branch,
                    ): idx
                    for idx, wt in enumerate(worktrees)
                }
                for future, idx in future_by_index.items():
                    try:
                        results[idx] = future.result()
                    except Exception:
                        wt = worktrees[idx]
                        results[idx] = WorktreeStatus(
                            path=wt.path,
                            branch=wt.branch,
                            last_commit_ts=0,
                            upstream=None,
                            ahead=None,
                            behind=None,
                            pr_number=None,
                            pr_title=None,
                            pr_state=None,
                            pr_url=None,
                            pr_base=None,
                            changes_added=None,
                            changes_deleted=None,
                            changes_target=None,
                        )
            statuses = [s for s in results if s is not None]
        self.cache.upsert_worktrees(statuses)
        return statuses

    def _build_status(
        self,
        wt: git.Worktree,
        default_branch: str,
        pr_by_branch: dict[str, dict[str, str | int]],
    ) -> WorktreeStatus:
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
        return WorktreeStatus(
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

    def _build_local_status(
        self,
        wt: git.Worktree,
        default_branch: str,
        base: WorktreeStatus | None = None,
    ) -> WorktreeStatus:
        last_commit = git.get_last_commit_ts(wt.path)
        upstream = git.get_upstream(self.repo_root, wt.branch) if wt.branch else None
        ahead = behind = None
        if wt.branch and upstream:
            try:
                ahead, behind = git.get_ahead_behind(self.repo_root, wt.branch, upstream)
            except git.GitError:
                ahead = behind = None
        changes_added = changes_deleted = None
        changes_target = None
        if wt.branch:
            target_label = (
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
        return WorktreeStatus(
            path=wt.path,
            branch=wt.branch,
            last_commit_ts=last_commit,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            pr_number=base.pr_number if base else None,
            pr_title=base.pr_title if base else None,
            pr_state=base.pr_state if base else None,
            pr_url=base.pr_url if base else None,
            pr_base=base.pr_base if base else None,
            changes_added=changes_added,
            changes_deleted=changes_deleted,
            changes_target=changes_target,
        )

    def _apply_remote_updates(
        self,
        status: WorktreeStatus,
        wt: git.Worktree,
        default_branch: str,
        pr_by_branch: dict[str, dict[str, str | int]],
    ) -> WorktreeStatus:
        pr_info = pr_by_branch.get(wt.branch or "") if wt.branch else None
        pr_base = pr_info.get("base") if pr_info else None
        upstream = status.upstream
        ahead = status.ahead
        behind = status.behind
        if wt.branch and upstream:
            try:
                ahead, behind = git.get_ahead_behind(self.repo_root, wt.branch, upstream)
            except git.GitError:
                ahead, behind = None, None
        changes_added = status.changes_added
        changes_deleted = status.changes_deleted
        changes_target = status.changes_target
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
                    changes_added, changes_deleted = None, None
        return WorktreeStatus(
            path=status.path,
            branch=status.branch,
            last_commit_ts=status.last_commit_ts,
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

    def _merge_cached_order(
        self,
        cached_sorted: list[WorktreeStatus],
        statuses: list[WorktreeStatus],
    ) -> list[WorktreeStatus]:
        updated_by_path = {s.path: s for s in statuses}
        ordered: list[WorktreeStatus] = []
        for cached_status in cached_sorted:
            updated = updated_by_path.pop(cached_status.path, None)
            if updated:
                ordered.append(updated)
        if updated_by_path:
            ordered.extend(
                sorted(updated_by_path.values(), key=lambda s: s.last_commit_ts, reverse=True)
            )
        return ordered

    def _order_worktrees(
        self,
        worktrees: list[git.Worktree],
        cached: list[WorktreeStatus],
    ) -> list[git.Worktree]:
        by_path = {wt.path: wt for wt in worktrees}
        cached_sorted = sorted(cached, key=lambda s: s.last_commit_ts, reverse=True)
        ordered: list[git.Worktree] = []
        for status in cached_sorted:
            wt = by_path.pop(status.path, None)
            if wt:
                ordered.append(wt)
        ordered.extend(by_path.values())
        return ordered

    def _placeholder_status(self, wt: git.Worktree) -> WorktreeStatus:
        return WorktreeStatus(
            path=wt.path,
            branch=wt.branch,
            last_commit_ts=0,
            upstream=None,
            ahead=None,
            behind=None,
            pr_number=None,
            pr_title=None,
            pr_state=None,
            pr_url=None,
            pr_base=None,
            changes_added=None,
            changes_deleted=None,
            changes_target=None,
        )

    def _refresh_statuses_local_stream(
        self,
        worktrees: list[git.Worktree],
        on_update: Callable[[WorktreeStatus], None],
        current: dict[Path, WorktreeStatus] | None = None,
    ) -> dict[Path, WorktreeStatus]:
        default_branch = git.get_default_branch(self.repo_root)
        statuses: dict[Path, WorktreeStatus] = {}
        max_workers = min(32, (os.cpu_count() or 4) * 4, len(worktrees))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._build_local_status,
                    wt,
                    default_branch,
                    (current or {}).get(wt.path),
                ): wt
                for wt in worktrees
            }
            for future in as_completed(futures):
                wt = futures[future]
                try:
                    status = future.result()
                except Exception:
                    status = self._placeholder_status(wt)
                statuses[status.path] = status
                on_update(status)
        return statuses

    def _refresh_statuses_remote_stream(
        self,
        worktrees: list[git.Worktree],
        current: dict[Path, WorktreeStatus],
        on_update: Callable[[WorktreeStatus], None],
    ) -> dict[Path, WorktreeStatus]:
        try:
            git.sync_repo(self.repo_root)
        except git.GitError as exc:
            print(f"git fetch failed: {exc}", file=sys.stderr)
        github_repo = git.get_github_repo(self.repo_root)
        pr_by_branch: dict[str, dict[str, str | int]] = {}
        if github_repo:
            pr_by_branch = git.list_pull_requests(self.repo_root, github_repo)
        default_branch = git.get_default_branch(self.repo_root)
        updated: dict[Path, WorktreeStatus] = {}
        max_workers = min(32, (os.cpu_count() or 4) * 4, len(worktrees))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for wt in worktrees:
                base = current.get(wt.path) or self._placeholder_status(wt)
                futures[executor.submit(
                    self._apply_remote_updates,
                    base,
                    wt,
                    default_branch,
                    pr_by_branch,
                )] = wt
            for future in as_completed(futures):
                wt = futures[future]
                try:
                    status = future.result()
                except Exception:
                    status = current.get(wt.path) or self._placeholder_status(wt)
                updated[status.path] = status
                on_update(status)
        return updated

    def _rewrite_table_line(self, line_idx: int, text: str, table_lines: int) -> None:
        if table_lines <= 0:
            return
        if line_idx < 0 or line_idx >= table_lines:
            return
        lines_up = (table_lines + 1) - line_idx
        sys.stdout.write("\x1b[s")
        sys.stdout.write(f"\x1b[{lines_up}A")
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.write(text)
        sys.stdout.write("\x1b[u")
        sys.stdout.flush()

    def _write_status_line(self, text: str, table_lines: int) -> None:
        sys.stdout.write("\x1b[s")
        sys.stdout.write("\x1b[1A")
        sys.stdout.write("\r\x1b[2K")
        if text:
            sys.stdout.write(text)
        sys.stdout.write("\x1b[u")
        sys.stdout.flush()

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
