from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

import questionary
from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
from prompt_toolkit.shortcuts import prompt

from .models import WorktreeStatus


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


def format_table_row(status: WorktreeStatus) -> str:
    branch = status.branch or "(detached)"
    ts_str = _format_relative_commit_age(status.last_commit_ts)
    status_str = _format_status(status)
    changes = _format_changes(status)
    pr_label = _format_pr_label(status)
    pr_display = _fit(pr_label, PR_WIDTH)
    pr_display = _link(pr_display, status.pr_url)
    return " ".join(
        [
            _fit(branch, BRANCH_WIDTH),
            _fit(ts_str, LAST_COMMIT_WIDTH),
            _fit(status_str, STATUS_WIDTH),
            _fit(changes, CHANGES_WIDTH),
            pr_display,
        ]
    )


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
        lines.append(_colorize(format_table_row(status), row_color))
    return lines


def render_table(statuses: Iterable[WorktreeStatus]) -> str:
    return "\n".join(render_table_lines(statuses))
