use crate::models::{ChecksInfo, PullRequestInfo};
use serde::Deserialize;
use serde_json::Value;
use std::path::Path;
use std::process::Command;

#[derive(Debug, Deserialize)]
struct PrListItem {
    number: i64,
    state: Option<String>,
    #[serde(rename = "baseRefName")]
    base_ref_name: Option<String>,
    #[serde(rename = "mergedAt")]
    merged_at: Option<String>,
    url: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ChecksView {
    #[serde(rename = "statusCheckRollup")]
    status_check_rollup: Option<Vec<Value>>,
}

fn run_gh(args: &[&str], repo_root: &Path) -> Option<String> {
    let output = Command::new("gh")
        .args(args)
        .current_dir(repo_root)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

pub fn get_pr_info(repo_root: &Path, branch: &str) -> Option<PullRequestInfo> {
    let stdout = run_gh(
        &[
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
        repo_root,
    )?;

    let list: Vec<PrListItem> = serde_json::from_str(&stdout).ok()?;
    let first = list.into_iter().next()?;

    let state = if first.merged_at.is_some() {
        "MERGED".to_string()
    } else {
        first.state.unwrap_or_else(|| "OPEN".to_string())
    };

    Some(PullRequestInfo {
        number: first.number,
        state,
        base: first.base_ref_name,
        url: first.url,
    })
}

pub fn get_checks_info(repo_root: &Path, pr_number: i64) -> Option<ChecksInfo> {
    let stdout = run_gh(
        &[
            "pr",
            "view",
            &pr_number.to_string(),
            "--json",
            "statusCheckRollup",
        ],
        repo_root,
    )?;

    let parsed: ChecksView = serde_json::from_str(&stdout).ok()?;
    let rollup = parsed.status_check_rollup.unwrap_or_default();

    let conclusions: Vec<Option<String>> = rollup
        .iter()
        .map(|item| {
            item.get("conclusion")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned)
        })
        .collect();
    let states: Vec<Option<String>> = rollup
        .iter()
        .map(|item| {
            item.get("state")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned)
        })
        .collect();

    Some(classify_checks(&conclusions, &states))
}

pub fn classify_checks(conclusions: &[Option<String>], states: &[Option<String>]) -> ChecksInfo {
    let total = conclusions.len() as i64;
    let mut passed = 0_i64;
    let mut failed = false;
    let mut pending = false;

    for (conclusion, state) in conclusions.iter().zip(states.iter()) {
        if state.as_deref().is_some_and(|v| v != "COMPLETED") {
            pending = true;
        }

        match conclusion.as_deref() {
            Some("SUCCESS") | Some("NEUTRAL") | Some("SKIPPED") => passed += 1,
            Some(_) => failed = true,
            None => pending = true,
        }
    }

    let status = if total == 0 {
        None
    } else if failed {
        Some("fail".to_string())
    } else if pending {
        Some("pend".to_string())
    } else {
        Some("ok".to_string())
    };

    ChecksInfo {
        passed,
        total,
        state: status,
    }
}
