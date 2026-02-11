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

    git_ops::prune_worktrees(&repo_root);
    let default_branch = git_ops::get_default_branch(&repo_root);
    let items = services::load_worktrees(&repo_root)?;

    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
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

    git_ops::set_bare(&repo_root)?;
    clear_repo_root(&repo_root, &preserved_with_git(preserved))?;

    for branch in &missing {
        let target = repo_root.join(branch);
        git_ops::worktree_add(&repo_root, &target, branch, None)?;
    }

    println!("gw init: done");
    Ok(())
}

fn preserved_with_git(mut keep: Vec<String>) -> HashSet<String> {
    keep.push(".git".to_string());
    keep.push(".gw".to_string());
    keep.into_iter().collect()
}

fn clear_repo_root(repo_root: &Path, keep_entries: &HashSet<String>) -> Result<()> {
    for entry in fs::read_dir(repo_root)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        if keep_entries.contains(&name) {
            continue;
        }

        let path = entry.path();
        if path.is_symlink() || path.is_file() {
            let _ = fs::remove_file(&path);
        } else if path.is_dir() {
            let _ = fs::remove_dir_all(&path);
        }
    }

    Ok(())
}

fn shell_init() -> Result<()> {
    let bash_zsh = r#"gw() {
  local tmp dest
  tmp="$(mktemp)" || return $?
  GW_OUTPUT_FILE="$tmp" command gw "$@" </dev/tty >/dev/tty
  dest="$(cat "$tmp" 2>/dev/null)"
  rm -f "$tmp"
  if [ -n "$dest" ]; then
    cd "$dest" || return $?
  fi
}
"#;

    let fish = r#"function gw
  set -l tmp (mktemp)
  if test -z "$tmp"
    return 1
  end
  env GW_OUTPUT_FILE=$tmp command gw $argv </dev/tty >/dev/tty
  set -l dest (cat $tmp 2>/dev/null)
  rm -f $tmp
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
    print!("{prompt} [y/N]: ");
    io::stdout().flush()?;

    let mut buf = String::new();
    io::stdin().read_line(&mut buf)?;
    let normalized = buf.trim().to_ascii_lowercase();
    Ok(matches!(normalized.as_str(), "y" | "yes"))
}
