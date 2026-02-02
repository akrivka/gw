# Repository Guidelines

## Project Structure & Module Organization
- `gw/` is the Python package. Key modules: `gw/cli.py` (Click entry), `gw/app.py` (orchestration), `gw/git.py` (git worktree ops), `gw/cache.py` (SQLite cache), `gw/ui.py` (output/pickers), `gw/models.py` (dataclasses).
- `tests/` contains pytest tests (e.g., `tests/test_core.py`).
- `spec.md` documents the product spec and intended architecture; keep changes aligned with it.
- `pyproject.toml` defines dependencies, tooling (ruff/pytest), and the `gw` console script.

## Build, Test, and Development Commands
- `uv run gw ...` runs the CLI using the locked environment (example: `uv run gw list`).
- `uv run pytest` runs the test suite (pytest is configured with `-q` for quiet output).
- `uv run ruff check .` runs linting.
- `uv run ruff format .` applies formatting (line length 100, double quotes).
- `uv run ty` runs static type checks.

## Coding Style & Naming Conventions
- Indentation: 4 spaces. Keep lines within 100 characters (ruff setting).
- Use double quotes for strings (ruff format setting).
- Prefer explicit, descriptive names for CLI commands and worktree actions.
- Files follow standard pytest naming: `tests/test_*.py`.

## Testing Guidelines
- Framework: pytest.
- Tests should be deterministic and avoid external network access.
- When adding features, extend or add tests under `tests/` with clear fixture setup.
- To run a single test file: `uv run pytest tests/test_core.py`.

## Commit & Pull Request Guidelines
- Commit messages in history are short, imperative, and scope-focused (e.g., “doctor and init commands”).
- PRs should include: a concise description, the commands run (tests/lint), and any user-facing CLI output changes (before/after snippets if relevant).
- Link related issues or specs (`spec.md`) when changes affect behavior or architecture.

## Configuration & Safety Notes
- The tool caches per-repo data in a SQLite DB under a user cache directory; avoid storing secrets there.
- Repo-specific hooks live in `.gw/settings.json` (see `spec.md`); document any new hooks in the spec.
