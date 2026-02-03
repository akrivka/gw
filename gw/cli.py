from __future__ import annotations

from pathlib import Path

import click

from .app import App
from .ui import run_worktree_screen


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """gw - manage git worktrees"""
    if ctx.invoked_subcommand is None:
        app = App(Path.cwd())
        selection = run_worktree_screen(app)
        if selection:
            print(selection)


@main.command("init")
def init_cmd() -> None:
    app = App(Path.cwd())
    app.init()


@main.command("doctor")
def doctor_cmd() -> None:
    app = App(Path.cwd())
    app.doctor()


if __name__ == "__main__":
    main()
