# Repository Guidelines

## Project Structure & Module Organization

```
src/
├── main.rs         # Binary entrypoint (delegates to cli::run)
├── cli.rs          # Clap CLI entrypoint and subcommands (init, shell-init, hooks)
├── models.rs       # Data models (WorktreeInfo, ParsedWorktree, AheadBehind, etc.)
├── git_ops.rs      # Git subprocess operations (all git commands)
├── gh_ops.rs       # GitHub CLI operations (PR and checks queries)
├── cache_db.rs     # SQLite caching with CacheDB API
├── services.rs     # Business logic (load_worktrees, refresh_from_upstream)
├── hooks.rs        # .gw/settings.json hook management and execution
└── tui.rs          # ratatui + crossterm interactive UI
```

Other files:
- `Cargo.toml`: crate metadata and dependencies
- `Cargo.lock`: locked dependency set for reproducible builds
- `spec.md`: product/spec notes; update when behavior or UX changes materially

### Module Responsibilities

- **models.rs**: Data structures only. Contains `WorktreeInfo`, `AheadBehind`, `DiffStat`, `ParsedWorktree`, and GitHub-related structs.
- **git_ops.rs**: All git subprocess calls. No UI or DB logic. Functions include `run()`, `get_repo_root()`, `parse_worktrees()`, `count_ahead_behind()`, branch/worktree operations, and upstream checks.
- **gh_ops.rs**: GitHub CLI operations and parsing. Includes `get_pr_info()`, `get_checks_info()`, `classify_checks()`.
- **cache_db.rs**: SQLite persistence. `CacheDB` API provides cached row reads and upserts for path, pull/push, changes, and PR/check data.
- **services.rs**: Orchestration layer. `load_worktrees()` combines git + cache. `refresh_from_upstream()` refreshes pull/push, diff stats, and GitHub metadata.
- **hooks.rs**: Local hook config in `.gw/settings.json`. Provides add/list/run helpers for `PostWorktreeCreation` command hooks.
- **tui.rs**: ratatui application state, rendering, key dispatch, modals, async refresh, and action execution.
- **cli.rs**: Top-level command routing and non-interactive behaviors.

## Build, Test, and Development Commands

This repo uses Cargo:

```bash
cargo run --             # run gw (interactive TUI when attached to TTY)
cargo run -- --help      # CLI help
cargo check              # fast compile/type check
cargo fmt                # format
cargo clippy -- -D warnings
cargo test               # run tests (when present)
```

## Coding Style & Naming Conventions

- Rust 2021 edition.
- Use `rustfmt` defaults; run `cargo fmt` before finalizing.
- Keep warnings clean in normal development (`cargo clippy -- -D warnings`).
- Naming: `snake_case` for functions/vars/modules, `PascalCase` for structs/enums, `UPPER_SNAKE_CASE` for constants.
- Prefer `Path`/`PathBuf` over raw strings for filesystem paths.
- Keep module boundaries strict: no UI code in `git_ops.rs`; no git subprocess logic in `tui.rs`.

## Testing Guidelines

- No dedicated test suite yet. If you add tests:
- Prefer unit tests in module `#[cfg(test)]` blocks and/or integration tests under `tests/`.
- Run with `cargo test`.

## Commit & Pull Request Guidelines

- Commit messages in history are short, imperative, and descriptive (e.g. `add PR functionality`), sometimes with an area prefix (e.g. `gw list: improve animation`).
- PRs include a what/why summary and explicit "how to verify" steps (exact commands/steps).
- PRs include screenshots or terminal recordings for UI changes (ratatui output can change subtly).
- PRs call out any new external dependencies (`gh`, network calls) and their failure modes.

## Security & Configuration Tips

- GitHub integration relies on the `gh` CLI; don't add tokens/credentials to this repo.
- Local state is stored in user cache (e.g. `~/.cache/gw/*.sqlite`); avoid making behavior depend on repo-committed files unless explicitly designed.
