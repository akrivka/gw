#![allow(dead_code)]

use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct WorktreeInfo {
    pub path: PathBuf,
    pub branch: String,
    pub head: String,
    pub ref_name: Option<String>,
    pub cache_key: String,
    pub last_commit_ts: i64,
    pub pull: i64,
    pub push: i64,
    pub pull_push_validated: bool,
    pub has_upstream: bool,
    pub behind: i64,
    pub ahead: i64,
    pub additions: i64,
    pub deletions: i64,
    pub dirty: bool,
    pub pr_number: Option<i64>,
    pub pr_state: Option<String>,
    pub pr_base: Option<String>,
    pub pr_url: Option<String>,
    pub pr_validated: bool,
    pub checks_passed: Option<i64>,
    pub checks_total: Option<i64>,
    pub checks_state: Option<String>,
    pub checks_validated: bool,
    pub changes_validated: bool,
}

impl WorktreeInfo {
    pub fn is_detached(&self) -> bool {
        self.ref_name.is_none()
    }
}

#[derive(Debug, Clone, Copy)]
pub struct AheadBehind {
    pub ahead: i64,
    pub behind: i64,
}

#[derive(Debug, Clone, Copy)]
pub struct DiffStat {
    pub additions: i64,
    pub deletions: i64,
    pub dirty: bool,
}

#[derive(Debug, Clone)]
pub struct ParsedWorktree {
    pub path: PathBuf,
    pub branch: String,
    pub head: String,
}

#[derive(Debug, Clone)]
pub struct PullRequestInfo {
    pub number: i64,
    pub state: String,
    pub base: Option<String>,
    pub url: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ChecksInfo {
    pub passed: i64,
    pub total: i64,
    pub state: Option<String>,
}

#[derive(Debug, Clone)]
pub struct HealthReport {
    pub missing_worktrees: Vec<String>,
    pub orphaned_worktrees: Vec<PathBuf>,
    pub unrecoverable_reasons: Vec<String>,
}

impl HealthReport {
    pub fn has_issues(&self) -> bool {
        !self.missing_worktrees.is_empty()
            || !self.orphaned_worktrees.is_empty()
            || !self.unrecoverable_reasons.is_empty()
    }

    pub fn is_recoverable(&self) -> bool {
        self.unrecoverable_reasons.is_empty()
    }
}
