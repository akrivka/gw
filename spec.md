# gw - a Rust utility for managing git worktrees

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

Each folder contains a worktree for that branch. **Every branch will ALWAYS have a worktree associated with it.** See "Reasoning for branch-worktree 1:1 mapping" below for more details. From now on, we'll refer to worktree, but actually mean the branch-worktree pair (use branch name for worktree name).

Whenever `gw` is run from anywhere within this folder, it should deduce that it is working on `my_repo` (no marker file required).

### UI

Just running `gw` should show a list of worktrees that looks roughly like this:

```
BRANCH NAME       | LAST COMMIT                            | PULL/PUSH                             | PULL REQUEST*1       | BEHIND|AHEAD | CHANGES     | CHECKS
full branch name  | X minutes/hours/days/weeks/months ago  | X↓ Y↑ (dirty - if uncommited/untracked changes) | #1234 (clickable)*2  |      B|A     | +656  -10   | ✅/❌/⏳ M / N
*1: detect if PR was merged and upstream branch deleted, if yes, say it clearly in the these (and previous) columns (important, because we'll want to periodically delete merged branches). Assume merged => branch deleted. If no PR, leave empty.
*2: show target branch name if different from default branch
... (sorted by LAST COMMIT)
```

LAST COMMIT is local (if no relative commits, use creation time). PULL/PUSH are commits to pull/push vs upstream. BEHIND|AHEAD is relative to default branch. CHECKS are PR checks, M/N passed/total.

The list should be navigateable by Up/Down arrow keys, and there should be commands available for the selected worktree (show a status bar above the list with commands). No search/filter; help via `gw help`.

### Commands

* <Enter>: `cd` into that worktree, exit `gw`
* D: delete this worktree, and the associated folder and branch, include confirmation dialog (warn if unpushed commits; do not delete remote branch)
* R: rename the current worktree (both the branch and the folder)
* n: create a new worktee-branch, always base it off of the default branch (use "main" if detection is too hard), run hooks after creation, check upstream if branch with that name is already present, if so, fetch it instead of creating it anew
* p: pull the branch
* P: push the branch
* r: refetch all info
* Esc/q: exit `gw`

### Sync and caching

`gw` should cache all necessary info in a per-repo SQLite DB in a user cache dir (e.g. `~/.cache/gw/<repo-id>.sqlite`), with WAL enabled for concurrent reads. Before showing the `gw` screen, it should refresh all locally-obtainable info (git commands that don't hit the upstream). Then, it should show cached fields in light gray, and start refetching from upstream (git server, GitHub) in the background, making them white when they've been validated. Refresh GH data on each invocation. UI should remain interactive. If `gh` isn't installed, show a user message to install/configure it.

### Health checking

Before the `gw` screen, it should also try to detect any inconsistencies that might be present in the local git repo or folder structure. If there are any branches without worktrees or worktrees without branches, show a separate screen saying something like "Detected issue with gw setup..." and an overview of what it's going to do: worktrees without branches will be deleted, branches without worktrees will get worktrees created; and a confirmation dialog. If anything super weird is detected, consider it unrecoverable.

### Hooks

One often needs to copy some files (such as `.env`) when creating a worktree. We will mimic the Claude Code configuration folder structure and store repo-specific settings in `.gw/settings.json`. After worktree creation, `gw` will read hooks from that file and execute them (repo root, literal commands).

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

You should be able to add a hook with `gw hooks add "<cmd>"` to the local config file (create if missing, append only).
You should also be able to rerun hooks in the current worktree with `gw hooks rerun`.

### `gw init`

There should be an additional command, `gw init`, that initializes the folder structure of the current git repo to be `gw`-compliant. If `gw` is run in a non-compliant git repo structure (non-recoverable via health checking/doctoring^), it should just instruct the user to run `gw init` first. `gw init` should clearly outline what it's going to do (delete tha main clone, keep just the bare repo in the top-level folder, create worktrees for all local branches; may need to move an existing checked-out main at root), with confirmation.

### VCS providers

`gw` will integrate with only GitHub via the `gh` CLI command (not optional). 


## Implementation plan

(I already have a gw-compliant repo structure.)

1. [ x ] Just the basic `gw` screen with only the <Enter> command. No caching. Omit the PR columns that hit the GitHub API.
2. [ x ] Add the remaining columns, caching and lazy-updating. Use `gh` for the GitHub fields. Make sure to adhere to the spec closely here.
3. [ x ] Add the remaining commands.
4. [ x ] Add hooks support.
5. [ ] Add the health checking/doctoring before startup and the `gw` init command.

## Tooling

- **cargo** for build, dependency management, and execution.
- **clap** for CLI parsing.
- **rusqlite** for cache persistence.
- **serde / serde_json** for settings and API payload parsing.
- **comfy-table** for table rendering.

## Appendix

### Reasoning for branch-worktree 1:1 mapping"

This is an invariant that is not present normally, but we enforce it here. The reasoning is that in the age of coding agents, a local branch is not really if you can't quickly run a coding agent on it, either to ask a quick question or implement a feature. git enforces __a branch may be checked out in at most one worktree__, but we basically elevate it to __exactly one worktree__. There's a way around this using detached HEADs, but in my opinion they're not useful since eventually I want to commit to this branch and merge it. This prevents you from running multiple agents on a branch in parallel in isolation, but imo that's a fine tradeoff.
