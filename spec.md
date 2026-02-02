# gw - a Python utility for managing git worktrees

Git worktrees are a common approach for running multiple AI agents in parallel on a codebase. Managing worktrees and braches can be quite complex, which is where `gw` comes in.

## Spec

This section will document what `gw` is supposed to do. It will, of course, be vibe-coded.

### Folder structure

For each repo, we will have:

```
my_repo/
  .git - contains bare repo
  main/
  branch1/
  branch2/
  prefix/
    branch3/
    branch4/
  ...
```

Each folder contains a worktree for that branch. **Each branch will ALWAYS have a worktree associated with it.** See "Reasoning for branch-worktree 1:1 mapping" for more details. From now on, we'll refer to worktree, but actually mean the worktree-branch pair.

Whenever `gw` is run from anywhere within this folder, it should deduce that it is working on `my_repo`.

### Commands

* `gw` - fuzzy search through all worktrees in current repo, sorted by recency, `cd` into it on enter
* `gw list` - list all worktrees, including extra information like upstream branch status (diff, behind/ahead, PR)
* `gw info` - shows info about the current worktree, if in one
* `gw new [branch-name]` - create a new branch and worktree for it, `cd` into it
* `gw delete` - fuzzy search to delete a worktree, use confirm dialog, `cd` into main if current worktree is deleted
* `gw delete .` - delete current worktree, use confirm dialog, `cd` into main
* `gw delete merged` - delete all worktrees whose branches have been merged into main on the upstream, use confirm dialog, `cd` into main if current worktree is deleted
* `gw delete no-upstream` - delete all worktrees whose branches have no upstream set, use confirm dialog, `cd` into main if current worktree is deleted
* `gw rename [new-branch-name]` - rename current worktree and branch
* `gw rename [old-branch-name] [new-branch-name]` - rename specified worktree and branch

### Sync and caching

`gw` should sync all necessary info on each command (`git fetch` etc.). It should cache all information in a per-repo SQLite DB in a user cache dir (e.g. `~/.cache/gw/<repo-id>.sqlite`), with WAL enabled for concurrent reads. Each command should show cached information immediatelly and show loading states for anything that needs to be updated.

### Hooks

One often needs to copy some files (such as `.env`) when creating a worktree. We will mimic the Claude Code configuration folder structure and store repo-specific settings in `.gw/settings.json`. After worktree creation, `gw` will read hooks from that file and execute them.

Example:

```
{
  "hooks": {
    "PostWorktreeCreation": [
      {
        "type": "command",
        "command": "cp ..."
      }
    ]
  }
}
```

## Architecture overview

### High-level components

1) **CLI (Click)**: parsing, prompts, output.
2) **Application layer**: command orchestration and invariants.
3) **Git service (pygit2)**: repo/worktree/branch operations and sync.
4) **Cache (SQLite)**: per-repo data with WAL, read-first then refresh.
5) **Hooks**: `.gw/settings.json` post-create commands.
6) **UI adapters**: fuzzy picker + minimal loading states.

### Data flow (typical command)

CLI → app → cache read → sync via pygit2 → cache update → render.

### Project layout (proposed)

```
gw/
  gw/
    __init__.py
    cli.py            # click entry points
    app.py            # command orchestration
    git.py            # subprocess wrapper
    cache.py          # sqlite storage
    hooks.py          # settings.json hooks
    ui.py             # formatting + fuzzy selection
    models.py         # dataclasses/typing
  tests/
  pyproject.toml
```

### Tooling

- **uv** for project management, virtualenvs, and dependency resolution.
- **ruff** for lint + format.
- **ty** for static type checks.

## Appendix

### Reasoning for branch-worktree 1:1 mapping"

This is an invariant that is not present normally, but we enforce it here. The reasoning is that in the age of coding agents, a local branch is not really if you can't quickly run a coding agent on it, either to ask a quick question or implement a feature. git enforces __a branch may be checked out in at most one worktree__, but we basically elevate it to __exactly one worktree__. There's a way around this using detached HEADs, but in my opinion they're not useful since eventually I want to commit to this branch and merge it. This prevents you from running multiple agents on a branch in parallel in isolation, but imo that's a fine tradeoff.
