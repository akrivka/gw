from __future__ import annotations

import datetime as dt
import sys
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import questionary
from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import create_output
from prompt_toolkit.shortcuts import prompt

from .models import DoctorItem, WorktreeStatus

if TYPE_CHECKING:
    from .app import App


BRANCH_WIDTH = 40
LAST_COMMIT_WIDTH = 20
STATUS_WIDTH = 12
CHANGES_WIDTH = 38
PR_WIDTH = 10
ANSI_RESET = "\x1b[0m"
ANSI_CACHED = "\x1b[38;5;245m"


def _format_relative_commit_age(last_commit_ts: int) -> str:
    if not last_commit_ts:
        return "unknown"
    now = dt.datetime.now()
    ts = dt.datetime.fromtimestamp(last_commit_ts)
    delta = now - ts
    if delta.total_seconds() < 0:
        return "just now"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        minutes = max(1, minutes)
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago"
    hours = minutes // 60
    if hours < 24:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    days = hours // 24
    if days < 7:
        unit = "day" if days == 1 else "days"
        return f"{days} {unit} ago"
    weeks = days // 7
    if days < 30:
        unit = "week" if weeks == 1 else "weeks"
        return f"{weeks} {unit} ago"
    months = days // 30
    unit = "month" if months == 1 else "months"
    return f"{months} {unit} ago"


def _fit(text: str, width: int) -> str:
    if len(text) > width:
        if width <= 3:
            return text[:width]
        return f"{text[: width - 3]}..."
    return text.ljust(width)


def _format_status(status: WorktreeStatus) -> str:
    if not status.upstream:
        return ""
    ahead = status.ahead if status.ahead is not None else "?"
    behind = status.behind if status.behind is not None else "?"
    return f"↑{ahead} ↓{behind}"


def _format_changes(status: WorktreeStatus) -> str:
    if (
        status.changes_added is None
        or status.changes_deleted is None
        or not status.changes_target
    ):
        return "n/a"
    added = str(status.changes_added).ljust(5)
    deleted = str(status.changes_deleted).ljust(5)
    return f"+{added} -{deleted} into {status.changes_target}"


def _format_pr_label(status: WorktreeStatus) -> str:
    if not status.pr_number:
        return "-"
    return f"#{status.pr_number}"


def _link(label: str, url: str | None) -> str:
    if not url:
        return label
    return f"\x1b[4m\x1b]8;;{url}\x1b\\{label}\x1b]8;;\x1b\\\x1b[24m"


def format_table_columns(status: WorktreeStatus) -> list[str]:
    branch = status.branch or "(detached)"
    ts_str = _format_relative_commit_age(status.last_commit_ts)
    status_str = _format_status(status)
    changes = _format_changes(status)
    pr_label = _format_pr_label(status)
    pr_display = _fit(pr_label, PR_WIDTH)
    pr_display = _link(pr_display, status.pr_url)
    return [
        _fit(branch, BRANCH_WIDTH),
        _fit(ts_str, LAST_COMMIT_WIDTH),
        _fit(status_str, STATUS_WIDTH),
        _fit(changes, CHANGES_WIDTH),
        pr_display,
    ]


def format_table_row(status: WorktreeStatus) -> str:
    return " ".join(format_table_columns(status))


def format_pick_label(status: WorktreeStatus) -> str:
    branch = status.branch or "(detached)"
    ts_str = _format_relative_commit_age(status.last_commit_ts)
    if status.upstream:
        ahead = status.ahead if status.ahead is not None else "?"
        behind = status.behind if status.behind is not None else "?"
        upstream = f"{status.upstream} ↑{ahead} ↓{behind}"
    else:
        upstream = "no-upstream"
    pr = "no-pr"
    if status.pr_number:
        state = (status.pr_state or "unknown").lower()
        title = status.pr_title or ""
        if len(title) > 24:
            title = f"{title[:21]}..."
        pr = f"#{status.pr_number} {state} {title}".strip()
    return f"{branch:30} {ts_str:20} {upstream:20} {pr:32} {status.path}"


def pick_worktree(statuses: list[WorktreeStatus]) -> WorktreeStatus | None:
    if not statuses:
        return None
    labels = [format_pick_label(s) for s in statuses]
    mapping = {label: status for label, status in zip(labels, statuses)}
    completer = FuzzyCompleter(WordCompleter(labels, ignore_case=True))
    selection = prompt("Worktree: ", completer=completer)
    return mapping.get(selection)


def confirm(text: str) -> bool:
    return bool(questionary.confirm(text, default=False).unsafe_ask())


def _colorize(text: str, color: str | None) -> str:
    if not color:
        return text
    return f"{color}{text}{ANSI_RESET}"


def render_table_lines(
    statuses: Iterable[WorktreeStatus],
    row_color: str | None = None,
    skip_branch_color: bool = False,
) -> list[str]:
    lines = [
        " ".join(
            [
                _fit("BRANCH", BRANCH_WIDTH),
                _fit("LAST COMMIT", LAST_COMMIT_WIDTH),
                _fit("UPSTREAM", STATUS_WIDTH),
                _fit("CHANGES", CHANGES_WIDTH),
                _fit("PR", PR_WIDTH),
            ]
        ),
        "-" * (BRANCH_WIDTH + LAST_COMMIT_WIDTH + STATUS_WIDTH + CHANGES_WIDTH + PR_WIDTH + 4),
    ]
    for status in statuses:
        if row_color:
            columns = format_table_columns(status)
            if skip_branch_color:
                colored = [columns[0]] + [_colorize(col, row_color) for col in columns[1:]]
            else:
                colored = [_colorize(col, row_color) for col in columns]
            lines.append(" ".join(colored))
        else:
            lines.append(format_table_row(status))
    return lines


def render_table(statuses: Iterable[WorktreeStatus]) -> str:
    return "\n".join(render_table_lines(statuses))


@dataclass
class _DoctorState:
    cursor: int = 0


def run_doctor(items: list[DoctorItem]) -> list[DoctorItem] | None:
    if not items:
        return []
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        for item in items:
            action = item.actions[item.selected]
            print(f"{item.label} [{action}]")
        return None

    state = _DoctorState()

    def _render() -> str:
        lines = [
            "Use up/down to select, left/right to change action, enter to apply, esc to cancel.",
            "",
        ]
        for idx, item in enumerate(items):
            prefix = "> " if idx == state.cursor else "  "
            action = item.actions[item.selected]
            lines.append(f"{prefix}{item.label} [{action}]")
        return "\n".join(lines)

    control = FormattedTextControl(text=_render, focusable=True)
    window = Window(content=control, always_hide_cursor=True)
    layout = Layout(window)

    kb = KeyBindings()

    @kb.add("up")
    def _up(event) -> None:  # type: ignore[no-untyped-def]
        if state.cursor > 0:
            state.cursor -= 1
            event.app.invalidate()

    @kb.add("down")
    def _down(event) -> None:  # type: ignore[no-untyped-def]
        if state.cursor < len(items) - 1:
            state.cursor += 1
            event.app.invalidate()

    @kb.add("left")
    def _left(event) -> None:  # type: ignore[no-untyped-def]
        item = items[state.cursor]
        if len(item.actions) > 1:
            item.selected = (item.selected - 1) % len(item.actions)
            event.app.invalidate()

    @kb.add("right")
    def _right(event) -> None:  # type: ignore[no-untyped-def]
        item = items[state.cursor]
        if len(item.actions) > 1:
            item.selected = (item.selected + 1) % len(item.actions)
            event.app.invalidate()

    @kb.add("enter")
    def _enter(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit(result=items)

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit(result=None)

    app = Application(layout=layout, key_bindings=kb, full_screen=True)
    return app.run()


@dataclass
class _WorktreeScreenState:
    cursor: int = 0
    row_order: list[Path] = field(default_factory=list)
    statuses_by_path: dict[Path, WorktreeStatus] = field(default_factory=dict)
    cached_paths: set[Path] = field(default_factory=set)
    status_line: str = "Loading..."
    refreshing: bool = False
    refresh_requested: bool = False


def run_worktree_screen(app: "App") -> Path | None:
    if not sys.stdin.isatty() or (not sys.stdout.isatty() and not sys.stderr.isatty()):
        return None

    output = None
    if not sys.stdout.isatty() and sys.stderr.isatty():
        try:
            output = create_output(stdout=sys.stderr)
        except Exception:
            output = None
    prompt_stream = sys.stderr if output is not None else sys.stdout

    state = _WorktreeScreenState()
    state_lock = threading.Lock()
    stop_event = threading.Event()

    cached = app.load_cached_statuses()
    if cached:
        cached_sorted = sorted(cached, key=lambda s: s.last_commit_ts, reverse=True)
        state.statuses_by_path = {s.path: s for s in cached_sorted}
        state.row_order = [s.path for s in cached_sorted]
        state.cached_paths = set(state.row_order)
        state.status_line = "Cached data"
    else:
        state.status_line = "Refreshing..."

    def _placeholder_status(path: Path, branch: str | None) -> WorktreeStatus:
        return WorktreeStatus(
            path=path,
            branch=branch,
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

    def _clone_status(status: WorktreeStatus, path: Path, branch: str | None) -> WorktreeStatus:
        return WorktreeStatus(
            path=path,
            branch=branch,
            last_commit_ts=status.last_commit_ts,
            upstream=status.upstream,
            ahead=status.ahead,
            behind=status.behind,
            pr_number=status.pr_number,
            pr_title=status.pr_title,
            pr_state=status.pr_state,
            pr_url=status.pr_url,
            pr_base=status.pr_base,
            changes_added=status.changes_added,
            changes_deleted=status.changes_deleted,
            changes_target=status.changes_target,
        )

    def _table_lines(
        statuses: list[WorktreeStatus],
        cached_paths: set[Path],
        cursor: int,
    ) -> list[str]:
        lines = [
            " ".join(
                [
                    _fit("BRANCH", BRANCH_WIDTH),
                    _fit("LAST COMMIT", LAST_COMMIT_WIDTH),
                    _fit("UPSTREAM", STATUS_WIDTH),
                    _fit("CHANGES", CHANGES_WIDTH),
                    _fit("PR", PR_WIDTH),
                ]
            ),
            "-" * (BRANCH_WIDTH + LAST_COMMIT_WIDTH + STATUS_WIDTH + CHANGES_WIDTH + PR_WIDTH + 4),
        ]
        lines[0] = f"  {lines[0]}"
        lines[1] = f"  {lines[1]}"
        for idx, status in enumerate(statuses):
            columns = format_table_columns(status)
            if status.path in cached_paths:
                columns = [_colorize(col, ANSI_CACHED) for col in columns]
            line = " ".join(columns)
            prefix = "> " if idx == cursor else "  "
            lines.append(f"{prefix}{line}")
        return lines

    def _render() -> str:
        with state_lock:
            row_order = list(state.row_order)
            statuses_by_path = dict(state.statuses_by_path)
            cached_paths = set(state.cached_paths)
            cursor = state.cursor
            status_line = state.status_line
        lines = [
            "Enter: open  D: delete  c: create  r: rename  Esc: cancel",
            "",
        ]
        if not row_order:
            lines.append("No worktrees found.")
            lines.append("")
            lines.append(status_line)
            return "\n".join(lines)
        statuses = [statuses_by_path[path] for path in row_order if path in statuses_by_path]
        lines.extend(_table_lines(statuses, cached_paths, cursor))
        lines.append("")
        lines.append(status_line)
        return "\n".join(lines)

    control = FormattedTextControl(text=_render, focusable=True)
    window = Window(content=control, always_hide_cursor=True)
    layout = Layout(window)
    kb = KeyBindings()

    def _set_status(text: str) -> None:
        with state_lock:
            state.status_line = text
        application.invalidate()

    def _clamp_cursor() -> None:
        with state_lock:
            if not state.row_order:
                state.cursor = 0
            elif state.cursor >= len(state.row_order):
                state.cursor = max(0, len(state.row_order) - 1)

    def _selected_status() -> WorktreeStatus | None:
        with state_lock:
            if not state.row_order:
                return None
            if state.cursor < 0 or state.cursor >= len(state.row_order):
                return None
            path = state.row_order[state.cursor]
            return state.statuses_by_path.get(path)

    def _prompt_text(prompt_text: str) -> str | None:
        result: str | None = None

        def _ask() -> None:
            nonlocal result
            prompt_stream.write(prompt_text)
            prompt_stream.flush()
            try:
                result = input()
            except EOFError:
                result = None

        run_in_terminal(_ask)
        return result

    def _confirm(prompt_text: str) -> bool:
        answer = _prompt_text(f"{prompt_text} [y/N]: ")
        if not answer:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def _merge_order(worktrees: list[Any], previous: list[Path]) -> list[Any]:
        by_path = {wt.path: wt for wt in worktrees}
        ordered: list[Any] = []
        for path in previous:
            wt = by_path.pop(path, None)
            if wt:
                ordered.append(wt)
        ordered.extend(by_path.values())
        return ordered

    def _refresh_worker() -> None:
        while True:
            if stop_event.is_set():
                return
            _set_status("Refreshing...")
            refresh_error = None
            try:
                worktrees = app.list_worktrees()
            except Exception as exc:
                refresh_error = f"Refresh failed: {exc}"
                worktrees = []

            with state_lock:
                ordered = _merge_order(worktrees, state.row_order)
                state.row_order = [wt.path for wt in ordered]
                existing = set(state.row_order)
                for path in list(state.statuses_by_path):
                    if path not in existing:
                        state.statuses_by_path.pop(path, None)
                        state.cached_paths.discard(path)
                for wt in ordered:
                    if wt.path not in state.statuses_by_path:
                        state.statuses_by_path[wt.path] = _placeholder_status(wt.path, wt.branch)
                        state.cached_paths.add(wt.path)
            _clamp_cursor()
            application.invalidate()

            if not worktrees:
                if refresh_error:
                    _set_status(refresh_error)
                else:
                    _set_status("No worktrees found.")
            else:
                current_snapshot = None
                with state_lock:
                    current_snapshot = dict(state.statuses_by_path)

                def _on_update(status: WorktreeStatus) -> None:
                    if stop_event.is_set():
                        return
                    with state_lock:
                        state.statuses_by_path[status.path] = status
                        state.cached_paths.discard(status.path)
                    application.invalidate()

                local_statuses = app.refresh_statuses_local_stream(
                    worktrees,
                    _on_update,
                    current_snapshot,
                )
                with state_lock:
                    state.statuses_by_path.update(local_statuses)
                    state.cached_paths.difference_update(local_statuses.keys())
                    current_snapshot = dict(state.statuses_by_path)

                try:
                    remote_statuses = app.refresh_statuses_remote_stream(
                        worktrees,
                        current_snapshot,
                        _on_update,
                    )
                except Exception as exc:
                    refresh_error = f"Refresh failed: {exc}"
                    remote_statuses = {}

                with state_lock:
                    state.statuses_by_path.update(remote_statuses)

                with state_lock:
                    ordered_paths = list(state.row_order)
                    statuses_by_path = dict(state.statuses_by_path)
                if ordered_paths:
                    app.upsert_cached_statuses(
                        [statuses_by_path[p] for p in ordered_paths if p in statuses_by_path]
                    )
                if refresh_error:
                    _set_status(refresh_error)
                else:
                    timestamp = dt.datetime.now().strftime("%H:%M:%S")
                    _set_status(f"Updated {timestamp}")

            with state_lock:
                state.refreshing = False
                run_again = state.refresh_requested
                state.refresh_requested = False
                if run_again:
                    state.refreshing = True
            if not run_again:
                return

    def _kick_refresh() -> None:
        with state_lock:
            if state.refreshing:
                state.refresh_requested = True
                return
            state.refreshing = True
        thread = threading.Thread(target=_refresh_worker, daemon=True)
        thread.start()

    @kb.add("up")
    def _up(event) -> None:  # type: ignore[no-untyped-def]
        with state_lock:
            if state.cursor > 0:
                state.cursor -= 1
        event.app.invalidate()

    @kb.add("down")
    def _down(event) -> None:  # type: ignore[no-untyped-def]
        with state_lock:
            if state.cursor < len(state.row_order) - 1:
                state.cursor += 1
        event.app.invalidate()

    @kb.add("enter")
    def _enter(event) -> None:  # type: ignore[no-untyped-def]
        selection = _selected_status()
        stop_event.set()
        if selection:
            event.app.exit(result=selection.path)
        else:
            event.app.exit(result=None)

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event) -> None:  # type: ignore[no-untyped-def]
        stop_event.set()
        event.app.exit(result=None)

    @kb.add("d")
    @kb.add("D")
    def _delete(event) -> None:  # type: ignore[no-untyped-def]
        selection = _selected_status()
        if not selection:
            return
        if app.is_current_path(selection.path):
            _set_status("Cannot delete the current worktree. Switch first.")
            return
        label = selection.branch or str(selection.path)
        if selection.branch:
            prompt_label = f"Delete worktree and branch {label}?"
        else:
            prompt_label = f"Delete worktree {label}?"
        if not _confirm(prompt_label):
            return
        try:
            app.delete_worktree_and_branch(selection.path, selection.branch)
        except Exception as exc:
            _set_status(f"Delete failed: {exc}")
            return
        try:
            remaining = {wt.path for wt in app.list_worktrees()}
            if selection.path in remaining:
                _set_status("Delete failed: worktree still present.")
                return
        except Exception:
            pass
        _set_status(f"Deleted {label}")
        with state_lock:
            state.statuses_by_path.pop(selection.path, None)
            state.cached_paths.discard(selection.path)
            if selection.path in state.row_order:
                state.row_order.remove(selection.path)
        _clamp_cursor()
        event.app.invalidate()
        _kick_refresh()

    @kb.add("c")
    @kb.add("C")
    def _create(event) -> None:  # type: ignore[no-untyped-def]
        branch = _prompt_text("New branch name: ")
        if not branch:
            return
        branch = branch.strip()
        if not branch:
            return
        try:
            path = app.create_worktree(branch)
        except Exception as exc:
            _set_status(f"Create failed: {exc}")
            return
        status = _placeholder_status(path, branch)
        with state_lock:
            state.statuses_by_path[path] = status
            state.cached_paths.add(path)
            state.row_order.insert(0, path)
            state.cursor = 0
        event.app.invalidate()
        _kick_refresh()

    @kb.add("r")
    @kb.add("R")
    def _rename(event) -> None:  # type: ignore[no-untyped-def]
        selection = _selected_status()
        if not selection:
            return
        if not selection.branch:
            _set_status("Cannot rename detached worktree.")
            return
        new_branch = _prompt_text(f"Rename {selection.branch} to: ")
        if not new_branch:
            return
        new_branch = new_branch.strip()
        if not new_branch:
            return
        try:
            new_path = app.rename_worktree(selection.branch, new_branch)
        except Exception as exc:
            _set_status(f"Rename failed: {exc}")
            return
        updated = _clone_status(selection, new_path, new_branch)
        with state_lock:
            state.statuses_by_path.pop(selection.path, None)
            state.cached_paths.discard(selection.path)
            state.statuses_by_path[new_path] = updated
            state.cached_paths.add(new_path)
            if selection.path in state.row_order:
                idx = state.row_order.index(selection.path)
                state.row_order[idx] = new_path
                state.cursor = idx
        event.app.invalidate()
        _kick_refresh()

    if output is None:
        application = Application(layout=layout, key_bindings=kb, full_screen=True)
    else:
        application = Application(layout=layout, key_bindings=kb, full_screen=True, output=output)
    _kick_refresh()
    result = application.run()
    stop_event.set()
    return result
