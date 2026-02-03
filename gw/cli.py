import curses
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable

import click


@dataclass
class WorktreeInfo:
    path: str
    branch: str
    head: str
    last_commit_ts: int
    pull: int
    push: int
    behind: int
    ahead: int
    additions: int
    deletions: int
    dirty: bool


def _run_git(args: list[str], cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _try_run_git(args: list[str], cwd: str | None = None) -> str | None:
    try:
        return _run_git(args, cwd=cwd)
    except subprocess.CalledProcessError:
        return None


def _prune_worktrees(repo_root: str) -> None:
    _try_run_git(["worktree", "prune"], cwd=repo_root)


def _get_repo_root() -> str:
    common_dir = _run_git(["rev-parse", "--git-common-dir"])
    if os.path.basename(common_dir) == ".git":
        return os.path.dirname(common_dir)
    return common_dir


def _parse_worktrees() -> list[tuple[str, str, str]]:
    output = _run_git(["worktree", "list", "--porcelain"])
    worktrees: list[tuple[str, str, str]] = []
    current_path = ""
    current_branch = ""
    current_head = ""
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current_path:
                worktrees.append((current_path, current_branch, current_head))
            current_path = line.split(" ", 1)[1]
            current_branch = ""
            current_head = ""
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1]
            current_branch = ref.removeprefix("refs/heads/")
        elif line.startswith("HEAD "):
            current_head = line.split(" ", 1)[1]
        elif line.startswith("detached"):
            current_branch = "(detached)"
    if current_path:
        worktrees.append((current_path, current_branch, current_head))
    return worktrees


def _default_branch(repo_root: str) -> str:
    ref = _try_run_git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=repo_root)
    if ref:
        return ref.split("/", 1)[1]
    return "main"


def _count_ahead_behind(repo_root: str, left: str, right: str) -> tuple[int, int]:
    out = _try_run_git(["rev-list", "--left-right", "--count", f"{left}...{right}"], cwd=repo_root)
    if not out:
        return 0, 0
    parts = out.split()
    if len(parts) != 2:
        return 0, 0
    return int(parts[0]), int(parts[1])


def _diff_counts(worktree_path: str) -> tuple[int, int, bool]:
    if not os.path.isdir(worktree_path):
        return 0, 0, False
    status = _try_run_git(["status", "--porcelain"], cwd=worktree_path) or ""
    dirty = bool(status.strip())
    additions = 0
    deletions = 0
    numstat = _try_run_git(["diff", "--numstat"], cwd=worktree_path) or ""
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            additions += int(parts[0])
            deletions += int(parts[1])
    untracked = [line for line in status.splitlines() if line.startswith("?? ")]
    additions += len(untracked)
    return additions, deletions, dirty


def _load_worktrees(repo_root: str) -> list[WorktreeInfo]:
    default_branch = _default_branch(repo_root)
    items: list[WorktreeInfo] = []
    for path, branch, head in _parse_worktrees():
        if not os.path.isdir(path):
            continue
        target = head if branch == "(detached)" or not branch else branch
        last_commit = _try_run_git(["log", "-1", "--format=%ct", target], cwd=repo_root)
        last_commit_ts = int(last_commit) if last_commit and last_commit.isdigit() else 0

        upstream = _try_run_git(["rev-parse", "--abbrev-ref", f"{target}@{{upstream}}"], cwd=repo_root)
        if upstream:
            ahead, behind = _count_ahead_behind(repo_root, target, upstream)
            pull = behind
            push = ahead
        else:
            pull = 0
            push = 0

        ahead, behind = _count_ahead_behind(repo_root, target, default_branch)
        additions, deletions, dirty = _diff_counts(path)
        items.append(
            WorktreeInfo(
                path=path,
                branch=branch or head,
                head=head,
                last_commit_ts=last_commit_ts,
                pull=pull,
                push=push,
                behind=behind,
                ahead=ahead,
                additions=additions,
                deletions=deletions,
                dirty=dirty,
            )
        )
    items.sort(key=lambda item: item.last_commit_ts, reverse=True)
    return items


def _relative_time(ts: int) -> str:
    if ts <= 0:
        return "unknown"
    delta = int(time.time()) - ts
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 604800:
        return f"{delta // 86400}d ago"
    if delta < 2629800:
        return f"{delta // 604800}w ago"
    return f"{delta // 2629800}mo ago"


def _format_row(item: WorktreeInfo) -> list[str]:
    pull_push = ""
    if item.pull or item.push:
        pull_push = f"{item.pull}↓ {item.push}↑"
    if item.dirty:
        pull_push = f"{pull_push} (dirty)".strip()

    behind_ahead = f"{item.behind}|{item.ahead}"
    changes = f"+{item.additions} -{item.deletions}"
    return [
        item.branch,
        _relative_time(item.last_commit_ts),
        pull_push,
        behind_ahead,
        changes,
    ]


def _column_widths(rows: Iterable[list[str]], headers: list[str]) -> list[int]:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    return widths


def _draw_screen(stdscr: curses.window, rows: list[list[str]], headers: list[str], selected: int) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    command_bar = "Enter: open worktree  |  q/Esc: quit"
    stdscr.addnstr(0, 0, command_bar, width - 1)

    widths = _column_widths(rows, headers)
    x = 0
    y = 2
    for idx, header in enumerate(headers):
        stdscr.addnstr(y, x, header.ljust(widths[idx]), width - x - 1)
        x += widths[idx] + 3

    y += 1
    for idx, row in enumerate(rows):
        if y + idx >= height:
            break
        x = 0
        if idx == selected:
            stdscr.attron(curses.A_REVERSE)
        for col_idx, cell in enumerate(row):
            stdscr.addnstr(y + idx, x, cell.ljust(widths[col_idx]), width - x - 1)
            x += widths[col_idx] + 3
        if idx == selected:
            stdscr.attroff(curses.A_REVERSE)

    stdscr.refresh()


def _run_tui(items: list[WorktreeInfo]) -> str | None:
    headers = ["BRANCH NAME", "LAST COMMIT", "PULL/PUSH", "BEHIND|AHEAD", "CHANGES"]
    rows = [_format_row(item) for item in items]

    selected = 0

    def _inner(stdscr: curses.window) -> str | None:
        nonlocal selected
        curses.curs_set(0)
        stdscr.keypad(True)
        while True:
            _draw_screen(stdscr, rows, headers, selected)
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                return None
            if ch in (curses.KEY_DOWN, ord("j")):
                if rows:
                    selected = min(selected + 1, len(rows) - 1)
            elif ch in (curses.KEY_UP, ord("k")):
                if rows:
                    selected = max(selected - 1, 0)
            elif ch in (curses.KEY_ENTER, 10, 13):
                if items:
                    return items[selected].path

    return curses.wrapper(_inner)


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """gw: interactive worktree picker."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        repo_root = _get_repo_root()
    except subprocess.CalledProcessError:
        click.echo("gw: not inside a git repository", err=True)
        raise SystemExit(1)

    _prune_worktrees(repo_root)
    items = _load_worktrees(repo_root)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        for item in items:
            click.echo(item.path)
        return

    selected_path = _run_tui(items)
    if selected_path:
        output_file = os.environ.get("GW_OUTPUT_FILE")
        if output_file:
            with open(output_file, "w", encoding="utf-8") as handle:
                handle.write(selected_path)
        else:
            click.echo(selected_path)


@main.command("shell-init")
def shell_init() -> None:
    """Print shell helpers for gw (bash/zsh + fish)."""
    bash_zsh = r'''gw() {
  local tmp dest
  tmp="$(mktemp)" || return $?
  GW_OUTPUT_FILE="$tmp" command gw "$@" </dev/tty >/dev/tty
  dest="$(cat "$tmp" 2>/dev/null)"
  rm -f "$tmp"
  if [ -n "$dest" ]; then
    cd "$dest" || return $?
  fi
}
'''
    fish = r'''function gw
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
'''
    click.echo("# bash/zsh\n" + bash_zsh + "\n# fish\n" + fish)


if __name__ == "__main__":
    main()
