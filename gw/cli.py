"""CLI entrypoint for gw."""

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import click

from gw import git_ops, hooks, services
from gw.tui import run_tui


def _clear_repo_root(repo_root: Path, keep_entries: set[str]) -> None:
    """Clear repo root except for specified entries."""
    for entry in os.listdir(repo_root):
        if entry in keep_entries:
            continue
        path = repo_root / entry
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except FileNotFoundError:
            continue


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """gw: interactive worktree picker."""
    if ctx.invoked_subcommand is not None:
        return

    try:
        repo_root = git_ops.get_repo_root()
    except subprocess.CalledProcessError:
        click.echo("gw: not inside a git repository", err=True)
        raise SystemExit(1)

    git_ops.prune_worktrees(repo_root)
    default_branch = git_ops.get_default_branch(repo_root)
    items = services.load_worktrees(repo_root)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        for item in items:
            click.echo(item.path)
        return

    gh_available = shutil.which("gh") is not None
    warning = None if gh_available else "gh not found: install/configure gh for PR data"

    state_lock = threading.Lock()
    refresh_state: dict[str, bool] = {"running": False}

    refresh_thread = threading.Thread(
        target=services.refresh_from_upstream,
        args=(repo_root, items, state_lock, gh_available),
        daemon=True,
    )
    refresh_state["running"] = True
    refresh_thread.start()

    def refresh_done_watcher() -> None:
        refresh_thread.join()
        refresh_state["running"] = False

    threading.Thread(target=refresh_done_watcher, daemon=True).start()

    selected_path = run_tui(repo_root, items, default_branch, warning, state_lock, refresh_state)

    if selected_path:
        output_file = os.environ.get("GW_OUTPUT_FILE")
        if output_file:
            with open(output_file, "w", encoding="utf-8") as handle:
                handle.write(str(selected_path))
        else:
            click.echo(selected_path)


@main.command("init")
def init_repo() -> None:
    """Initialize the current repo into a gw-compliant layout."""
    try:
        repo_root = git_ops.get_repo_root()
    except subprocess.CalledProcessError:
        click.echo("gw init: not inside a git repository", err=True)
        raise SystemExit(1)

    is_bare = git_ops.is_bare_repo(repo_root)
    branches = git_ops.list_local_branches(repo_root)

    if not branches:
        click.echo("gw init: no local branches found", err=True)
        raise SystemExit(1)

    worktree_map = git_ops.worktree_branch_map(repo_root)

    def get_conflicting_paths(branches_to_add: list[str]) -> list[str]:
        conflicts: list[str] = []
        for branch in branches_to_add:
            target = repo_root / branch
            if target.exists() and branch not in worktree_map:
                conflicts.append(branch)
        return conflicts

    if is_bare:
        missing = [branch for branch in branches if branch not in worktree_map]
        conflicts = get_conflicting_paths(missing)

        if conflicts:
            click.echo(
                "gw init: cannot create worktrees; paths already exist: " + ", ".join(conflicts),
                err=True,
            )
            raise SystemExit(1)

        click.echo(f"gw init will initialize worktrees under {repo_root}")
        if missing:
            click.echo(f"- create worktrees for {len(missing)} local branches")
        else:
            click.echo("- no new worktrees to create")

        if not click.confirm("Continue?", default=False):
            click.echo("gw init: cancelled")
            return

        for branch in missing:
            target = repo_root / branch
            git_ops.worktree_add(repo_root, target, branch)

        click.echo("gw init: done")
        return

    if git_ops.has_uncommitted_changes(repo_root):
        click.echo("gw init: working tree has uncommitted or untracked changes", err=True)
        raise SystemExit(1)

    root_branch_paths = {
        branch for branch, path in worktree_map.items() if path.resolve() == repo_root.resolve()
    }

    missing = [
        branch for branch in branches if branch not in worktree_map or branch in root_branch_paths
    ]

    worktree_paths = [wt.path for wt in git_ops.parse_worktrees(repo_root)]
    keep_entries = git_ops.get_entries_to_preserve(repo_root, worktree_paths)
    conflicts = get_conflicting_paths(missing)

    if conflicts:
        click.echo(
            "gw init: cannot create worktrees; paths already exist: " + ", ".join(conflicts),
            err=True,
        )
        raise SystemExit(1)

    click.echo(f"gw init will convert {repo_root} into a gw-compliant layout:")
    click.echo("- delete the current working tree at the repo root")
    click.echo("- keep only the bare repo in the top-level .git directory")
    click.echo("- ensure every local branch has a worktree")

    if missing:
        click.echo(f"- create {len(missing)} new worktrees under {repo_root}/<branch>")

    preserved = sorted(entry for entry in keep_entries if entry != ".git")
    if preserved:
        click.echo(f"- preserve existing worktree paths: {', '.join(preserved)}")

    if not click.confirm("Continue?", default=False):
        click.echo("gw init: cancelled")
        return

    git_ops.set_bare(repo_root)
    _clear_repo_root(repo_root, keep_entries)

    for branch in missing:
        target = repo_root / branch
        git_ops.worktree_add(repo_root, target, branch)

    click.echo("gw init: done")


@main.command("shell-init")
def shell_init() -> None:
    """Print shell helpers for gw (bash/zsh + fish)."""
    bash_zsh = r"""gw() {
  local tmp dest
  tmp="$(mktemp)" || return $?
  GW_OUTPUT_FILE="$tmp" command gw "$@" </dev/tty >/dev/tty
  dest="$(cat "$tmp" 2>/dev/null)"
  rm -f "$tmp"
  if [ -n "$dest" ]; then
    cd "$dest" || return $?
  fi
}
"""
    fish = r"""function gw
  set -l tmp (mktemp)
  if test -z "$tmp"
    return 1
  end
  env GW_OUTPUT_FILE=$tmp command gw $argv </dev/tty >/dev/tty
  set -l dest (cat $tmp 2>/dev/null)
  rm -f $tmp
  if test -n "$dest"
    cd "$dest"
  end
end
"""
    click.echo("# bash/zsh\n" + bash_zsh + "\n# fish\n" + fish)


@main.group("hooks")
def hooks_cmd() -> None:
    """Manage repo-local hooks."""


@hooks_cmd.command("add")
@click.argument("command")
def add_hook(command: str) -> None:
    """Add a PostWorktreeCreation command hook."""
    try:
        repo_root = git_ops.get_repo_root()
    except subprocess.CalledProcessError:
        click.echo("gw hooks add: not inside a git repository", err=True)
        raise SystemExit(1)

    try:
        hooks.add_post_worktree_creation_hook(repo_root, command)
    except hooks.HookError as exc:
        click.echo(f"gw hooks add: {exc}", err=True)
        raise SystemExit(1)

    click.echo("gw hooks add: hook added")


@hooks_cmd.command("rerun")
def rerun_hooks() -> None:
    """Rerun PostWorktreeCreation hooks in the current worktree root."""
    try:
        repo_root = git_ops.get_repo_root()
        worktree_root = Path(git_ops.run(["rev-parse", "--show-toplevel"], cwd=Path.cwd())).resolve()
    except subprocess.CalledProcessError:
        click.echo("gw hooks rerun: not inside a git worktree", err=True)
        raise SystemExit(1)

    try:
        hooks.run_post_worktree_creation_hooks(repo_root, cwd=worktree_root)
    except hooks.HookError as exc:
        click.echo(f"gw hooks rerun: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"gw hooks rerun: hooks executed in {worktree_root}")


if __name__ == "__main__":
    main()
