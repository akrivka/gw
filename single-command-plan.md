# Single-command interactive screen migration plan

## Motivation and context

`gw` currently exposes multiple subcommands (`list`, `new`, `delete`, `rename`, etc.) and uses
non-interactive output for most flows. This adds cognitive overhead and makes common tasks like
switching or pruning worktrees slower. A single interactive screen launched by `gw` can combine the
most-used actions (select, delete, create, rename) into one place, while preserving the two
special-purpose commands `gw init` and `gw doctor` that don’t fit the same interaction model.

Key constraints to preserve:
- **Instant startup**: show cached data immediately, then progressively refresh.
- **Live syncing**: still run git fetch / PR lookup in the background and update the UI as data arrives.
- **1:1 worktree-branch invariant**: still enforced by `gw`’s git operations.

## Scope changes

Keep:
- `gw init` (destructive repo conversion flow).
- `gw doctor` (multi-item repair tool).

Replace with the interactive screen:
- `gw` (no subcommand): full-screen interactive list.
- `gw list`, `gw new`, `gw delete`, `gw rename`, `gw info`: deprecated or removed in favor of the UI.

## UX overview (single screen)

- **List**: table-like rows showing the same info as `gw list` (branch, last commit age, upstream
  status, changes, PR).
- **Navigation**: Up/Down to select.
- **Actions**:
  - `Enter`: exit and print the selected worktree path (for `cd $(gw)` usage).
  - `D`: delete selected worktree (confirmation prompt).
  - `c`: create new worktree+branch (prompt for branch name).
  - `r`: rename selected worktree+branch (prompt for new name).
  - `Esc`/`Ctrl-C`: exit without output.
- **Status line**: show refresh state (`Refreshing…`, errors, or last update timestamp).

## Architectural changes

### 1) UI: new interactive screen

Add a new prompt_toolkit-based UI function in `gw/ui.py` (separate from `run_doctor`).
Responsibilities:
- Hold selection state and list of `WorktreeStatus`.
- Render rows with the same columns as `gw list` (reuse `render_table_lines`).
- Bind keys for actions (`Enter`, `D`, `c`, `r`).
- Support incremental row updates while background refresh runs.

### 2) App: mutation helpers that don’t print

Current `App` methods mix prompting, printing, and side effects. The UI will need
non-interactive methods it can call directly. Add a new layer of methods like:

- `App.load_cached_statuses() -> list[WorktreeStatus]`
- `App.list_worktrees() -> list[git.Worktree]`
- `App.refresh_statuses_local_stream(...)`
- `App.refresh_statuses_remote_stream(...)`
- `App.create_worktree(branch: str) -> Path`
- `App.rename_worktree(old_branch: str, new_branch: str) -> Path`
- `App.delete_worktrees(paths: list[Path]) -> None`

These should not prompt or print; they return data or raise errors.

### 3) Cache + refresh pipeline

Reuse existing cache logic from `Cache` and `App` but let the UI drive the flow:

1. **Initial render** from `Cache.load_worktrees()` (gray/cached color).
2. **Local refresh**: `git.list_worktrees` + per-worktree local status (no fetch).
3. **Remote refresh**: `git fetch` + PR lookup + recompute ahead/behind and PR data.
4. Each stage streams row updates and ends with `Cache.upsert_worktrees`.

### 4) CLI entrypoints

- `gw` (default) calls the interactive UI and prints the selected path when it exits with a result.
- Keep `gw init` and `gw doctor` as-is.
- Remove or deprecate the other subcommands.

## Implementation steps (ordered)

1. **Refactor App methods**
   - Extract non-printing helpers for create/rename/delete and for streaming status updates.
   - Ensure old command methods call these helpers to keep behavior consistent until removed.

2. **Build the UI screen**
   - Add `run_worktree_screen(...)` in `gw/ui.py` with prompt_toolkit
     (use `run_doctor` as a reference).
   - Render with `render_table_lines` or `format_table_row` to match `gw list` layout.

3. **Wire incremental refresh**
   - UI loads cached rows immediately.
   - Kick off background threads for local then remote refresh using the streaming helpers.
   - Use `Application.invalidate()` to re-render after updates.

4. **Wire key actions**
   - Delete: confirm and remove selected row optimistically, then refresh.
   - Create: prompt for branch name, call `App.create_worktree`, insert row, refresh.
   - Rename: prompt for new name, call `App.rename_worktree`, update row, refresh.
   - Enter: exit and print selected worktree path.

5. **Update CLI**
   - Set default `gw` to the interactive screen.
   - Keep `gw init` and `gw doctor` commands unchanged.
   - Remove or deprecate other commands.

6. **Testing / validation**
   - Add unit tests for new App helpers (no UI dependency).
   - Manual verification for UI flows.
   - Verify cache-first rendering still works and refresh updates rows in place.

## Risks and mitigations

- **Threading + UI rendering**: prompt_toolkit requires safe UI invalidation from background
  threads. Use thread-safe callbacks and avoid direct UI mutation outside the event loop.
- **State drift**: deleting/renaming while background refresh is running can race. Mitigate by
  reloading statuses after each mutation and diffing by path.
- **Missing paths**: cached rows might refer to deleted worktrees; local refresh should drop
  any missing entries quickly.

## Follow-ups (optional)

- Add an optional filter/search later (but keep v1 simple).
- Add a keybinding to show details (expanded PR info or diff stats) without leaving the screen.
