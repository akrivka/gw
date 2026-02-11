use anyhow::Result;
use rusqlite::{params, Connection};
use sha1::{Digest, Sha1};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone)]
pub struct CachedWorktree {
    pub pr_number: Option<i64>,
    pub pr_state: Option<String>,
    pub pr_base: Option<String>,
    pub pr_url: Option<String>,
    pub checks_passed: Option<i64>,
    pub checks_total: Option<i64>,
    pub checks_state: Option<String>,
    pub additions: i64,
    pub deletions: i64,
    pub dirty: bool,
}

fn db_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

fn now_ts() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn get_cache_dir() -> Result<PathBuf> {
    let home = dirs::home_dir().ok_or_else(|| anyhow::anyhow!("cannot resolve home directory"))?;
    let dir = home.join(".cache").join("gw");
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

fn get_db_path(repo_root: &Path) -> Result<PathBuf> {
    let mut hasher = Sha1::new();
    hasher.update(repo_root.to_string_lossy().as_bytes());
    let digest = hasher.finalize();
    let repo_id = format!("{:x}", digest);
    Ok(get_cache_dir()?.join(format!("{repo_id}.sqlite")))
}

fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS worktree_cache (
          branch TEXT PRIMARY KEY,
          path TEXT NOT NULL,
          pr_number INTEGER,
          pr_state TEXT,
          pr_base TEXT,
          pr_url TEXT,
          pr_updated_at INTEGER,
          checks_passed INTEGER,
          checks_total INTEGER,
          checks_state TEXT,
          checks_updated_at INTEGER,
          additions INTEGER,
          deletions INTEGER,
          dirty INTEGER,
          changes_updated_at INTEGER,
          pull INTEGER,
          push INTEGER,
          pullpush_validated_at INTEGER
        );
        "#,
    )?;
    Ok(())
}

pub struct CacheDB {
    conn: Connection,
}

impl CacheDB {
    pub fn open(repo_root: &Path) -> Result<Self> {
        let _guard = db_lock().lock().expect("cache lock poisoned");
        let db_path = get_db_path(repo_root)?;
        let conn = Connection::open(db_path)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")?;
        ensure_schema(&conn)?;
        drop(_guard);
        Ok(Self { conn })
    }

    pub fn get_cached_worktree(&self, cache_key: &str) -> Result<Option<CachedWorktree>> {
        let _guard = db_lock().lock().expect("cache lock poisoned");

        let mut stmt = self.conn.prepare(
            r#"
            SELECT
              pr_number, pr_state, pr_base, pr_url,
              checks_passed, checks_total, checks_state,
              additions, deletions, dirty
            FROM worktree_cache
            WHERE branch = ?
            "#,
        )?;

        let row = stmt.query_row(params![cache_key], |row| {
            Ok(CachedWorktree {
                pr_number: row.get(0)?,
                pr_state: row.get(1)?,
                pr_base: row.get(2)?,
                pr_url: row.get(3)?,
                checks_passed: row.get(4)?,
                checks_total: row.get(5)?,
                checks_state: row.get(6)?,
                additions: row.get::<_, Option<i64>>(7)?.unwrap_or(0),
                deletions: row.get::<_, Option<i64>>(8)?.unwrap_or(0),
                dirty: row.get::<_, Option<i64>>(9)?.unwrap_or(0) != 0,
            })
        });

        drop(_guard);

        match row {
            Ok(data) => Ok(Some(data)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(err) => Err(err.into()),
        }
    }

    pub fn upsert_path(&self, cache_key: &str, path: &Path) -> Result<()> {
        let _guard = db_lock().lock().expect("cache lock poisoned");
        self.conn.execute(
            r#"
            INSERT INTO worktree_cache (branch, path)
            VALUES (?, ?)
            ON CONFLICT(branch) DO UPDATE SET path = excluded.path
            "#,
            params![cache_key, path.to_string_lossy().to_string()],
        )?;
        Ok(())
    }

    pub fn upsert_pull_push(
        &self,
        cache_key: &str,
        path: &Path,
        pull: i64,
        push: i64,
    ) -> Result<()> {
        let _guard = db_lock().lock().expect("cache lock poisoned");
        self.conn.execute(
            r#"
            INSERT INTO worktree_cache (branch, path, pull, push, pullpush_validated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(branch) DO UPDATE SET
              path = excluded.path,
              pull = excluded.pull,
              push = excluded.push,
              pullpush_validated_at = excluded.pullpush_validated_at
            "#,
            params![
                cache_key,
                path.to_string_lossy().to_string(),
                pull,
                push,
                now_ts()
            ],
        )?;
        Ok(())
    }

    pub fn upsert_changes(
        &self,
        cache_key: &str,
        path: &Path,
        additions: i64,
        deletions: i64,
        dirty: bool,
    ) -> Result<()> {
        let _guard = db_lock().lock().expect("cache lock poisoned");
        self.conn.execute(
            r#"
            INSERT INTO worktree_cache (branch, path, additions, deletions, dirty, changes_updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(branch) DO UPDATE SET
              path = excluded.path,
              additions = excluded.additions,
              deletions = excluded.deletions,
              dirty = excluded.dirty,
              changes_updated_at = excluded.changes_updated_at
            "#,
            params![
                cache_key,
                path.to_string_lossy().to_string(),
                additions,
                deletions,
                if dirty { 1 } else { 0 },
                now_ts()
            ],
        )?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn upsert_pr_and_checks(
        &self,
        cache_key: &str,
        path: &Path,
        pr_number: Option<i64>,
        pr_state: Option<&str>,
        pr_base: Option<&str>,
        pr_url: Option<&str>,
        checks_passed: Option<i64>,
        checks_total: Option<i64>,
        checks_state: Option<&str>,
    ) -> Result<()> {
        let _guard = db_lock().lock().expect("cache lock poisoned");
        let now = now_ts();
        self.conn.execute(
            r#"
            INSERT INTO worktree_cache (
              branch, path, pr_number, pr_state, pr_base, pr_url,
              pr_updated_at, checks_passed, checks_total, checks_state, checks_updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(branch) DO UPDATE SET
              path = excluded.path,
              pr_number = excluded.pr_number,
              pr_state = excluded.pr_state,
              pr_base = excluded.pr_base,
              pr_url = excluded.pr_url,
              pr_updated_at = excluded.pr_updated_at,
              checks_passed = excluded.checks_passed,
              checks_total = excluded.checks_total,
              checks_state = excluded.checks_state,
              checks_updated_at = excluded.checks_updated_at
            "#,
            params![
                cache_key,
                path.to_string_lossy().to_string(),
                pr_number,
                pr_state,
                pr_base,
                pr_url,
                now,
                checks_passed,
                checks_total,
                checks_state,
                now,
            ],
        )?;
        Ok(())
    }
}
