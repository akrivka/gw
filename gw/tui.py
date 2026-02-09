"""Curses TUI for gw."""

import curses
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from gw import git_ops, services
from gw.models import Cell, WorktreeInfo

HEADERS = [
    "BRANCH NAME",
    "LAST COMMIT",
    "PULL/PUSH",
    "PULL REQUEST",
    "BEHIND|AHEAD",
    "CHANGES",
    "CHECKS",
]
COMMAND_BAR = "Enter: open  |  n: new  |  D: delete  |  R: rename  |  p: pull  |  P: push  |  r: refresh  |  q/Esc: quit"


def relative_time(ts: int) -> str:
    """Format a timestamp as relative time."""
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


def format_row(item: WorktreeInfo, default_branch: str) -> list[Cell]:
    """Format a worktree item as a row of cells."""
    pull_push = ""
    if item.pr_state == "MERGED":
        pull_push = "merged (remote deleted)"
    elif item.has_upstream and (item.pull or item.push):
        pull_push = f"{item.pull}↓ {item.push}↑"
    if item.dirty:
        pull_push = f"{pull_push} (dirty)".strip()

    pr = ""
    if item.pr_number is not None:
        pr_state = item.pr_state or "OPEN"
        if pr_state == "MERGED":
            pr = f"#{item.pr_number} merged (remote deleted)"
        elif pr_state == "CLOSED":
            pr = f"#{item.pr_number} closed"
        else:
            pr = f"#{item.pr_number}"
        if item.pr_base and item.pr_base != default_branch:
            pr = f"{pr} -> {item.pr_base}"

    behind_ahead = f"{item.behind}|{item.ahead}"
    changes = f"+{item.additions} -{item.deletions}"

    checks = ""
    if item.checks_passed is not None and item.checks_total is not None:
        if item.checks_state == "fail":
            checks = f"fail {item.checks_passed}/{item.checks_total}"
        elif item.checks_state == "pend":
            checks = f"pend {item.checks_passed}/{item.checks_total}"
        elif item.checks_state == "ok":
            checks = f"ok {item.checks_passed}/{item.checks_total}"
        else:
            checks = f"{item.checks_passed}/{item.checks_total}"

    return [
        Cell(item.branch),
        Cell(relative_time(item.last_commit_ts)),
        Cell(pull_push, cached=not item.pull_push_validated),
        Cell(pr, cached=not item.pr_validated),
        Cell(behind_ahead),
        Cell(changes, cached=not item.changes_validated),
        Cell(checks, cached=not item.checks_validated),
    ]


def column_widths(rows: Iterable[list[Cell]], headers: list[str]) -> list[int]:
    """Calculate column widths based on content."""
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell.text))
    return widths


def draw_screen(
    stdscr: curses.window,
    rows: list[list[Cell]],
    headers: list[str],
    selected: int,
    status_line: str | None,
    warning: str | None,
) -> None:
    """Draw the TUI screen."""
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    if width <= 1 or height <= 1:
        stdscr.refresh()
        return

    def safe_addnstr(y: int, x: int, text: str, max_width: int, attr: int = 0) -> None:
        if y < 0 or y >= height or x < 0 or x >= width or max_width <= 0:
            return
        try:
            stdscr.addnstr(y, x, text, max_width, attr)
        except curses.error:
            pass

    safe_addnstr(0, 0, COMMAND_BAR, width - 1)
    if status_line:
        safe_addnstr(1, 0, status_line, width - 1)
    if warning:
        warning_line = 2 if status_line else 1
        safe_addnstr(warning_line, 0, warning, width - 1)

    widths = column_widths(rows, headers)
    x = 0
    if status_line and warning:
        y = 4
    elif status_line or warning:
        y = 3
    else:
        y = 2

    if y >= height:
        stdscr.refresh()
        return

    for idx, header in enumerate(headers):
        safe_addnstr(y, x, header.ljust(widths[idx]), width - x - 1)
        x += widths[idx] + 3

    y += 1
    for idx, row in enumerate(rows):
        if y + idx >= height:
            break
        x = 0
        base_attr = curses.A_REVERSE if idx == selected else 0
        for col_idx, cell in enumerate(row):
            attr = base_attr | (curses.A_DIM if cell.cached else 0)
            safe_addnstr(y + idx, x, cell.text.ljust(widths[col_idx]), width - x - 1, attr)
            x += widths[col_idx] + 3

    stdscr.refresh()


def prompt_yes_no(stdscr: curses.window, prompt: str, timeout_ms: int) -> bool:
    """Prompt for a yes/no response."""
    height, width = stdscr.getmaxyx()
    y = height - 1
    if y < 0:
        return False

    stdscr.move(y, 0)
    stdscr.clrtoeol()
    prompt_text = f"{prompt} (y/n): "
    stdscr.addnstr(y, 0, prompt_text, max(0, width - 1))
    stdscr.refresh()
    stdscr.timeout(-1)

    while True:
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            stdscr.timeout(timeout_ms)
            return True
        if ch in (ord("n"), ord("N"), 27):
            stdscr.timeout(timeout_ms)
            return False


def prompt_text(stdscr: curses.window, prompt: str, timeout_ms: int) -> str | None:
    """Prompt for text input."""
    height, width = stdscr.getmaxyx()
    y = height - 1
    if y < 0:
        return None

    stdscr.move(y, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(y, 0, prompt, max(0, width - 1))
    stdscr.refresh()
    stdscr.timeout(-1)
    curses.echo()
    curses.curs_set(1)

    try:
        max_len = max(1, width - len(prompt) - 1)
        raw = stdscr.getstr(y, len(prompt), max_len)
    finally:
        curses.noecho()
        curses.curs_set(0)
        stdscr.timeout(timeout_ms)

    try:
        value = raw.decode("utf-8").strip()
    except Exception:
        value = ""

    return value or None


class TuiApp:
    """Main TUI application."""

    def __init__(
        self,
        repo_root: Path,
        items: list[WorktreeInfo],
        default_branch: str,
        warning: str | None,
        state_lock: threading.Lock,
        refresh_state: dict[str, bool],
    ) -> None:
        self.repo_root = repo_root
        self.items = items
        self.default_branch = default_branch
        self.warning = warning
        self.state_lock = state_lock
        self.refresh_state = refresh_state
        self.selected = 0
        self.status_line: str | None = None
        self.timeout_ms = 200

    def reload_items(self, selected_branch: str | None) -> int:
        """Reload worktree items and return new selection index."""
        self.default_branch = git_ops.get_default_branch(self.repo_root)
        new_items = services.load_worktrees(self.repo_root)

        with self.state_lock:
            self.items.clear()
            self.items.extend(new_items)

        if not self.items:
            return 0

        if selected_branch:
            for idx, item in enumerate(self.items):
                if item.branch == selected_branch:
                    return idx

        return min(self.selected, len(self.items) - 1)

    def start_refresh_thread(self) -> None:
        """Start a background refresh thread."""
        if self.refresh_state.get("running"):
            return

        self.refresh_state["running"] = True

        def runner() -> None:
            try:
                services.refresh_from_upstream(
                    self.repo_root,
                    self.items,
                    self.state_lock,
                    shutil.which("gh") is not None,
                )
            finally:
                self.refresh_state["running"] = False

        threading.Thread(target=runner, daemon=True).start()

    def render(self, stdscr: curses.window) -> None:
        """Render the current state."""
        with self.state_lock:
            rows = [format_row(item, self.default_branch) for item in self.items]
        draw_screen(stdscr, rows, HEADERS, self.selected, self.status_line, self.warning)

    def run_with_spinner(
        self, stdscr: curses.window, message: str, action: Callable[[], None]
    ) -> str | None:
        """Run an action with a spinner, returning error message on failure."""
        spinner = "|/-\\"
        idx = 0
        error: list[str | None] = [None]
        done = threading.Event()

        def worker() -> None:
            try:
                action()
            except subprocess.CalledProcessError as exc:
                error[0] = exc.stderr.strip() or exc.stdout.strip() or "Unknown error"
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()

        while not done.is_set():
            self.status_line = f"{message} {spinner[idx % len(spinner)]}"
            self.render(stdscr)
            idx += 1
            time.sleep(0.1)

        return error[0]

    def handle_pull(self, stdscr: curses.window, current: WorktreeInfo) -> None:
        """Handle pull command."""
        if current.is_detached:
            self.status_line = "Cannot pull a detached worktree."
            return

        error = self.run_with_spinner(
            stdscr,
            f"Pulling {current.branch}",
            lambda: git_ops.pull(current.path),
        )

        if error:
            self.status_line = f"Pull failed: {error}"
        else:
            self.status_line = f"Pulled {current.branch}."
            self.selected = self.reload_items(current.branch)

    def handle_push(self, stdscr: curses.window, current: WorktreeInfo) -> None:
        """Handle push command."""
        if current.is_detached:
            self.status_line = "Cannot push a detached worktree."
            return

        def do_push() -> None:
            if current.has_upstream:
                git_ops.push(current.path)
            else:
                git_ops.push_set_upstream(current.path, current.ref_name)  # type: ignore[arg-type]

        error = self.run_with_spinner(stdscr, f"Pushing {current.branch}", do_push)

        if error:
            self.status_line = f"Push failed: {error}"
        else:
            self.status_line = f"Pushed {current.branch}."
            self.selected = self.reload_items(current.branch)

    def handle_delete(self, stdscr: curses.window, current: WorktreeInfo) -> None:
        """Handle delete command."""
        if current.is_detached:
            self.status_line = "Cannot delete a detached worktree."
            return

        warn_parts = []
        if current.dirty:
            warn_parts.append("working tree has uncommitted changes")
        if git_ops.has_unpushed_commits(self.repo_root, current.ref_name):  # type: ignore[arg-type]
            warn_parts.append("branch has unpushed commits")

        prompt = f"Delete {current.branch}?"
        if warn_parts:
            prompt = f"Delete {current.branch} ({'; '.join(warn_parts)})?"

        if not prompt_yes_no(stdscr, prompt, self.timeout_ms):
            self.status_line = "Delete cancelled."
            return

        def do_delete() -> None:
            git_ops.worktree_remove(self.repo_root, current.path)
            git_ops.branch_delete(self.repo_root, current.ref_name)  # type: ignore[arg-type]

        error = self.run_with_spinner(stdscr, f"Deleting {current.branch}", do_delete)

        if error:
            self.status_line = f"Delete failed: {error}"
        else:
            self.status_line = f"Deleted {current.branch}."
            self.selected = self.reload_items(None)

    def handle_rename(self, stdscr: curses.window, current: WorktreeInfo) -> None:
        """Handle rename command."""
        if current.is_detached:
            self.status_line = "Cannot rename a detached worktree."
            return

        new_branch = prompt_text(stdscr, f"Rename {current.branch} to: ", self.timeout_ms)
        if not new_branch:
            self.status_line = "Rename cancelled."
            return

        if not git_ops.is_valid_branch_name(self.repo_root, new_branch):
            self.status_line = "Invalid branch name."
            return

        if git_ops.branch_exists(self.repo_root, new_branch):
            self.status_line = "Branch already exists."
            return

        new_path = self.repo_root / new_branch

        def do_rename() -> None:
            git_ops.branch_rename(self.repo_root, current.ref_name, new_branch)  # type: ignore[arg-type]
            git_ops.worktree_move(self.repo_root, current.path, new_path)

        error = self.run_with_spinner(stdscr, f"Renaming to {new_branch}", do_rename)

        if error:
            self.status_line = f"Rename failed: {error}"
        else:
            self.status_line = f"Renamed to {new_branch}."
            self.selected = self.reload_items(new_branch)

    def handle_new(self, stdscr: curses.window) -> None:
        """Handle new worktree command."""
        new_branch = prompt_text(stdscr, "New branch name: ", self.timeout_ms)
        if not new_branch:
            self.status_line = "Create cancelled."
            return

        if not git_ops.is_valid_branch_name(self.repo_root, new_branch):
            self.status_line = "Invalid branch name."
            return

        if git_ops.branch_exists(self.repo_root, new_branch):
            self.status_line = "Branch already exists locally."
            return

        new_path = self.repo_root / new_branch
        if new_path.exists():
            self.status_line = "Target worktree path already exists."
            return

        def do_create() -> None:
            if git_ops.remote_branch_exists(self.repo_root, new_branch):
                git_ops.fetch_branch(self.repo_root, new_branch)
                git_ops.branch_set_upstream(self.repo_root, new_branch, f"origin/{new_branch}")
                git_ops.worktree_add(self.repo_root, new_path, new_branch)
            else:
                git_ops.worktree_add(self.repo_root, new_path, new_branch, self.default_branch)

        error = self.run_with_spinner(stdscr, f"Creating {new_branch}", do_create)

        if error:
            self.status_line = f"Create failed: {error}"
        else:
            self.status_line = f"Created {new_branch}."
            self.selected = self.reload_items(new_branch)

    def run(self) -> Path | None:
        """Run the TUI and return the selected path (or None if cancelled)."""

        def inner(stdscr: curses.window) -> Path | None:
            curses.curs_set(0)
            stdscr.keypad(True)
            stdscr.timeout(self.timeout_ms)

            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()

            while True:
                with self.state_lock:
                    row_count = len(self.items)

                self.render(stdscr)
                ch = stdscr.getch()

                if ch in (ord("q"), 27):
                    return None

                if ch in (curses.KEY_DOWN, ord("j")):
                    if row_count:
                        self.selected = min(self.selected + 1, row_count - 1)
                elif ch in (curses.KEY_UP, ord("k")):
                    if row_count:
                        self.selected = max(self.selected - 1, 0)
                elif ch in (curses.KEY_ENTER, 10, 13):
                    if self.items:
                        return self.items[self.selected].path
                elif ch == ord("r"):
                    self.status_line = "Refreshing..."
                    self.start_refresh_thread()
                elif ch in (ord("p"), ord("P"), ord("D"), ord("R"), ord("n")):
                    if not self.items:
                        self.status_line = "No worktrees available."
                        continue

                    if ch == ord("n"):
                        self.handle_new(stdscr)
                    else:
                        current = self.items[self.selected]
                        if ch == ord("p"):
                            self.handle_pull(stdscr, current)
                        elif ch == ord("P"):
                            self.handle_push(stdscr, current)
                        elif ch == ord("D"):
                            self.handle_delete(stdscr, current)
                        elif ch == ord("R"):
                            self.handle_rename(stdscr, current)

        return curses.wrapper(inner)


def run_tui(
    repo_root: Path,
    items: list[WorktreeInfo],
    default_branch: str,
    warning: str | None,
    state_lock: threading.Lock,
    refresh_state: dict[str, bool],
) -> Path | None:
    """Run the TUI application."""
    app = TuiApp(repo_root, items, default_branch, warning, state_lock, refresh_state)
    return app.run()
