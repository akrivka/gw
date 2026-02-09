"""Hook configuration and execution helpers."""

import json
import subprocess
from pathlib import Path
from typing import cast


class HookError(Exception):
    """Hook configuration or execution failed."""


def _settings_path(repo_root: Path) -> Path:
    return repo_root / ".gw" / "settings.json"


def _load_settings(repo_root: Path) -> dict[str, object]:
    path = _settings_path(repo_root)
    if not path.is_file():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HookError(f"Invalid JSON in {path}") from exc

    if not isinstance(raw, dict):
        raise HookError(f"Invalid settings format in {path}")
    return raw


def _save_settings(repo_root: Path, settings: dict[str, object]) -> None:
    path = _settings_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _expect_object_dict(value: object, section: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HookError(f"Invalid {section} section in settings.")
    return cast(dict[str, object], value)


def _expect_hook_entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise HookError("Invalid PostWorktreeCreation section in settings.")
    entries: list[dict[str, object]] = []
    for entry in value:
        entries.append(_expect_object_dict(entry, "hook entry"))
    return entries


def add_post_worktree_creation_hook(repo_root: Path, command: str) -> None:
    """Append a post-worktree-creation command hook."""
    normalized_command = command.strip()
    if not normalized_command:
        raise HookError("Hook command cannot be empty.")

    settings = _load_settings(repo_root)
    hooks_raw = settings.get("hooks")
    hooks = {} if hooks_raw is None else _expect_object_dict(hooks_raw, "hooks")

    post_create_raw = hooks.get("PostWorktreeCreation")
    post_create = [] if post_create_raw is None else _expect_hook_entries(post_create_raw)

    post_create.append({"type": "command", "command": normalized_command})
    hooks["PostWorktreeCreation"] = cast(object, post_create)
    settings["hooks"] = hooks
    _save_settings(repo_root, settings)


def get_post_worktree_creation_commands(repo_root: Path) -> list[str]:
    """Return literal commands for post-worktree-creation hooks."""
    settings = _load_settings(repo_root)
    hooks_raw = settings.get("hooks")
    if hooks_raw is None:
        return []
    hooks = _expect_object_dict(hooks_raw, "hooks")

    post_create_raw = hooks.get("PostWorktreeCreation")
    if post_create_raw is None:
        return []
    post_create = _expect_hook_entries(post_create_raw)

    commands: list[str] = []
    for entry in post_create:
        if entry.get("type") != "command":
            continue
        command = entry.get("command")
        if isinstance(command, str) and command.strip():
            commands.append(command)
    return commands


def run_post_worktree_creation_hooks(repo_root: Path, cwd: Path | None = None) -> None:
    """Run configured post-worktree-creation command hooks."""
    run_cwd = cwd or repo_root
    for command in get_post_worktree_creation_commands(repo_root):
        try:
            subprocess.run(
                command,
                cwd=run_cwd,
                shell=True,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip() or "unknown error"
            raise HookError(f"Hook failed: `{command}`: {stderr}") from exc
