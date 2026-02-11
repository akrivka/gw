use crate::cache_db::CacheDB;
use crate::models::WorktreeInfo;
use crate::{gh_ops, git_ops};
use anyhow::Result;
use std::path::Path;

pub fn make_cache_key(branch: &str, head: &str) -> String {
    if !branch.is_empty() && branch != "(detached)" {
        branch.to_string()
    } else {
        format!("detached:{head}")
    }
}

pub fn load_worktrees(repo_root: &Path) -> Result<Vec<WorktreeInfo>> {
    let default_branch = git_ops::get_default_branch(repo_root);
    let db = CacheDB::open(repo_root)?;

    let mut items = Vec::new();
    for wt in git_ops::parse_worktrees(Some(repo_root))? {
        if !wt.path.is_dir() {
            continue;
        }

        let ref_name = if wt.branch.is_empty() || wt.branch == "(detached)" {
            None
        } else {
            Some(wt.branch.clone())
        };

        let target = ref_name.as_deref().unwrap_or(&wt.head);
        let last_commit_ts = git_ops::get_last_commit_ts(repo_root, target);

        let upstream = ref_name
            .as_deref()
            .and_then(|name| git_ops::get_upstream(repo_root, name));

        let (pull, push, has_upstream) =
            if let (Some(ref_name), Some(upstream)) = (ref_name.as_deref(), upstream.as_deref()) {
                let ab = git_ops::count_ahead_behind(repo_root, ref_name, upstream);
                (ab.behind, ab.ahead, true)
            } else {
                (0, 0, false)
            };

        let ab = git_ops::count_ahead_behind(repo_root, target, &default_branch);
        let cache_key = make_cache_key(&wt.branch, &wt.head);
        let cached = db.get_cached_worktree(&cache_key)?;

        let (
            pr_number,
            pr_state,
            pr_base,
            pr_url,
            checks_passed,
            checks_total,
            checks_state,
            additions,
            deletions,
            dirty,
        ) = if let Some(cached) = cached {
            (
                cached.pr_number,
                cached.pr_state,
                cached.pr_base,
                cached.pr_url,
                cached.checks_passed,
                cached.checks_total,
                cached.checks_state,
                cached.additions,
                cached.deletions,
                cached.dirty,
            )
        } else {
            (None, None, None, None, None, None, None, 0, 0, false)
        };

        db.upsert_path(&cache_key, &wt.path)?;

        items.push(WorktreeInfo {
            path: wt.path,
            branch: if wt.branch.is_empty() {
                wt.head.clone()
            } else {
                wt.branch.clone()
            },
            head: wt.head,
            ref_name,
            cache_key,
            last_commit_ts,
            pull,
            push,
            pull_push_validated: false,
            has_upstream,
            behind: ab.behind,
            ahead: ab.ahead,
            additions,
            deletions,
            dirty,
            pr_number,
            pr_state,
            pr_base,
            pr_url,
            pr_validated: false,
            checks_passed,
            checks_total,
            checks_state,
            checks_validated: false,
            changes_validated: false,
        });
    }

    items.sort_by(|a, b| b.last_commit_ts.cmp(&a.last_commit_ts));
    Ok(items)
}

pub fn refresh_pull_push(repo_root: &Path, items: &mut [WorktreeInfo]) -> Result<()> {
    git_ops::fetch_prune(repo_root);
    let db = CacheDB::open(repo_root)?;

    for item in items {
        if item.ref_name.is_none() {
            item.pull = 0;
            item.push = 0;
            item.has_upstream = false;
            item.pull_push_validated = true;
            continue;
        }

        let ref_name = item.ref_name.as_deref().unwrap_or_default();
        let upstream = git_ops::get_upstream(repo_root, ref_name);
        if let Some(upstream) = upstream {
            let ab = git_ops::count_ahead_behind(repo_root, ref_name, &upstream);
            item.pull = ab.behind;
            item.push = ab.ahead;
            item.has_upstream = true;
        } else {
            item.pull = 0;
            item.push = 0;
            item.has_upstream = false;
        }

        item.pull_push_validated = true;
        db.upsert_pull_push(&item.cache_key, &item.path, item.pull, item.push)?;
    }

    Ok(())
}

pub fn refresh_changes(repo_root: &Path, items: &mut [WorktreeInfo]) -> Result<()> {
    let db = CacheDB::open(repo_root)?;

    for item in items {
        if !item.path.is_dir() {
            continue;
        }

        let stats = git_ops::diff_counts(&item.path);
        item.additions = stats.additions;
        item.deletions = stats.deletions;
        item.dirty = stats.dirty;
        item.changes_validated = true;

        db.upsert_changes(
            &item.cache_key,
            &item.path,
            stats.additions,
            stats.deletions,
            stats.dirty,
        )?;
    }

    Ok(())
}

pub fn refresh_github(repo_root: &Path, items: &mut [WorktreeInfo]) -> Result<()> {
    let db = CacheDB::open(repo_root)?;

    for item in items {
        let Some(ref_name) = item.ref_name.as_deref() else {
            item.pr_number = None;
            item.pr_state = None;
            item.pr_base = None;
            item.pr_url = None;
            item.pr_validated = true;
            item.checks_passed = None;
            item.checks_total = None;
            item.checks_state = None;
            item.checks_validated = true;
            continue;
        };

        let pr_info = gh_ops::get_pr_info(repo_root, ref_name);
        let Some(pr_info) = pr_info else {
            item.pr_number = None;
            item.pr_state = None;
            item.pr_base = None;
            item.pr_url = None;
            item.pr_validated = true;
            item.checks_passed = None;
            item.checks_total = None;
            item.checks_state = None;
            item.checks_validated = true;

            db.upsert_pr_and_checks(
                &item.cache_key,
                &item.path,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )?;
            continue;
        };

        let checks_info = gh_ops::get_checks_info(repo_root, pr_info.number);

        item.pr_number = Some(pr_info.number);
        item.pr_state = Some(pr_info.state.clone());
        item.pr_base = pr_info.base.clone();
        item.pr_url = pr_info.url.clone();
        item.pr_validated = true;
        item.checks_passed = checks_info.as_ref().map(|c| c.passed);
        item.checks_total = checks_info.as_ref().map(|c| c.total);
        item.checks_state = checks_info.as_ref().and_then(|c| c.state.clone());
        item.checks_validated = true;

        db.upsert_pr_and_checks(
            &item.cache_key,
            &item.path,
            Some(pr_info.number),
            Some(&pr_info.state),
            pr_info.base.as_deref(),
            pr_info.url.as_deref(),
            checks_info.as_ref().map(|c| c.passed),
            checks_info.as_ref().map(|c| c.total),
            checks_info.as_ref().and_then(|c| c.state.as_deref()),
        )?;
    }

    Ok(())
}

pub fn refresh_from_upstream(
    repo_root: &Path,
    items: &mut [WorktreeInfo],
    gh_available: bool,
) -> Result<()> {
    refresh_pull_push(repo_root, items)?;
    refresh_changes(repo_root, items)?;

    if gh_available {
        refresh_github(repo_root, items)?;
    }

    Ok(())
}
