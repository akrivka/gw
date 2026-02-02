from __future__ import annotations

from pathlib import Path

import click

from .app import App


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """gw - manage git worktrees"""
    if ctx.invoked_subcommand is None:
        App(Path.cwd()).pick_and_print_path()


@main.command("list")
def list_cmd() -> None:
    app = App(Path.cwd())
    app.list_worktrees()


@main.command("info")
def info_cmd() -> None:
    app = App(Path.cwd())
    app.info()


@main.command("new")
@click.argument("branch")
def new_cmd(branch: str) -> None:
    app = App(Path.cwd())
    app.new_worktree(branch)


@main.command("delete")
@click.argument("target", required=False)
def delete_cmd(target: str | None) -> None:
    app = App(Path.cwd())
    if target == "merged":
        app.delete_merged()
    elif target == "no-upstream":
        app.delete_no_upstream()
    else:
        app.delete_worktree(target)


@main.command("rename")
@click.argument("args", nargs=-1)
def rename_cmd(args: tuple[str, ...]) -> None:
    app = App(Path.cwd())
    if len(args) == 1:
        app.rename(None, args[0])
    elif len(args) == 2:
        app.rename(args[0], args[1])
    else:
        raise click.UsageError("rename requires 1 or 2 arguments")


if __name__ == "__main__":
    main()
