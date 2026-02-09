"""GitHub CLI operations."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PullRequestInfo:
    """Information about a pull request."""

    number: int
    state: str
    base: str | None
    url: str | None


@dataclass
class ChecksInfo:
    """Information about PR checks."""

    passed: int
    total: int
    state: str | None  # "ok", "fail", "pend", or None


def get_pr_info(repo_root: Path, branch: str) -> PullRequestInfo | None:
    """Get pull request information for a branch."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "all",
                "--head",
                branch,
                "--json",
                "number,state,baseRefName,mergedAt,url",
                "--limit",
                "1",
            ],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        pr_list = json.loads(result.stdout.strip() or "[]")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

    if not pr_list:
        return None

    pr = pr_list[0]
    merged_at = pr.get("mergedAt")
    state = "MERGED" if merged_at else pr.get("state", "OPEN")

    return PullRequestInfo(
        number=pr.get("number"),
        state=state,
        base=pr.get("baseRefName"),
        url=pr.get("url"),
    )


def get_checks_info(repo_root: Path, pr_number: int) -> ChecksInfo | None:
    """Get check status information for a pull request."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "statusCheckRollup"],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        checks_json = json.loads(result.stdout.strip() or "{}")
        rollup = checks_json.get("statusCheckRollup") or []
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None

    conclusions = [item.get("conclusion") for item in rollup]
    states = [item.get("state") for item in rollup]

    return classify_checks(conclusions, states)


def classify_checks(conclusions: list[str | None], states: list[str | None]) -> ChecksInfo:
    """Classify check results into passed/total/state."""
    total = len(conclusions)
    passed = 0
    failed = False
    pending = False

    for conclusion, state in zip(conclusions, states):
        if state and state != "COMPLETED":
            pending = True
        if conclusion is None:
            pending = True
            continue
        if conclusion == "SUCCESS":
            passed += 1
        elif conclusion in {"NEUTRAL", "SKIPPED"}:
            passed += 1
        else:
            failed = True

    status: str | None = None
    if total == 0:
        status = None
    elif failed:
        status = "fail"
    elif pending:
        status = "pend"
    else:
        status = "ok"

    return ChecksInfo(passed=passed, total=total, state=status)
