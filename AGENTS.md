# Repository Guidelines

## Project Structure & Module Organization

- `gw/`: Python package source.
- `gw/cli.py`: primary entry point (Click CLI + curses TUI).
- `pyproject.toml`: project metadata and tool configuration (Ruff, script entrypoint).
- `uv.lock`: locked dependency set for reproducible installs.
- `spec.md`: product/spec notes; update when behavior or UX changes materially.

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

- Python 3.11+ codebase; prefer type hints throughout.
- Indentation: 4 spaces; no tabs.
- Formatting/linting: Ruff with `line-length = 100` and double quotes.
- Naming: `snake_case` for functions/vars, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Keep CLI/TUI changes cohesive: update the display logic and the underlying data fetch/caching together.

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
