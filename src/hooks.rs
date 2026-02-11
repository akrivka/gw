use anyhow::{anyhow, Context, Result};
use serde_json::{json, Value};
use std::fs;
use std::path::Path;
use std::process::Command;

fn settings_path(repo_root: &Path) -> std::path::PathBuf {
    repo_root.join(".gw").join("settings.json")
}

fn load_settings(repo_root: &Path) -> Result<Value> {
    let path = settings_path(repo_root);
    if !path.exists() {
        return Ok(json!({}));
    }

    let text =
        fs::read_to_string(&path).with_context(|| format!("failed to read {}", path.display()))?;
    let raw: Value = serde_json::from_str(&text)
        .with_context(|| format!("invalid JSON in {}", path.display()))?;
    if !raw.is_object() {
        return Err(anyhow!("invalid settings format in {}", path.display()));
    }
    Ok(raw)
}

fn save_settings(repo_root: &Path, settings: &Value) -> Result<()> {
    let path = settings_path(repo_root);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut text = serde_json::to_string_pretty(settings)?;
    text.push('\n');
    fs::write(&path, text).with_context(|| format!("failed to write {}", path.display()))?;
    Ok(())
}

pub fn add_post_worktree_creation_hook(repo_root: &Path, command: &str) -> Result<()> {
    let normalized = command.trim();
    if normalized.is_empty() {
        return Err(anyhow!("hook command cannot be empty"));
    }

    let mut settings = load_settings(repo_root)?;
    let Some(settings_obj) = settings.as_object_mut() else {
        return Err(anyhow!("invalid settings object"));
    };

    if !settings_obj.contains_key("hooks") {
        settings_obj.insert("hooks".to_string(), json!({}));
    }

    let hooks = settings_obj
        .get_mut("hooks")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| anyhow!("invalid hooks section in settings"))?;

    if !hooks.contains_key("PostWorktreeCreation") {
        hooks.insert("PostWorktreeCreation".to_string(), json!([]));
    }

    let entries = hooks
        .get_mut("PostWorktreeCreation")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| anyhow!("invalid PostWorktreeCreation section in settings"))?;

    entries.push(json!({
        "type": "command",
        "command": normalized,
    }));

    save_settings(repo_root, &settings)
}

pub fn get_post_worktree_creation_commands(repo_root: &Path) -> Result<Vec<String>> {
    let settings = load_settings(repo_root)?;
    let Some(hooks) = settings.get("hooks") else {
        return Ok(Vec::new());
    };
    let Some(hooks_obj) = hooks.as_object() else {
        return Err(anyhow!("invalid hooks section in settings"));
    };
    let Some(entries) = hooks_obj.get("PostWorktreeCreation") else {
        return Ok(Vec::new());
    };
    let Some(entries) = entries.as_array() else {
        return Err(anyhow!("invalid PostWorktreeCreation section in settings"));
    };

    let mut commands = Vec::new();
    for entry in entries {
        let Some(obj) = entry.as_object() else {
            continue;
        };
        let is_command = obj.get("type").and_then(Value::as_str) == Some("command");
        if !is_command {
            continue;
        }
        if let Some(command) = obj.get("command").and_then(Value::as_str) {
            let normalized = command.trim();
            if !normalized.is_empty() {
                commands.push(normalized.to_string());
            }
        }
    }

    Ok(commands)
}

pub fn run_post_worktree_creation_hooks(repo_root: &Path, cwd: Option<&Path>) -> Result<()> {
    let run_cwd = cwd.unwrap_or(repo_root);
    for command in get_post_worktree_creation_commands(repo_root)? {
        #[cfg(unix)]
        let output = Command::new("sh")
            .arg("-c")
            .arg(&command)
            .current_dir(run_cwd)
            .output()
            .with_context(|| format!("failed to run hook `{command}`"))?;

        #[cfg(windows)]
        let output = Command::new("cmd")
            .arg("/C")
            .arg(&command)
            .current_dir(run_cwd)
            .output()
            .with_context(|| format!("failed to run hook `{command}`"))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
            let msg = if !stderr.is_empty() {
                stderr
            } else if !stdout.is_empty() {
                stdout
            } else {
                "unknown error".to_string()
            };
            return Err(anyhow!("hook failed: `{command}`: {msg}"));
        }
    }

    Ok(())
}
