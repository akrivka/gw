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

## Development

This program is completely vibe-coded in Rust. You can see the spec I used in docs/spec.md. I initially vibe-coded this in Python (curses -> Textual) but found it too buggy, so told Codex "rewrite in Rust" and it two-shotted a better impl.

Still WIP, suggestions welcome.
