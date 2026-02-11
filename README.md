# gw

A cli utlity for managing git worktrees and branches built around a simple every: **1:1 mapping between worktrees and branches**. \
_(for simplicity we will refer to the worktree-branch pair simply as "worktree" here)_

![](https://i.imgur.com/T4qqM9h.png)

For each repo, you'll have the following folder structure (run `gw init` to initialize it):

```
my_repo/
  .git        - bare repo
  main/       - wt with main
  branch1/    - wt with branch1
  yourname/
    branch3/  - wt with yourname/branch3
```

Every local branch has a worktree. `gw` takes care of enforcing this invariant.

Run `gw` to
* quickly switch worktrees
* see their upstream status, pull and push
* create, delete and rename them

## Installation

After each installation method, make sure to run `gw shell-init` and add the corresponding function to your shell config. Otherwise, switching directories won't work.

### `cargo`

```bash
cargo install --path .
```

### Homebrew

Coming soon.

## Hooks

`gw` supports repo-local command hooks that run after creating a worktree.

### Add a hook

```bash
gw hooks add "<command>"
```

Example:

```bash
gw hooks add "ln -s ~/repo/.env .env"
```

This writes hook config to `.gw/settings.json` in the repository root.

### Rerun hooks in the current worktree

```bash
gw hooks rerun
```

This executes the configured post-creation hooks in your current worktree directory.

### Settings format

Hooks are stored in `.gw/settings.json` under `hooks.PostWorktreeCreation`:

```json
{
  "hooks": {
    "PostWorktreeCreation": [
      {
        "type": "command",
        "command": "git fetch --all --prune"
      }
    ]
  }
}
```

### Behavior notes

- Hooks run after `gw` creates a new worktree in the TUI flow.
- Hooks are executed with `sh -c` on Unix and `cmd /C` on Windows.
- Commands run in the newly created worktree directory (or current worktree for `gw hooks rerun`).
- If a hook exits non-zero, `gw` stops and reports the first failing command.
- `gw hooks add` appends a new entry; it does not deduplicate existing commands.

## Development

This program is completely vibe-coded in Rust. You can see the spec I used in docs/spec.md. I initially vibe-coded this in Python (curses -> Textual) but found it too buggy, so told Codex "rewrite in Rust" and it two-shotted a better impl.

Still WIP, suggestions welcome.

### Deployment

Update Homebrew by tagging a release, updating the tap formula, and pushing the tap change:

```bash
# 1) Tag and push a new release from this repo
git tag vX.Y.Z
git push origin vX.Y.Z

# 2) Re-generate formula in the tap from the new tag tarball
brew create --tap akrivka/tap \
  --set-name gw \
  https://github.com/akrivka/gw/archive/refs/tags/vX.Y.Z.tar.gz

# 3) Update Formula/gw.rb with:
#    - url "https://github.com/akrivka/gw/archive/refs/tags/vX.Y.Z.tar.gz"
#    - sha256 "<new-tarball-sha256>"

# 4) Validate locally
brew audit --strict --new-formula gw
brew install --build-from-source akrivka/tap/gw
brew test gw

# 5) Commit and push tap changes
cd "$(brew --repository akrivka/tap)"
git add Formula/gw.rb
git commit -m "Update gw to vX.Y.Z"
git push
```
