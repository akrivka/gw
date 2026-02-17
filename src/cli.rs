use crate::models::HealthReport;
use crate::{git_ops, hooks, services, tui};
use anyhow::{anyhow, Context, Result};
use clap::{Args, Parser, Subcommand};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};

#[derive(Debug, Parser)]
#[command(name = "gw", version, about = "Interactive git worktree manager")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Option<Commands>,
}

#[derive(Debug, Subcommand)]
pub enum Commands {
    Init,
    #[command(name = "shell-init")]
    ShellInit,
    Hooks(HooksArgs),
}

#[derive(Debug, Args)]
pub struct HooksArgs {
    #[command(subcommand)]
    pub command: HooksSubcommands,
}

#[derive(Debug, Subcommand)]
pub enum HooksSubcommands {
    Add { command: String },
    Rerun,
}

pub fn run() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Some(Commands::Init) => init_repo(),
        Some(Commands::ShellInit) => shell_init(),
        Some(Commands::Hooks(hooks_args)) => match hooks_args.command {
            HooksSubcommands::Add { command } => add_hook(&command),
            HooksSubcommands::Rerun => rerun_hooks(),
        },
        None => run_default(),
    }
}

fn run_default() -> Result<()> {
    let repo_root = git_ops::get_repo_root().context("gw: not inside a git repository")?;
    let interactive = io::stdin().is_terminal() && io::stderr().is_terminal();

    git_ops::prune_worktrees(&repo_root);
    let health = services::health_check(&repo_root)?;
    if health.has_issues() {
        if !interactive {
            return Err(anyhow!(
                "gw: detected worktree/branch inconsistencies; rerun in an interactive terminal to repair them, or run `gw init`"
            ));
        }
        if !handle_health_issues(&repo_root, &health)? {
            return Ok(());
        }
    }

    let default_branch = git_ops::get_default_branch(&repo_root);
    let items = services::load_worktrees(&repo_root)?;

    if !interactive {
        for item in &items {
            println!("{}", item.path.display());
        }
        return Ok(());
    }

    let gh_available = command_available("gh");
    let warning =
        (!gh_available).then(|| "gh not found: install/configure gh for PR data".to_string());

    let selected = tui::run_tui(
        repo_root.clone(),
        items,
        default_branch,
        warning,
        gh_available,
    )?;
    if let Some(path) = selected {
        tui::write_selected_path(&path)?;
    }

    Ok(())
}

fn handle_health_issues(repo_root: &Path, health: &HealthReport) -> Result<bool> {
    eprintln!("Detected issue with gw setup in {}", repo_root.display());
    eprintln!();

    if !health.orphaned_worktrees.is_empty() {
        eprintln!(
            "- worktrees without branches to delete: {}",
            health.orphaned_worktrees.len()
        );
        for path in &health.orphaned_worktrees {
            eprintln!("  - {}", path.display());
        }
    }

    if !health.missing_worktrees.is_empty() {
        eprintln!(
            "- branches without worktrees to create: {}",
            health.missing_worktrees.len()
        );
        for branch in &health.missing_worktrees {
            eprintln!("  - {branch} -> {}", repo_root.join(branch).display());
        }
    }

    if !health.unrecoverable_reasons.is_empty() {
        eprintln!("- unrecoverable issues:");
        for reason in &health.unrecoverable_reasons {
            eprintln!("  - {reason}");
        }
        return Err(anyhow!(
            "gw: setup is not recoverable automatically; run `gw init` first"
        ));
    }

    eprintln!();
    if !confirm("Apply these fixes now?")? {
        eprintln!("gw: cancelled");
        return Ok(false);
    }

    services::doctor_repo(repo_root, health)?;
    eprintln!("gw: setup repaired");
    Ok(true)
}

fn command_available(cmd: &str) -> bool {
    std::process::Command::new(cmd)
        .arg("--version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn init_repo() -> Result<()> {
    let repo_root = git_ops::get_repo_root().context("gw init: not inside a git repository")?;
    let is_bare = git_ops::is_bare_repo(&repo_root)?;
    let branches = git_ops::list_local_branches(&repo_root)?;

    if branches.is_empty() {
        return Err(anyhow!("gw init: no local branches found"));
    }

    let worktree_map = git_ops::worktree_branch_map(&repo_root)?;

    let get_conflicting_paths = |branches_to_add: &[String], map: &HashMap<String, PathBuf>| {
        let mut conflicts = Vec::new();
        for branch in branches_to_add {
            let target = repo_root.join(branch);
            if target.exists() && !map.contains_key(branch) {
                conflicts.push(branch.clone());
            }
        }
        conflicts
    };

    if is_bare {
        let missing: Vec<String> = branches
            .iter()
            .filter(|branch| !worktree_map.contains_key(*branch))
            .cloned()
            .collect();
        let conflicts = get_conflicting_paths(&missing, &worktree_map);

        if !conflicts.is_empty() {
            return Err(anyhow!(
                "gw init: cannot create worktrees; paths already exist: {}",
                conflicts.join(", ")
            ));
        }

        println!(
            "gw init will initialize worktrees under {}",
            repo_root.display()
        );
        if missing.is_empty() {
            println!("- no new worktrees to create");
        } else {
            println!("- create worktrees for {} local branches", missing.len());
        }

        if !confirm("Continue?")? {
            println!("gw init: cancelled");
            return Ok(());
        }

        for branch in &missing {
            let target = repo_root.join(branch);
            git_ops::worktree_add(&repo_root, &target, branch, None)?;
        }

        println!("gw init: done");
        return Ok(());
    }

    if git_ops::has_uncommitted_changes(&repo_root)? {
        return Err(anyhow!(
            "gw init: working tree has uncommitted or untracked changes"
        ));
    }

    let repo_abs = repo_root
        .canonicalize()
        .unwrap_or_else(|_| repo_root.clone());
    let root_branches: HashSet<String> = worktree_map
        .iter()
        .filter_map(|(branch, path)| {
            let path_abs = path.canonicalize().unwrap_or_else(|_| path.clone());
            if path_abs == repo_abs {
                Some(branch.clone())
            } else {
                None
            }
        })
        .collect();

    let missing: Vec<String> = branches
        .iter()
        .filter(|branch| !worktree_map.contains_key(*branch) || root_branches.contains(*branch))
        .cloned()
        .collect();

    let worktree_paths: Vec<PathBuf> = git_ops::parse_worktrees(Some(&repo_root))?
        .into_iter()
        .map(|wt| wt.path)
        .collect();

    let keep_entries = git_ops::get_entries_to_preserve(&repo_root, &worktree_paths)?;
    let conflicts = get_conflicting_paths(&missing, &worktree_map);

    if !conflicts.is_empty() {
        return Err(anyhow!(
            "gw init: cannot create worktrees; paths already exist: {}",
            conflicts.join(", ")
        ));
    }

    println!(
        "gw init will convert {} into a gw-compliant layout:",
        repo_root.display()
    );
    println!("- delete the current working tree at the repo root");
    println!("- keep only the bare repo in the top-level .git directory");
    println!("- ensure every local branch has a worktree");

    if !missing.is_empty() {
        println!(
            "- create {} new worktrees under {}/<branch>",
            missing.len(),
            repo_root.display()
        );
    }

    let preserved: Vec<String> = keep_entries
        .into_iter()
        .filter(|entry| entry != ".git")
        .collect();
    if !preserved.is_empty() {
        println!(
            "- preserve existing worktree paths: {}",
            preserved.join(", ")
        );
    }

    if !confirm("Continue?")? {
        println!("gw init: cancelled");
        return Ok(());
    }

    let keep_entries = preserved_with_git(preserved);
    convert_repo_with_rollback(&repo_root, &keep_entries, &missing)?;

    println!("gw init: done");
    Ok(())
}

fn preserved_with_git(mut keep: Vec<String>) -> HashSet<String> {
    keep.push(".git".to_string());
    keep.push(".gw".to_string());
    keep.into_iter().collect()
}

#[derive(Debug)]
struct StagedEntry {
    original: PathBuf,
    backup: PathBuf,
}

fn convert_repo_with_rollback(
    repo_root: &Path,
    keep_entries: &HashSet<String>,
    missing_branches: &[String],
) -> Result<()> {
    let backup_dir = create_backup_dir(repo_root)?;
    let mut tx = InitConversionTx {
        repo_root: repo_root.to_path_buf(),
        backup_dir,
        staged_entries: Vec::new(),
        created_worktrees: Vec::new(),
        bare_changed: false,
    };

    let mut stage_keep = keep_entries.clone();
    if let Some(name) = tx.backup_dir.file_name() {
        stage_keep.insert(name.to_string_lossy().to_string());
    }
    preflight_worktree_targets(repo_root, missing_branches)?;

    let convert_result = (|| -> Result<()> {
        tx.staged_entries = stage_repo_root(repo_root, &stage_keep, &tx.backup_dir)?;
        git_ops::set_bare(repo_root)?;
        tx.bare_changed = true;

        for branch in missing_branches {
            let target = repo_root.join(branch);
            git_ops::worktree_add(repo_root, &target, branch, None)
                .with_context(|| format!("gw init: failed to create worktree for {branch}"))?;
            tx.created_worktrees.push(target);
        }

        postcheck_worktrees(repo_root, missing_branches)?;
        Ok(())
    })();

    match convert_result {
        Ok(()) => {
            if let Err(err) = fs::remove_dir_all(&tx.backup_dir) {
                eprintln!(
                    "gw init: warning: conversion succeeded, but failed to remove backup {}: {err}",
                    tx.backup_dir.display()
                );
            }
            Ok(())
        }
        Err(err) => {
            let rollback_errors = rollback_conversion(&tx);
            if rollback_errors.is_empty() {
                Err(err)
            } else {
                Err(anyhow!(
                    "{err}\ngw init: rollback encountered errors:\n{}",
                    rollback_errors.join("\n")
                ))
            }
        }
    }
}

struct InitConversionTx {
    repo_root: PathBuf,
    backup_dir: PathBuf,
    staged_entries: Vec<StagedEntry>,
    created_worktrees: Vec<PathBuf>,
    bare_changed: bool,
}

fn preflight_worktree_targets(repo_root: &Path, missing_branches: &[String]) -> Result<()> {
    for branch in missing_branches {
        let target = repo_root.join(branch);
        if target.exists() {
            return Err(anyhow!(
                "gw init: cannot create worktree for {branch}; target path already exists: {}",
                target.display()
            ));
        }
        target.parent().ok_or_else(|| {
            anyhow!(
                "gw init: invalid worktree target for {branch}: {}",
                target.display()
            )
        })?;
    }
    Ok(())
}

fn create_backup_dir(repo_root: &Path) -> Result<PathBuf> {
    let pid = std::process::id();
    for attempt in 0..50 {
        let candidate = repo_root.join(format!(".gw-init-backup-{pid}-{attempt}"));
        if !candidate.exists() {
            fs::create_dir(&candidate).with_context(|| {
                format!(
                    "gw init: failed to create backup directory {}",
                    candidate.display()
                )
            })?;
            return Ok(candidate);
        }
    }
    Err(anyhow!(
        "gw init: failed to allocate a unique backup directory under {}",
        repo_root.display()
    ))
}

fn stage_repo_root(
    repo_root: &Path,
    keep_entries: &HashSet<String>,
    backup_dir: &Path,
) -> Result<Vec<StagedEntry>> {
    let mut staged = Vec::new();
    for entry in fs::read_dir(repo_root)
        .with_context(|| format!("gw init: failed to list {}", repo_root.display()))?
    {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        if keep_entries.contains(&name) {
            continue;
        }
        let source = entry.path();
        let backup = backup_dir.join(&name);
        fs::rename(&source, &backup).with_context(|| {
            format!(
                "gw init: failed to stage {} into backup {}",
                source.display(),
                backup.display()
            )
        })?;
        staged.push(StagedEntry {
            original: source,
            backup,
        });
    }
    Ok(staged)
}

fn postcheck_worktrees(repo_root: &Path, missing_branches: &[String]) -> Result<()> {
    let map = git_ops::worktree_branch_map(repo_root)?;
    for branch in missing_branches {
        if !map.contains_key(branch) {
            return Err(anyhow!(
                "gw init: post-check failed; worktree for branch {branch} was not registered"
            ));
        }
    }
    Ok(())
}

fn rollback_conversion(tx: &InitConversionTx) -> Vec<String> {
    let mut errors = Vec::new();

    for path in tx.created_worktrees.iter().rev() {
        if let Err(err) = git_ops::worktree_remove(&tx.repo_root, path) {
            errors.push(format!(
                "- failed to remove created worktree {}: {err}",
                path.display()
            ));
        }
    }

    if tx.bare_changed {
        if let Err(err) = git_ops::run(&["config", "core.bare", "false"], Some(&tx.repo_root)) {
            errors.push(format!("- failed to restore core.bare=false: {err}"));
        }
    }

    for entry in tx.staged_entries.iter().rev() {
        if let Err(err) = fs::rename(&entry.backup, &entry.original) {
            errors.push(format!(
                "- failed to restore {} from backup {}: {err}",
                entry.original.display(),
                entry.backup.display()
            ));
        }
    }

    if let Err(err) = fs::remove_dir_all(&tx.backup_dir) {
        errors.push(format!(
            "- failed to remove backup directory {}: {err}",
            tx.backup_dir.display()
        ));
    }

    errors
}

fn shell_init() -> Result<()> {
    let bash_zsh = r#"gw() {
  local dest
  dest="$(command gw "$@" </dev/tty)" || return $?
  if [ -n "$dest" ]; then
    cd "$dest" || return $?
  fi
}
"#;

    let fish = r#"function gw
  set -l dest (command gw $argv | string collect)
  set -l gw_status $status
  if test $gw_status -ne 0
    return $gw_status
  end
  if test -n "$dest"
    cd "$dest"
  end
end
"#;

    println!("# bash/zsh\n{bash_zsh}\n# fish\n{fish}");
    Ok(())
}

fn add_hook(command: &str) -> Result<()> {
    let repo_root =
        git_ops::get_repo_root().context("gw hooks add: not inside a git repository")?;
    hooks::add_post_worktree_creation_hook(&repo_root, command)?;
    println!("gw hooks add: hook added");
    Ok(())
}

fn rerun_hooks() -> Result<()> {
    let repo_root =
        git_ops::get_repo_root().context("gw hooks rerun: not inside a git repository")?;

    let cwd = std::env::current_dir()?;
    let worktree_root_raw = git_ops::run(&["rev-parse", "--show-toplevel"], Some(&cwd))
        .context("gw hooks rerun: not inside a git worktree")?;
    let worktree_root = PathBuf::from(worktree_root_raw)
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from("."));

    hooks::run_post_worktree_creation_hooks(&repo_root, Some(&worktree_root))?;
    println!(
        "gw hooks rerun: hooks executed in {}",
        worktree_root.display()
    );
    Ok(())
}

fn confirm(prompt: &str) -> Result<bool> {
    eprint!("{prompt} [y/N]: ");
    io::stderr().flush()?;

    let mut buf = String::new();
    io::stdin().read_line(&mut buf)?;
    let normalized = buf.trim().to_ascii_lowercase();
    Ok(matches!(normalized.as_str(), "y" | "yes"))
}
