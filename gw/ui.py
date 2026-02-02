from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

import questionary
from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
from prompt_toolkit.shortcuts import prompt

from .models import WorktreeStatus


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


def format_status(status: WorktreeStatus) -> str:
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
    labels = [format_status(s) for s in statuses]
    mapping = {label: status for label, status in zip(labels, statuses)}
    completer = FuzzyCompleter(WordCompleter(labels, ignore_case=True))
    selection = prompt("Worktree: ", completer=completer)
    return mapping.get(selection)


def confirm(text: str) -> bool:
    return bool(questionary.confirm(text, default=False).unsafe_ask())


def render_table(statuses: Iterable[WorktreeStatus]) -> str:
    lines = [
        f"{'BRANCH':30} {'LAST COMMIT':20} {'UPSTREAM':20} {'PR':32} PATH",
        "-" * 118,
    ]
    for status in statuses:
        lines.append(format_status(status))
    return "\n".join(lines)
