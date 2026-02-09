"""Textual TUI for gw."""

import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static

from gw import git_ops, hooks, services
from gw.models import WorktreeInfo

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
SPINNER = "|/-\\"


CSS = """
Screen {
    layout: vertical;
}

#command_bar {
    padding: 0 1;
    height: 1;
    color: $text-muted;
}

#status_line {
    padding: 0 1;
    height: 1;
}

#warning_line {
    padding: 0 1;
    height: 1;
    color: $warning;
}

#table {
    height: 1fr;
}

.modal {
    align: center middle;
}

.modal-body {
    width: 72;
    max-width: 90;
    height: auto;
    border: thick $primary;
    background: $surface;
    padding: 1 2;
}

.modal-title {
    margin-bottom: 1;
    text-style: bold;
}

.modal-hint {
    margin-top: 1;
    color: $text-muted;
}

.modal-input {
    margin-top: 1;
}
"""


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


def _format_pull_push(item: WorktreeInfo) -> tuple[str, bool]:
    pull_push = ""
    if item.pr_state == "MERGED":
        pull_push = "merged (remote deleted)"
    elif item.has_upstream and (item.pull or item.push):
        pull_push = f"{item.pull}↓ {item.push}↑"
    if item.dirty:
        pull_push = f"{pull_push} (dirty)".strip()
    return pull_push, not item.pull_push_validated


def _format_pr(item: WorktreeInfo, default_branch: str) -> tuple[str, bool]:
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
    return pr, not item.pr_validated


def _format_changes(item: WorktreeInfo) -> tuple[str, bool]:
    return f"+{item.additions} -{item.deletions}", not item.changes_validated


def _format_checks(item: WorktreeInfo) -> tuple[str, bool]:
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
    return checks, not item.checks_validated


def format_row(item: WorktreeInfo, default_branch: str) -> list[tuple[str, bool]]:
    """Format a worktree as displayable values with cached flags."""
    pr, pr_cached = _format_pr(item, default_branch)
    pull_push, pull_push_cached = _format_pull_push(item)
    changes, changes_cached = _format_changes(item)
    checks, checks_cached = _format_checks(item)

    return [
        (item.branch, False),
        (relative_time(item.last_commit_ts), False),
        (pull_push, pull_push_cached),
        (pr, pr_cached),
        (f"{item.behind}|{item.ahead}", False),
        (changes, changes_cached),
        (checks, checks_cached),
    ]


def _render_cell(text: str, cached: bool) -> Text:
    return Text(text, style="dim" if cached else "")


def _error_text(exc: subprocess.CalledProcessError) -> str:
    return (exc.stderr or exc.stdout or "Unknown error").strip() or "Unknown error"


class ConfirmScreen(ModalScreen[bool]):
    """Simple yes/no modal."""

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "No"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            with Vertical(classes="modal-body"):
                yield Static(self.prompt, classes="modal-title")
                yield Static("Press y to confirm, n or Esc to cancel.", classes="modal-hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class TextInputScreen(ModalScreen[str | None]):
    """Text input modal."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal"):
            with Vertical(classes="modal-body"):
                yield Static(self.prompt, classes="modal-title")
                yield Input(
                    placeholder="Type and press Enter", classes="modal-input", id="value_input"
                )
                yield Static("Esc to cancel.", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#value_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TuiApp(App[Path | None]):
    """Main textual application."""

    CSS = CSS
    BINDINGS = [
        Binding("q", "quit_picker", "Quit"),
        Binding("escape", "quit_picker", "Quit"),
        Binding("enter", "choose", "Open"),
        Binding("n", "new_worktree", "New"),
        Binding("d", "delete_worktree", "Delete", show=False),
        Binding("shift+d", "delete_worktree", "Delete"),
        Binding("r", "refresh", "Refresh"),
        Binding("shift+r", "rename_worktree", "Rename"),
        Binding("p", "pull_worktree", "Pull"),
        Binding("shift+p", "push_worktree", "Push"),
    ]

    def __init__(
        self,
        repo_root: Path,
        items: list[WorktreeInfo],
        default_branch: str,
        warning: str | None,
        state_lock: threading.Lock,
        refresh_state: dict[str, bool],
    ) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.items = items
        self.default_branch = default_branch
        self.warning = warning
        self.state_lock = state_lock
        self.refresh_state = refresh_state

        self.selected_path: Path | None = None
        self._busy = False
        self._spinner_index = 0
        self._spinner_message: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(COMMAND_BAR, id="command_bar")
        yield Static("", id="status_line")
        yield Static(self.warning or "", id="warning_line")
        yield DataTable(id="table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.zebra_stripes = True
        table.cursor_type = "row"
        table.add_columns(*HEADERS)
        self._populate_table()
        self.set_interval(0.25, self._tick)

    def _tick(self) -> None:
        if self._spinner_message:
            spinner = SPINNER[self._spinner_index % len(SPINNER)]
            self._spinner_index += 1
            self._set_status(f"{self._spinner_message} {spinner}")

        # Repaint to show results from background refresh threads.
        self._populate_table()

    def _set_status(self, message: str | None) -> None:
        self.query_one("#status_line", Static).update(message or "")

    def _populate_table(self) -> None:
        table = self.query_one("#table", DataTable)
        selected_branch = self._selected_branch()

        with self.state_lock:
            snapshot = list(self.items)

        table.clear(columns=False)
        for item in snapshot:
            values = format_row(item, self.default_branch)
            row = [_render_cell(text, cached) for text, cached in values]
            table.add_row(*row, key=item.path.as_posix())

        if not snapshot:
            return

        row_index = 0
        if selected_branch:
            for idx, item in enumerate(snapshot):
                if item.branch == selected_branch:
                    row_index = idx
                    break
        table.move_cursor(row=row_index)

    def _selected_branch(self) -> str | None:
        item = self._current_item()
        return item.branch if item else None

    def _current_item(self) -> WorktreeInfo | None:
        table = self.query_one("#table", DataTable)
        if not self.items:
            return None
        row = table.cursor_row
        if row < 0 or row >= len(self.items):
            return None
        with self.state_lock:
            if row >= len(self.items):
                return None
            return self.items[row]

    def _reload_items(self, selected_branch: str | None) -> None:
        self.default_branch = git_ops.get_default_branch(self.repo_root)
        new_items = services.load_worktrees(self.repo_root)

        with self.state_lock:
            self.items.clear()
            self.items.extend(new_items)

        self._populate_table()

        if selected_branch:
            table = self.query_one("#table", DataTable)
            for idx, item in enumerate(new_items):
                if item.branch == selected_branch:
                    table.move_cursor(row=idx)
                    break

    def _start_refresh_thread(self) -> None:
        if self.refresh_state.get("running"):
            self._set_status("Refresh already in progress...")
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

    def _run_action_with_spinner(
        self,
        spinner_message: str,
        action: Callable[[], None],
        success_message: str,
        failure_prefix: str,
        selected_branch_after: str | None,
    ) -> None:
        if self._busy:
            self._set_status("Another operation is in progress.")
            return

        self._busy = True
        self._spinner_index = 0
        self._spinner_message = spinner_message

        def runner() -> None:
            try:
                action()
            except subprocess.CalledProcessError as exc:
                self.call_from_thread(
                    self._finish_action,
                    f"{failure_prefix}: {_error_text(exc)}",
                    False,
                    None,
                )
                return
            except Exception as exc:
                self.call_from_thread(self._finish_action, f"{failure_prefix}: {exc}", False, None)
                return

            self.call_from_thread(self._finish_action, success_message, True, selected_branch_after)

        threading.Thread(target=runner, daemon=True).start()

    def _finish_action(
        self, status: str, succeeded: bool, selected_branch_after: str | None
    ) -> None:
        self._spinner_message = None
        self._busy = False
        self._set_status(status)
        if not succeeded:
            return
        if selected_branch_after is not None:
            self._reload_items(selected_branch_after)
        elif "Deleted " in status:
            self._reload_items(None)

    def action_quit_picker(self) -> None:
        self.exit(None)

    def action_choose(self) -> None:
        current = self._current_item()
        if current is None:
            self.exit(None)
            return
        self.selected_path = current.path
        self.exit(current.path)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open selected worktree when DataTable handles Enter."""
        if event.data_table.id != "table":
            return
        self.action_choose()

    async def action_refresh(self) -> None:
        if self._busy:
            self._set_status("Another operation is in progress.")
            return
        self._set_status("Refreshing...")
        self._start_refresh_thread()

    async def action_pull_worktree(self) -> None:
        current = self._current_item()
        if current is None:
            self._set_status("No worktrees available.")
            return
        if current.is_detached:
            self._set_status("Cannot pull a detached worktree.")
            return

        self._run_action_with_spinner(
            f"Pulling {current.branch}",
            lambda: git_ops.pull(current.path),
            f"Pulled {current.branch}.",
            "Pull failed",
            current.branch,
        )

    async def action_push_worktree(self) -> None:
        current = self._current_item()
        if current is None:
            self._set_status("No worktrees available.")
            return
        if current.is_detached:
            self._set_status("Cannot push a detached worktree.")
            return

        def do_push() -> None:
            if current.has_upstream:
                git_ops.push(current.path)
            else:
                git_ops.push_set_upstream(current.path, current.ref_name)  # type: ignore[arg-type]

        self._run_action_with_spinner(
            f"Pushing {current.branch}",
            do_push,
            f"Pushed {current.branch}.",
            "Push failed",
            current.branch,
        )

    async def action_delete_worktree(self) -> None:
        current = self._current_item()
        if current is None:
            self._set_status("No worktrees available.")
            return
        if current.is_detached:
            self._set_status("Cannot delete a detached worktree.")
            return

        warn_parts: list[str] = []
        if current.dirty:
            warn_parts.append("working tree has uncommitted changes")
        if git_ops.has_unpushed_commits(self.repo_root, current.ref_name):  # type: ignore[arg-type]
            warn_parts.append("branch has unpushed commits")

        prompt = f"Delete {current.branch}?"
        if warn_parts:
            prompt = f"Delete {current.branch} ({'; '.join(warn_parts)})?"

        confirmed = await self.push_screen_wait(ConfirmScreen(prompt))
        if not confirmed:
            self._set_status("Delete cancelled.")
            return

        def do_delete() -> None:
            git_ops.worktree_remove(self.repo_root, current.path)
            git_ops.branch_delete(self.repo_root, current.ref_name)  # type: ignore[arg-type]

        self._run_action_with_spinner(
            f"Deleting {current.branch}",
            do_delete,
            f"Deleted {current.branch}.",
            "Delete failed",
            None,
        )

    async def action_rename_worktree(self) -> None:
        current = self._current_item()
        if current is None:
            self._set_status("No worktrees available.")
            return
        if current.is_detached:
            self._set_status("Cannot rename a detached worktree.")
            return

        new_branch = await self.push_screen_wait(TextInputScreen(f"Rename {current.branch} to:"))
        if not new_branch:
            self._set_status("Rename cancelled.")
            return

        if not git_ops.is_valid_branch_name(self.repo_root, new_branch):
            self._set_status("Invalid branch name.")
            return

        if git_ops.branch_exists(self.repo_root, new_branch):
            self._set_status("Branch already exists.")
            return

        new_path = self.repo_root / new_branch

        def do_rename() -> None:
            git_ops.branch_rename(self.repo_root, current.ref_name, new_branch)  # type: ignore[arg-type]
            git_ops.worktree_move(self.repo_root, current.path, new_path)

        self._run_action_with_spinner(
            f"Renaming to {new_branch}",
            do_rename,
            f"Renamed to {new_branch}.",
            "Rename failed",
            new_branch,
        )

    async def action_new_worktree(self) -> None:
        new_branch = await self.push_screen_wait(TextInputScreen("New branch name:"))
        if not new_branch:
            self._set_status("Create cancelled.")
            return

        if not git_ops.is_valid_branch_name(self.repo_root, new_branch):
            self._set_status("Invalid branch name.")
            return

        if git_ops.branch_exists(self.repo_root, new_branch):
            self._set_status("Branch already exists locally.")
            return

        new_path = self.repo_root / new_branch
        if new_path.exists():
            self._set_status("Target worktree path already exists.")
            return

        def do_create() -> None:
            if git_ops.remote_branch_exists(self.repo_root, new_branch):
                git_ops.fetch_branch(self.repo_root, new_branch)
                git_ops.branch_set_upstream(self.repo_root, new_branch, f"origin/{new_branch}")
                git_ops.worktree_add(self.repo_root, new_path, new_branch)
            else:
                git_ops.worktree_add(self.repo_root, new_path, new_branch, self.default_branch)
            hooks.run_post_worktree_creation_hooks(self.repo_root)

        self._run_action_with_spinner(
            f"Creating {new_branch}",
            do_create,
            f"Created {new_branch}.",
            "Create failed",
            new_branch,
        )


def run_tui(
    repo_root: Path,
    items: list[WorktreeInfo],
    default_branch: str,
    warning: str | None,
    state_lock: threading.Lock,
    refresh_state: dict[str, bool],
) -> Path | None:
    """Run the textual TUI application."""
    app = TuiApp(repo_root, items, default_branch, warning, state_lock, refresh_state)
    return app.run()
