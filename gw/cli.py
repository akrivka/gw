import curses
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import click


@dataclass
class WorktreeInfo:
    path: str
    branch: str
    head: str
    ref_name: str | None
    cache_key: str
    last_commit_ts: int
    pull: int
    push: int
    pull_push_validated: bool
    has_upstream: bool
    behind: int
    ahead: int
    additions: int
    deletions: int
    dirty: bool
    pr_number: int | None
    pr_state: str | None
    pr_base: str | None
    pr_url: str | None
    pr_validated: bool
    checks_passed: int | None
    checks_total: int | None
    checks_state: str | None
    checks_validated: bool
    changes_validated: bool


@dataclass
class Cell:
    text: str
    cached: bool = False


_DB_LOCK = threading.Lock()


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


def _cache_db_path(repo_root: str) -> str:
    cache_dir = os.path.expanduser("~/.cache/gw")
    os.makedirs(cache_dir, exist_ok=True)
    repo_id = hashlib.sha1(repo_root.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{repo_id}.sqlite")


def _open_cache_db(repo_root: str) -> sqlite3.Connection:
    db_path = _cache_db_path(repo_root)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worktree_cache (
          branch TEXT PRIMARY KEY,
          path TEXT NOT NULL,
          pr_number INTEGER,
          pr_state TEXT,
          pr_base TEXT,
          pr_url TEXT,
          pr_updated_at INTEGER,
          checks_passed INTEGER,
          checks_total INTEGER,
          checks_state TEXT,
          checks_updated_at INTEGER,
          additions INTEGER,
          deletions INTEGER,
          dirty INTEGER,
          changes_updated_at INTEGER,
          pull INTEGER,
          push INTEGER,
          pullpush_validated_at INTEGER
        )
        """
    )
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(worktree_cache)")}
    add_cols = {
        "additions": "INTEGER",
        "deletions": "INTEGER",
        "dirty": "INTEGER",
        "changes_updated_at": "INTEGER",
    }
    for col, col_type in add_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE worktree_cache ADD COLUMN {col} {col_type}")
    conn.commit()
    return conn


def _cache_key(branch: str, head: str) -> str:
    if branch and branch != "(detached)":
        return branch
    return f"detached:{head}"


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
    current_is_bare = False
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current_path:
                if not current_is_bare and (current_branch or current_head):
                    worktrees.append((current_path, current_branch, current_head))
            current_path = line.split(" ", 1)[1]
            current_branch = ""
            current_head = ""
            current_is_bare = False
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1]
            current_branch = ref.removeprefix("refs/heads/")
        elif line.startswith("HEAD "):
            current_head = line.split(" ", 1)[1]
        elif line.startswith("detached"):
            current_branch = "(detached)"
        elif line.startswith("bare"):
            current_is_bare = True
    if current_path:
        if not current_is_bare and (current_branch or current_head):
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
    with _DB_LOCK:
        conn = _open_cache_db(repo_root)
        for path, branch, head in _parse_worktrees():
            if not os.path.isdir(path):
                continue
            ref_name = None if branch == "(detached)" or not branch else branch
            target = head if ref_name is None else ref_name
            last_commit = _try_run_git(["log", "-1", "--format=%ct", target], cwd=repo_root)
            last_commit_ts = int(last_commit) if last_commit and last_commit.isdigit() else 0

            upstream = None
            if ref_name is not None:
                upstream = _try_run_git(["rev-parse", "--abbrev-ref", f"{ref_name}@{{upstream}}"], cwd=repo_root)
            if upstream:
                ahead, behind = _count_ahead_behind(repo_root, ref_name or target, upstream)
                pull = behind
                push = ahead
                has_upstream = True
            else:
                pull = 0
                push = 0
                has_upstream = False

            ahead, behind = _count_ahead_behind(repo_root, ref_name or target, default_branch)
            key = _cache_key(branch, head)
            cached = conn.execute(
                """
                SELECT
                  pr_number,
                  pr_state,
                  pr_base,
                  pr_url,
                  checks_passed,
                  checks_total,
                  checks_state,
                  additions,
                  deletions,
                  dirty
                FROM worktree_cache
                WHERE branch = ?
                """,
                (key,),
            ).fetchone()
            pr_number = cached["pr_number"] if cached else None
            pr_state = cached["pr_state"] if cached else None
            pr_base = cached["pr_base"] if cached else None
            pr_url = cached["pr_url"] if cached else None
            checks_passed = cached["checks_passed"] if cached else None
            checks_total = cached["checks_total"] if cached else None
            checks_state = cached["checks_state"] if cached else None
            additions = cached["additions"] if cached and cached["additions"] is not None else 0
            deletions = cached["deletions"] if cached and cached["deletions"] is not None else 0
            dirty = bool(cached["dirty"]) if cached and cached["dirty"] is not None else False
            conn.execute(
                """
                INSERT INTO worktree_cache (branch, path)
                VALUES (?, ?)
                ON CONFLICT(branch) DO UPDATE SET path = excluded.path
                """,
                (key, path),
            )
            items.append(
                WorktreeInfo(
                    path=path,
                    branch=branch or head,
                    head=head,
                    ref_name=ref_name,
                    cache_key=key,
                    last_commit_ts=last_commit_ts,
                    pull=pull,
                    push=push,
                    pull_push_validated=False,
                    has_upstream=has_upstream,
                    behind=behind,
                    ahead=ahead,
                    additions=additions,
                    deletions=deletions,
                    dirty=dirty,
                    pr_number=pr_number,
                    pr_state=pr_state,
                    pr_base=pr_base,
                    pr_url=pr_url,
                    pr_validated=False,
                    checks_passed=checks_passed,
                    checks_total=checks_total,
                    checks_state=checks_state,
                    checks_validated=False,
                    changes_validated=False,
                )
            )
        items.sort(key=lambda item: item.last_commit_ts, reverse=True)
        conn.commit()
        conn.close()
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


def _format_row(item: WorktreeInfo, default_branch: str) -> list[Cell]:
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
        Cell(_relative_time(item.last_commit_ts)),
        Cell(pull_push, cached=not item.pull_push_validated),
        Cell(pr, cached=not item.pr_validated),
        Cell(behind_ahead),
        Cell(changes, cached=not item.changes_validated),
        Cell(checks, cached=not item.checks_validated),
    ]


def _column_widths(rows: Iterable[list[Cell]], headers: list[str]) -> list[int]:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell.text))
    return widths


def _draw_screen(
    stdscr: curses.window,
    rows: list[list[Cell]],
    headers: list[str],
    selected: int,
    status_line: str | None,
    warning: str | None,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    command_bar = "Enter: open  |  n: new  |  D: delete  |  R: rename  |  p: pull  |  P: push  |  r: refresh  |  q/Esc: quit"
    if width <= 1 or height <= 1:
        stdscr.refresh()
        return

    def _safe_addnstr(y: int, x: int, text: str, max_width: int, attr: int = 0) -> None:
        if y < 0 or y >= height or x < 0 or x >= width or max_width <= 0:
            return
        try:
            stdscr.addnstr(y, x, text, max_width, attr)
        except curses.error:
            return

    _safe_addnstr(0, 0, command_bar, width - 1)
    if status_line:
        _safe_addnstr(1, 0, status_line, width - 1)
    if warning:
        warning_line = 2 if status_line else 1
        _safe_addnstr(warning_line, 0, warning, width - 1)

    widths = _column_widths(rows, headers)
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
        _safe_addnstr(y, x, header.ljust(widths[idx]), width - x - 1)
        x += widths[idx] + 3

    y += 1
    for idx, row in enumerate(rows):
        if y + idx >= height:
            break
        x = 0
        base_attr = curses.A_REVERSE if idx == selected else 0
        for col_idx, cell in enumerate(row):
            attr = base_attr | (curses.A_DIM if cell.cached else 0)
            _safe_addnstr(y + idx, x, cell.text.ljust(widths[col_idx]), width - x - 1, attr)
            x += widths[col_idx] + 3

    stdscr.refresh()


def _prompt_yes_no(stdscr: curses.window, prompt: str, timeout_ms: int) -> bool:
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


def _prompt_text(stdscr: curses.window, prompt: str, timeout_ms: int) -> str | None:
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


def _branch_exists(repo_root: str, branch: str) -> bool:
    return _try_run_git(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=repo_root) is not None


def _remote_branch_exists(repo_root: str, branch: str) -> bool:
    out = _try_run_git(["ls-remote", "--heads", "origin", branch], cwd=repo_root)
    return bool(out and out.strip())


def _ensure_worktree_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _has_unpushed_commits(repo_root: str, branch: str) -> bool:
    upstream = _try_run_git(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], cwd=repo_root)
    if not upstream:
        return True
    ahead, _ = _count_ahead_behind(repo_root, branch, upstream)
    return ahead > 0


def _run_tui(
    repo_root: str,
    items: list[WorktreeInfo],
    default_branch: str,
    warning: str | None,
    state_lock: threading.Lock,
    refresh_state: dict[str, bool],
) -> str | None:
    headers = ["BRANCH NAME", "LAST COMMIT", "PULL/PUSH", "PULL REQUEST", "BEHIND|AHEAD", "CHANGES", "CHECKS"]

    selected = 0

    status_line: str | None = None

    def _reload_items(selected_branch: str | None) -> int:
        nonlocal default_branch
        default_branch = _default_branch(repo_root)
        new_items = _load_worktrees(repo_root)
        with state_lock:
            items.clear()
            items.extend(new_items)
        if not items:
            return 0
        if selected_branch:
            for idx, item in enumerate(items):
                if item.branch == selected_branch:
                    return idx
        return min(selected, len(items) - 1)

    def _start_refresh_thread() -> None:
        if refresh_state.get("running"):
            return
        refresh_state["running"] = True

        def _runner() -> None:
            try:
                _refresh_from_upstream(repo_root, items, state_lock, shutil.which("gh") is not None)
            finally:
                refresh_state["running"] = False

        threading.Thread(target=_runner, daemon=True).start()

    def _render(stdscr: curses.window) -> None:
        with state_lock:
            rows = [_format_row(item, default_branch) for item in items]
        _draw_screen(stdscr, rows, headers, selected, status_line, warning)

    def _run_with_spinner(
        stdscr: curses.window, message: str, action: Callable[[], None]
    ) -> str | None:
        nonlocal status_line
        spinner = "|/-\\"
        idx = 0
        error: list[str | None] = [None]
        done = threading.Event()

        def _worker() -> None:
            try:
                action()
            except subprocess.CalledProcessError as exc:
                error[0] = exc.stderr.strip() or exc.stdout.strip() or "Unknown error"
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        while not done.is_set():
            status_line = f"{message} {spinner[idx % len(spinner)]}"
            _render(stdscr)
            idx += 1
            time.sleep(0.1)
        return error[0]

    def _inner(stdscr: curses.window) -> str | None:
        nonlocal selected, status_line
        curses.curs_set(0)
        stdscr.keypad(True)
        timeout_ms = 200
        stdscr.timeout(timeout_ms)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
        while True:
            with state_lock:
                row_count = len(items)
            _render(stdscr)
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                return None
            if ch in (curses.KEY_DOWN, ord("j")):
                if row_count:
                    selected = min(selected + 1, row_count - 1)
            elif ch in (curses.KEY_UP, ord("k")):
                if row_count:
                    selected = max(selected - 1, 0)
            elif ch in (curses.KEY_ENTER, 10, 13):
                if items:
                    return items[selected].path
            elif ch in (ord("r"),):
                status_line = "Refreshing..."
                _start_refresh_thread()
            elif ch in (ord("p"), ord("P"), ord("D"), ord("R"), ord("n")):
                if not items:
                    status_line = "No worktrees available."
                    continue
                current = items[selected]
                if ch == ord("p"):
                    if current.ref_name is None:
                        status_line = "Cannot pull a detached worktree."
                        continue
                    error = _run_with_spinner(
                        stdscr, f"Pulling {current.branch}", lambda: _run_git(["pull"], cwd=current.path)
                    )
                    if error:
                        status_line = f"Pull failed: {error}"
                    else:
                        status_line = f"Pulled {current.branch}."
                        selected = _reload_items(current.branch)
                elif ch == ord("P"):
                    if current.ref_name is None:
                        status_line = "Cannot push a detached worktree."
                        continue
                    def _push() -> None:
                        if current.has_upstream:
                            _run_git(["push"], cwd=current.path)
                        else:
                            _run_git(["push", "-u", "origin", current.ref_name], cwd=current.path)

                    error = _run_with_spinner(stdscr, f"Pushing {current.branch}", _push)
                    if error:
                        status_line = f"Push failed: {error}"
                    else:
                        status_line = f"Pushed {current.branch}."
                        selected = _reload_items(current.branch)
                elif ch == ord("D"):
                    if current.ref_name is None:
                        status_line = "Cannot delete a detached worktree."
                        continue
                    warn_parts = []
                    if current.dirty:
                        warn_parts.append("working tree has uncommitted changes")
                    if _has_unpushed_commits(repo_root, current.ref_name):
                        warn_parts.append("branch has unpushed commits")
                    prompt = f"Delete {current.branch}?"
                    if warn_parts:
                        prompt = f"Delete {current.branch} ({'; '.join(warn_parts)})?"
                    if not _prompt_yes_no(stdscr, prompt, timeout_ms):
                        status_line = "Delete cancelled."
                        continue
                    def _delete() -> None:
                        _run_git(["worktree", "remove", "--force", current.path], cwd=repo_root)
                        _run_git(["branch", "-D", current.ref_name], cwd=repo_root)

                    error = _run_with_spinner(stdscr, f"Deleting {current.branch}", _delete)
                    if error:
                        status_line = f"Delete failed: {error}"
                    else:
                        status_line = f"Deleted {current.branch}."
                        selected = _reload_items(None)
                elif ch == ord("R"):
                    if current.ref_name is None:
                        status_line = "Cannot rename a detached worktree."
                        continue
                    new_branch = _prompt_text(stdscr, f"Rename {current.branch} to: ", timeout_ms)
                    if not new_branch:
                        status_line = "Rename cancelled."
                        continue
                    if _try_run_git(["check-ref-format", "--branch", new_branch], cwd=repo_root) is None:
                        status_line = "Invalid branch name."
                        continue
                    if _branch_exists(repo_root, new_branch):
                        status_line = "Branch already exists."
                        continue
                    new_path = os.path.join(repo_root, new_branch)
                    def _rename() -> None:
                        _ensure_worktree_parent(new_path)
                        _run_git(["branch", "-m", current.ref_name, new_branch], cwd=repo_root)
                        _run_git(["worktree", "move", current.path, new_path], cwd=repo_root)

                    error = _run_with_spinner(stdscr, f"Renaming to {new_branch}", _rename)
                    if error:
                        status_line = f"Rename failed: {error}"
                    else:
                        status_line = f"Renamed to {new_branch}."
                        selected = _reload_items(new_branch)
                elif ch == ord("n"):
                    new_branch = _prompt_text(stdscr, "New branch name: ", timeout_ms)
                    if not new_branch:
                        status_line = "Create cancelled."
                        continue
                    if _try_run_git(["check-ref-format", "--branch", new_branch], cwd=repo_root) is None:
                        status_line = "Invalid branch name."
                        continue
                    if _branch_exists(repo_root, new_branch):
                        status_line = "Branch already exists locally."
                        continue
                    new_path = os.path.join(repo_root, new_branch)
                    if os.path.exists(new_path):
                        status_line = "Target worktree path already exists."
                        continue
                    def _create() -> None:
                        if _remote_branch_exists(repo_root, new_branch):
                            _run_git(["fetch", "origin", f"{new_branch}:{new_branch}"], cwd=repo_root)
                            _run_git(["branch", "--set-upstream-to", f"origin/{new_branch}", new_branch], cwd=repo_root)
                            _ensure_worktree_parent(new_path)
                            _run_git(["worktree", "add", new_path, new_branch], cwd=repo_root)
                        else:
                            _ensure_worktree_parent(new_path)
                            _run_git(["worktree", "add", "-b", new_branch, new_path, default_branch], cwd=repo_root)

                    error = _run_with_spinner(stdscr, f"Creating {new_branch}", _create)
                    if error:
                        status_line = f"Create failed: {error}"
                    else:
                        status_line = f"Created {new_branch}."
                        selected = _reload_items(new_branch)

    return curses.wrapper(_inner)


def _classify_checks(conclusions: list[str | None], states: list[str | None]) -> tuple[int, int, str | None]:
    total = len(conclusions)
    passed = 0
    failed = False
    pending = False
    for conclusion, state in zip(conclusions, states):
        if state and state != "COMPLETED":
            pending = True
        if conclusion is None:
            pending = True
            continue
        if conclusion == "SUCCESS":
            passed += 1
        elif conclusion in {"NEUTRAL", "SKIPPED"}:
            passed += 1
        else:
            failed = True
    status = None
    if total == 0:
        status = None
    elif failed:
        status = "fail"
    elif pending:
        status = "pend"
    else:
        status = "ok"
    return passed, total, status


def _refresh_from_upstream(
    repo_root: str,
    items: list[WorktreeInfo],
    state_lock: threading.Lock,
    gh_available: bool,
) -> None:
    _try_run_git(["fetch", "--prune"], cwd=repo_root)
    with _DB_LOCK:
        conn = _open_cache_db(repo_root)
        now = int(time.time())
        with state_lock:
            for item in items:
                if item.ref_name is None:
                    item.pull = 0
                    item.push = 0
                    item.has_upstream = False
                    item.pull_push_validated = True
                    continue
                upstream = _try_run_git(["rev-parse", "--abbrev-ref", f"{item.ref_name}@{{upstream}}"], cwd=repo_root)
                if upstream:
                    ahead, behind = _count_ahead_behind(repo_root, item.ref_name, upstream)
                    item.pull = behind
                    item.push = ahead
                    item.has_upstream = True
                else:
                    item.pull = 0
                    item.push = 0
                    item.has_upstream = False
                item.pull_push_validated = True
                conn.execute(
                    """
                    INSERT INTO worktree_cache (branch, path, pull, push, pullpush_validated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(branch) DO UPDATE SET
                      path = excluded.path,
                      pull = excluded.pull,
                      push = excluded.push,
                      pullpush_validated_at = excluded.pullpush_validated_at
                    """,
                    (item.cache_key, item.path, item.pull, item.push, now),
                )
        conn.commit()
        conn.close()

    with _DB_LOCK:
        conn = _open_cache_db(repo_root)
        for item in items:
            if not os.path.isdir(item.path):
                continue
            additions, deletions, dirty = _diff_counts(item.path)
            with state_lock:
                item.additions = additions
                item.deletions = deletions
                item.dirty = dirty
                item.changes_validated = True
            conn.execute(
                """
                INSERT INTO worktree_cache (branch, path, additions, deletions, dirty, changes_updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch) DO UPDATE SET
                  path = excluded.path,
                  additions = excluded.additions,
                  deletions = excluded.deletions,
                  dirty = excluded.dirty,
                  changes_updated_at = excluded.changes_updated_at
                """,
                (item.cache_key, item.path, additions, deletions, int(dirty), int(time.time())),
            )
        conn.commit()
        conn.close()

    if not gh_available:
        return

    for item in items:
        if item.ref_name is None:
            with state_lock:
                item.pr_number = None
                item.pr_state = None
                item.pr_base = None
                item.pr_url = None
                item.pr_validated = True
                item.checks_passed = None
                item.checks_total = None
                item.checks_state = None
                item.checks_validated = True
            continue
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--state",
                    "all",
                    "--head",
                    item.ref_name,
                    "--json",
                    "number,state,baseRefName,mergedAt,url",
                    "--limit",
                    "1",
                ],
                cwd=repo_root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            pr_list = json.loads(result.stdout.strip() or "[]")
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            pr_list = []

        if not pr_list:
            with state_lock:
                item.pr_number = None
                item.pr_state = None
                item.pr_base = None
                item.pr_url = None
                item.pr_validated = True
                item.checks_passed = None
                item.checks_total = None
                item.checks_state = None
                item.checks_validated = True
            with _DB_LOCK:
                conn = _open_cache_db(repo_root)
                conn.execute(
                    """
                    INSERT INTO worktree_cache (
                      branch, path, pr_number, pr_state, pr_base, pr_url,
                      pr_updated_at, checks_passed, checks_total, checks_state, checks_updated_at
                    )
                    VALUES (?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?)
                    ON CONFLICT(branch) DO UPDATE SET
                      path = excluded.path,
                      pr_number = NULL,
                      pr_state = NULL,
                      pr_base = NULL,
                      pr_url = NULL,
                      pr_updated_at = excluded.pr_updated_at,
                      checks_passed = NULL,
                      checks_total = NULL,
                      checks_state = NULL,
                      checks_updated_at = excluded.checks_updated_at
                    """,
                    (item.cache_key, item.path, int(time.time()), int(time.time())),
                )
                conn.commit()
                conn.close()
            continue

        pr = pr_list[0]
        pr_number = pr.get("number")
        merged_at = pr.get("mergedAt")
        pr_state = "MERGED" if merged_at else pr.get("state")
        pr_base = pr.get("baseRefName")
        pr_url = pr.get("url")

        checks_passed = None
        checks_total = None
        checks_state = None
        if pr_number is not None:
            try:
                checks_result = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "view",
                        str(pr_number),
                        "--json",
                        "statusCheckRollup",
                    ],
                    cwd=repo_root,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                checks_json = json.loads(checks_result.stdout.strip() or "{}")
                rollup = checks_json.get("statusCheckRollup") or []
                conclusions = [item.get("conclusion") for item in rollup]
                states = [item.get("state") for item in rollup]
                checks_passed, checks_total, checks_state = _classify_checks(conclusions, states)
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                checks_passed = None
                checks_total = None
                checks_state = None

        with state_lock:
            item.pr_number = pr_number
            item.pr_state = pr_state
            item.pr_base = pr_base
            item.pr_url = pr_url
            item.pr_validated = True
            item.checks_passed = checks_passed
            item.checks_total = checks_total
            item.checks_state = checks_state
            item.checks_validated = True

        with _DB_LOCK:
            conn = _open_cache_db(repo_root)
            conn.execute(
                """
                INSERT INTO worktree_cache (
                  branch, path, pr_number, pr_state, pr_base, pr_url,
                  pr_updated_at, checks_passed, checks_total, checks_state, checks_updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch) DO UPDATE SET
                  path = excluded.path,
                  pr_number = excluded.pr_number,
                  pr_state = excluded.pr_state,
                  pr_base = excluded.pr_base,
                  pr_url = excluded.pr_url,
                  pr_updated_at = excluded.pr_updated_at,
                  checks_passed = excluded.checks_passed,
                  checks_total = excluded.checks_total,
                  checks_state = excluded.checks_state,
                  checks_updated_at = excluded.checks_updated_at
                """,
                (
                    item.cache_key,
                    item.path,
                    pr_number,
                    pr_state,
                    pr_base,
                    pr_url,
                    int(time.time()),
                    checks_passed,
                    checks_total,
                    checks_state,
                    int(time.time()),
                ),
            )
            conn.commit()
            conn.close()


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
    default_branch = _default_branch(repo_root)
    items = _load_worktrees(repo_root)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        for item in items:
            click.echo(item.path)
        return

    gh_available = shutil.which("gh") is not None
    warning = None if gh_available else "gh not found: install/configure gh for PR data"
    state_lock = threading.Lock()
    refresh_state: dict[str, bool] = {"running": False}
    refresh_thread = threading.Thread(
        target=_refresh_from_upstream,
        args=(repo_root, items, state_lock, gh_available),
        daemon=True,
    )
    refresh_state["running"] = True
    refresh_thread.start()

    def _refresh_done_watcher() -> None:
        refresh_thread.join()
        refresh_state["running"] = False

    threading.Thread(target=_refresh_done_watcher, daemon=True).start()

    selected_path = _run_tui(repo_root, items, default_branch, warning, state_lock, refresh_state)
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
