# Repository Guidelines

## Project Structure & Module Organization

```
gw/
├── __init__.py     # Package metadata and version
├── __main__.py     # Enable `python -m gw`
├── cli.py          # Click CLI entrypoint and commands (init, shell-init)
├── models.py       # Data models (WorktreeInfo, Cell, AheadBehind, etc.)
├── git_ops.py      # Git subprocess operations (all git commands)
├── gh_ops.py       # GitHub CLI operations (PR and checks queries)
├── cache_db.py     # SQLite caching with CacheDB context manager
├── services.py     # Business logic (load_worktrees, refresh_from_upstream)
└── tui.py          # Curses TUI (TuiApp class, rendering, input handling)
```

Other files:
- `pyproject.toml`: project metadata and tool configuration (Ruff, script entrypoint)
- `uv.lock`: locked dependency set for reproducible installs
- `spec.md`: product/spec notes; update when behavior or UX changes materially

### Module Responsibilities

- **models.py**: Pure data structures with no dependencies. Contains `WorktreeInfo`, `Cell`, `AheadBehind`, `DiffStat`, `ParsedWorktree`.
- **git_ops.py**: All git subprocess calls. No curses, no sqlite, no click. Functions like `run()`, `get_repo_root()`, `parse_worktrees()`, `count_ahead_behind()`, etc.
- **gh_ops.py**: GitHub CLI operations. `get_pr_info()`, `get_checks_info()`, `classify_checks()`.
- **cache_db.py**: SQLite persistence. `CacheDB` context manager with methods like `upsert_pr()`, `upsert_changes()`. Thread-safe with `get_db_lock()`.
- **services.py**: Orchestration layer. `load_worktrees()` combines git + cache. `refresh_from_upstream()` updates items from remote.
- **tui.py**: Curses UI. `TuiApp` class handles rendering and key dispatch. Helper functions for prompts and drawing.
- **cli.py**: Click group with `main()`, `init_repo()`, `shell_init()` commands.

## Build, Test, and Development Commands

This repo uses `uv` for env/deps and `ruff` for lint/format:

```bash
uv sync --dev          # create/update .venv with dev tools
uv run gw              # run the interactive TUI
uv run gw --help       # CLI help (subcommands/options)
uv run ruff check .    # lint
uv run ruff format .   # auto-format
uv run ty check        # static type checks
```

## Coding Style & Naming Conventions

- Python 3.11+ codebase; type hints required throughout.
- Indentation: 4 spaces; no tabs.
- Formatting/linting: Ruff with `line-length = 100` and double quotes.
- Naming: `snake_case` for functions/vars, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Use `pathlib.Path` over `str` for filesystem paths in new code.
- Use dataclasses for structured data (prefer frozen when immutable).
- Keep modules focused: no cross-cutting concerns (e.g., git_ops has no UI code).

## Testing Guidelines

- No dedicated test suite yet. If you add tests, use `pytest` and place them under `tests/`.
- Naming convention: `tests/test_*.py` with `test_*` functions.
- Run with `uv run pytest` (add `pytest` to the `dev` dependency group).

## Commit & Pull Request Guidelines

- Commit messages in history are short, imperative, and descriptive (e.g. `add PR functionality`), sometimes with an area prefix (e.g. `gw list: improve animation`).
- PRs include a what/why summary and explicit "how to verify" steps (exact commands/steps).
- PRs include screenshots or a short recording for UI changes (curses output can change subtly).
- PRs call out any new external dependencies (`gh`, network calls) and their failure modes.

## Security & Configuration Tips

- GitHub integration relies on the `gh` CLI; don't add tokens/credentials to this repo.
- Local state is stored in user cache (e.g. `~/.cache/gw/*.sqlite`); avoid making behavior depend on repo-committed files unless explicitly designed.
