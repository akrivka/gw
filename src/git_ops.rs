#![allow(dead_code)]

use crate::models::{AheadBehind, DiffStat, ParsedWorktree};
use anyhow::{anyhow, Result};
use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn fmt_args(args: &[&str]) -> String {
    args.join(" ")
}

pub fn run(args: &[&str], cwd: Option<&Path>) -> Result<String> {
    let mut cmd = Command::new("git");
    cmd.args(args);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }

    let output = cmd.output()?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(anyhow!("git {}: {}", fmt_args(args), stderr));
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

pub fn try_run(args: &[&str], cwd: Option<&Path>) -> Option<String> {
    run(args, cwd).ok()
}

pub fn get_repo_root() -> Result<PathBuf> {
    let common_dir_raw = run(&["rev-parse", "--git-common-dir"], None)?;
    let common = PathBuf::from(common_dir_raw);
    let mut common_abs = if common.is_absolute() {
        common
    } else {
        std::env::current_dir()?.join(common)
    };

    common_abs = common_abs.canonicalize().unwrap_or(common_abs);

    if common_abs.file_name() == Some(OsStr::new(".git")) {
        if let Some(parent) = common_abs.parent() {
            return Ok(parent.to_path_buf());
        }
    }

    Ok(common_abs)
}

pub fn is_bare_repo(repo_root: &Path) -> Result<bool> {
    Ok(run(&["rev-parse", "--is-bare-repository"], Some(repo_root))? == "true")
}

pub fn get_default_branch(repo_root: &Path) -> String {
    if let Some(reference) = try_run(
        &[
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
        Some(repo_root),
    ) {
        if let Some((_, branch)) = reference.split_once('/') {
            return branch.to_string();
        }
    }
    "main".to_string()
}

pub fn prune_worktrees(repo_root: &Path) {
    let _ = try_run(&["worktree", "prune"], Some(repo_root));
}

pub fn parse_worktrees(repo_root: Option<&Path>) -> Result<Vec<ParsedWorktree>> {
    let output = run(&["worktree", "list", "--porcelain"], repo_root)?;
    let mut worktrees = Vec::new();

    let mut current_path = String::new();
    let mut current_branch = String::new();
    let mut current_head = String::new();
    let mut current_is_bare = false;

    for line in output.lines() {
        if let Some(path) = line.strip_prefix("worktree ") {
            if !current_path.is_empty()
                && !current_is_bare
                && (!current_branch.is_empty() || !current_head.is_empty())
            {
                worktrees.push(ParsedWorktree {
                    path: PathBuf::from(&current_path),
                    branch: current_branch.clone(),
                    head: current_head.clone(),
                });
            }
            current_path = path.to_string();
            current_branch.clear();
            current_head.clear();
            current_is_bare = false;
        } else if let Some(reference) = line.strip_prefix("branch ") {
            current_branch = reference.trim_start_matches("refs/heads/").to_string();
        } else if let Some(head) = line.strip_prefix("HEAD ") {
            current_head = head.to_string();
        } else if line.starts_with("detached") {
            current_branch = "(detached)".to_string();
        } else if line.starts_with("bare") {
            current_is_bare = true;
        }
    }

    if !current_path.is_empty()
        && !current_is_bare
        && (!current_branch.is_empty() || !current_head.is_empty())
    {
        worktrees.push(ParsedWorktree {
            path: PathBuf::from(current_path),
            branch: current_branch,
            head: current_head,
        });
    }

    Ok(worktrees)
}

pub fn count_ahead_behind(repo_root: &Path, left: &str, right: &str) -> AheadBehind {
    let range = format!("{left}...{right}");
    let Some(output) = try_run(
        &["rev-list", "--left-right", "--count", &range],
        Some(repo_root),
    ) else {
        return AheadBehind {
            ahead: 0,
            behind: 0,
        };
    };

    let mut parts = output.split_whitespace();
    let ahead = parts
        .next()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(0);
    let behind = parts
        .next()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(0);

    AheadBehind { ahead, behind }
}

pub fn diff_counts(worktree_path: &Path) -> DiffStat {
    if !worktree_path.is_dir() {
        return DiffStat {
            additions: 0,
            deletions: 0,
            dirty: false,
        };
    }

    let status = try_run(&["status", "--porcelain"], Some(worktree_path)).unwrap_or_default();
    let dirty = !status.trim().is_empty();

    let mut additions = 0_i64;
    let mut deletions = 0_i64;

    let numstat = try_run(&["diff", "--numstat"], Some(worktree_path)).unwrap_or_default();
    for line in numstat.lines() {
        let mut parts = line.split('\t');
        let a = parts.next().and_then(|v| v.parse::<i64>().ok());
        let d = parts.next().and_then(|v| v.parse::<i64>().ok());
        if let (Some(a), Some(d)) = (a, d) {
            additions += a;
            deletions += d;
        }
    }

    let untracked = status
        .lines()
        .filter(|line| line.starts_with("?? "))
        .count() as i64;
    additions += untracked;

    DiffStat {
        additions,
        deletions,
        dirty,
    }
}

pub fn get_last_commit_ts(repo_root: &Path, target: &str) -> i64 {
    try_run(&["log", "-1", "--format=%ct", target], Some(repo_root))
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(0)
}

pub fn get_upstream(repo_root: &Path, ref_name: &str) -> Option<String> {
    let arg = format!("{ref_name}@{{upstream}}");
    try_run(&["rev-parse", "--abbrev-ref", &arg], Some(repo_root))
}

pub fn list_local_branches(repo_root: &Path) -> Result<Vec<String>> {
    let out = run(
        &["for-each-ref", "--format=%(refname:short)", "refs/heads"],
        Some(repo_root),
    )?;
    Ok(out
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(ToOwned::to_owned)
        .collect())
}

pub fn branch_exists(repo_root: &Path, branch: &str) -> bool {
    let ref_name = format!("refs/heads/{branch}");
    try_run(&["show-ref", "--verify", &ref_name], Some(repo_root)).is_some()
}

pub fn remote_branch_exists(repo_root: &Path, branch: &str) -> bool {
    let out = try_run(&["ls-remote", "--heads", "origin", branch], Some(repo_root));
    out.is_some_and(|v| !v.trim().is_empty())
}

pub fn is_valid_branch_name(repo_root: &Path, name: &str) -> bool {
    try_run(&["check-ref-format", "--branch", name], Some(repo_root)).is_some()
}

pub fn has_unpushed_commits(repo_root: &Path, branch: &str) -> bool {
    let Some(upstream) = get_upstream(repo_root, branch) else {
        return true;
    };
    let ab = count_ahead_behind(repo_root, branch, &upstream);
    ab.ahead > 0
}

pub fn has_uncommitted_changes(repo_root: &Path) -> Result<bool> {
    Ok(!run(&["status", "--porcelain"], Some(repo_root))?
        .trim()
        .is_empty())
}

pub fn fetch_prune(repo_root: &Path) {
    let _ = try_run(&["fetch", "--prune"], Some(repo_root));
}

pub fn worktree_add(repo_root: &Path, path: &Path, branch: &str, base: Option<&str>) -> Result<()> {
    ensure_worktree_parent(path)?;
    let path_s = path.to_string_lossy().to_string();
    if let Some(base) = base {
        run(
            &["worktree", "add", "-b", branch, &path_s, base],
            Some(repo_root),
        )?;
    } else {
        run(&["worktree", "add", &path_s, branch], Some(repo_root))?;
    }
    Ok(())
}

pub fn worktree_remove(repo_root: &Path, path: &Path) -> Result<()> {
    let path_s = path.to_string_lossy().to_string();
    run(&["worktree", "remove", "--force", &path_s], Some(repo_root))?;
    Ok(())
}

pub fn worktree_move(repo_root: &Path, src: &Path, dest: &Path) -> Result<()> {
    ensure_worktree_parent(dest)?;
    let src_s = src.to_string_lossy().to_string();
    let dest_s = dest.to_string_lossy().to_string();
    run(&["worktree", "move", &src_s, &dest_s], Some(repo_root))?;
    Ok(())
}

pub fn branch_delete(repo_root: &Path, branch: &str) -> Result<()> {
    run(&["branch", "-D", branch], Some(repo_root))?;
    Ok(())
}

pub fn branch_rename(repo_root: &Path, old_name: &str, new_name: &str) -> Result<()> {
    run(&["branch", "-m", old_name, new_name], Some(repo_root))?;
    Ok(())
}

pub fn branch_set_upstream(repo_root: &Path, branch: &str, upstream: &str) -> Result<()> {
    run(
        &["branch", "--set-upstream-to", upstream, branch],
        Some(repo_root),
    )?;
    Ok(())
}

pub fn fetch_branch(repo_root: &Path, branch: &str) -> Result<()> {
    let spec = format!("{branch}:{branch}");
    run(&["fetch", "origin", &spec], Some(repo_root))?;
    Ok(())
}

pub fn pull(worktree_path: &Path) -> Result<()> {
    run(&["pull"], Some(worktree_path))?;
    Ok(())
}

pub fn push(worktree_path: &Path) -> Result<()> {
    run(&["push"], Some(worktree_path))?;
    Ok(())
}

pub fn push_set_upstream(worktree_path: &Path, branch: &str) -> Result<()> {
    run(&["push", "-u", "origin", branch], Some(worktree_path))?;
    Ok(())
}

pub fn set_bare(repo_root: &Path) -> Result<()> {
    run(&["config", "core.bare", "true"], Some(repo_root))?;
    Ok(())
}

pub fn ensure_worktree_parent(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    Ok(())
}

pub fn worktree_branch_map(repo_root: &Path) -> Result<HashMap<String, PathBuf>> {
    let mut mapping = HashMap::new();
    for wt in parse_worktrees(Some(repo_root))? {
        if !wt.branch.is_empty() && wt.branch != "(detached)" {
            mapping.insert(wt.branch, wt.path);
        }
    }
    Ok(mapping)
}

pub fn get_entries_to_preserve(
    repo_root: &Path,
    worktree_paths: &[PathBuf],
) -> Result<Vec<String>> {
    let mut keep = vec![".git".to_string(), ".gw".to_string()];
    let repo_abs = repo_root
        .canonicalize()
        .unwrap_or_else(|_| repo_root.to_path_buf());

    for path in worktree_paths {
        let abs_path = path.canonicalize().unwrap_or_else(|_| path.clone());
        if abs_path == repo_abs {
            continue;
        }
        if !abs_path.starts_with(&repo_abs) {
            continue;
        }

        if let Ok(rel) = abs_path.strip_prefix(&repo_abs) {
            if let Some(first) = rel.components().next() {
                let entry = first.as_os_str().to_string_lossy().to_string();
                if !entry.is_empty() && !keep.contains(&entry) {
                    keep.push(entry);
                }
            }
        }
    }

    keep.sort();
    Ok(keep)
}
