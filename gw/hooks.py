from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def run_post_worktree_creation(repo_root: Path, worktree_path: Path) -> None:
    settings_path = repo_root / ".gw" / "settings.json"
    if not settings_path.exists():
        return
    try:
        data: dict[str, Any] = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return
    hooks = data.get("hooks", {}).get("PostWorktreeCreation", [])
    for hook in hooks:
        if hook.get("type") != "command":
            continue
        cmd = hook.get("command")
        if not cmd:
            continue
        subprocess.run(cmd, shell=True, cwd=str(worktree_path), check=False)
