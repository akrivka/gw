from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

import questionary
from prompt_toolkit.completion import FuzzyCompleter, WordCompleter
from prompt_toolkit.shortcuts import prompt

from .models import WorktreeStatus


def format_status(status: WorktreeStatus) -> str:
    branch = status.branch or "(detached)"
    ts = dt.datetime.fromtimestamp(status.last_commit_ts) if status.last_commit_ts else None
    ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "unknown"
    if status.upstream:
        ahead = status.ahead if status.ahead is not None else "?"
        behind = status.behind if status.behind is not None else "?"
        upstream = f"{status.upstream} ↑{ahead} ↓{behind}"
    else:
        upstream = "no-upstream"
    return f"{branch:30} {ts_str:16} {upstream:20} {status.path}"


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
        f"{'BRANCH':30} {'LAST COMMIT':16} {'UPSTREAM':20} PATH",
        "-" * 80,
    ]
    for status in statuses:
        lines.append(format_status(status))
    return "\n".join(lines)
