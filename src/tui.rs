use crate::models::WorktreeInfo;
use crate::{git_ops, hooks, services};
use anyhow::Result;
use ratatui::backend::CrosstermBackend;
use ratatui::crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind};
use ratatui::crossterm::execute;
use ratatui::crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Text};
use ratatui::widgets::{Block, Borders, Cell, Clear, Paragraph, Row, Table, TableState};
use ratatui::Terminal;
use std::collections::HashMap;
use std::io::{self, Stderr};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const HEADERS: [&str; 6] = [
    "BRANCH NAME",
    "LAST COMMIT",
    "PULL/PUSH",
    "PULL REQUEST",
    "BEHIND|AHEAD",
    "CHANGES",
];

const COMMAND_BAR: &str =
    "Enter: open  |  n: new from main  |  N: new from selected  |  D: delete  |  R: rename  |  p: pull  |  P: push  |  r: refresh  |  q/Esc: quit";
const SPINNER: &[char] = &['|', '/', '-', '\\'];

enum ConfirmAction {
    Delete {
        branch: String,
        path: PathBuf,
        ref_name: String,
    },
}

enum InputAction {
    Rename {
        old_ref_name: String,
        old_path: PathBuf,
    },
    NewWorktree {
        base_branch: String,
        pull_before_create: Option<PathBuf>,
    },
}

enum Mode {
    Normal,
    Confirm {
        prompt: String,
        action: ConfirmAction,
    },
    Input {
        prompt: String,
        value: String,
        action: InputAction,
    },
}

#[derive(Clone, Copy)]
enum PostSuccessAction {
    None,
    ReloadOnly,
    ReloadAndRefresh,
}

struct OpResult {
    status: String,
    succeeded: bool,
    post_success_action: PostSuccessAction,
    selected_branch_after: Option<String>,
}

pub fn run_tui(
    repo_root: PathBuf,
    items: Vec<WorktreeInfo>,
    default_branch: String,
    warning: Option<String>,
    gh_available: bool,
) -> Result<Option<PathBuf>> {
    let mut terminal = setup_terminal()?;
    let mut app = TuiApp::new(repo_root, items, default_branch, warning, gh_available);
    app.start_refresh(false);

    let run_result = app.run(&mut terminal);
    let restore_result = restore_terminal(&mut terminal);

    restore_result?;

    run_result
}

struct TuiApp {
    repo_root: PathBuf,
    default_branch: String,
    warning: Option<String>,
    gh_available: bool,
    items: Arc<Mutex<Vec<WorktreeInfo>>>,
    table_state: TableState,
    mode: Mode,
    status: String,
    selected_path: Option<PathBuf>,
    should_quit: bool,
    busy: bool,
    spinner_index: usize,
    spinner_message: Option<String>,
    refresh_running: Arc<AtomicBool>,
    refresh_rx: Option<mpsc::Receiver<Option<String>>>,
    op_rx: Option<mpsc::Receiver<OpResult>>,
}

impl TuiApp {
    fn new(
        repo_root: PathBuf,
        mut items: Vec<WorktreeInfo>,
        default_branch: String,
        warning: Option<String>,
        gh_available: bool,
    ) -> Self {
        if !gh_available {
            for item in &mut items {
                item.pr_validated = true;
                item.checks_validated = true;
            }
        }

        let mut table_state = TableState::default();
        if items.is_empty() {
            table_state.select(None);
        } else {
            table_state.select(Some(0));
        }

        Self {
            repo_root,
            default_branch,
            warning,
            gh_available,
            items: Arc::new(Mutex::new(items)),
            table_state,
            mode: Mode::Normal,
            status: String::new(),
            selected_path: None,
            should_quit: false,
            busy: false,
            spinner_index: 0,
            spinner_message: None,
            refresh_running: Arc::new(AtomicBool::new(false)),
            refresh_rx: None,
            op_rx: None,
        }
    }

    fn run(
        &mut self,
        terminal: &mut Terminal<CrosstermBackend<Stderr>>,
    ) -> Result<Option<PathBuf>> {
        loop {
            self.handle_async_results();

            terminal.draw(|frame| self.draw(frame))?;

            if self.should_quit {
                return Ok(self.selected_path.take());
            }

            if event::poll(Duration::from_millis(100))? {
                if let Event::Key(key) = event::read()? {
                    if key.kind == KeyEventKind::Press {
                        self.handle_key(key);
                    }
                }
            }

            self.on_tick();
        }
    }

    fn on_tick(&mut self) {
        if self.busy || self.refresh_running.load(Ordering::SeqCst) {
            self.spinner_index = (self.spinner_index + 1) % SPINNER.len();
        }
    }

    fn handle_async_results(&mut self) {
        if let Some(rx) = &self.op_rx {
            match rx.try_recv() {
                Ok(result) => {
                    self.finish_operation(result);
                    self.op_rx = None;
                }
                Err(mpsc::TryRecvError::Disconnected) => {
                    self.busy = false;
                    self.spinner_message = None;
                    self.op_rx = None;
                    self.status = "Operation interrupted.".to_string();
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
        }

        if let Some(rx) = &self.refresh_rx {
            match rx.try_recv() {
                Ok(maybe_err) => {
                    if let Some(err) = maybe_err {
                        self.status = format!("Refresh failed: {err}");
                    } else if self.status.starts_with("Refreshing") {
                        self.status = "Refreshed.".to_string();
                    }
                    self.refresh_rx = None;
                }
                Err(mpsc::TryRecvError::Disconnected) => {
                    self.refresh_rx = None;
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
        }
    }

    fn finish_operation(&mut self, result: OpResult) {
        self.busy = false;
        self.spinner_message = None;
        self.status = result.status;

        if !result.succeeded {
            return;
        }

        match result.post_success_action {
            PostSuccessAction::None => {}
            PostSuccessAction::ReloadOnly | PostSuccessAction::ReloadAndRefresh => {
                if let Err(err) = self.reload_items(result.selected_branch_after.as_deref()) {
                    self.status = format!("Reload failed: {err}");
                    return;
                }
            }
        }

        match result.post_success_action {
            PostSuccessAction::None => {}
            PostSuccessAction::ReloadOnly => {
                let mut items = match self.items.lock() {
                    Ok(guard) => guard,
                    Err(poisoned) => poisoned.into_inner(),
                };
                mark_refresh_columns_validated(&mut items);
            }
            PostSuccessAction::ReloadAndRefresh => self.start_refresh(false),
        }
    }

    fn handle_key(&mut self, key: KeyEvent) {
        match self.mode {
            Mode::Normal => self.handle_key_normal(key),
            Mode::Confirm { .. } => self.handle_key_confirm(key),
            Mode::Input { .. } => self.handle_key_input(key),
        }
    }

    fn handle_key_normal(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Up => self.select_prev(),
            KeyCode::Down => self.select_next(),
            KeyCode::Enter => self.action_choose(),
            KeyCode::Esc => self.should_quit = true,
            KeyCode::Char('q') => self.should_quit = true,
            KeyCode::Char('r') => self.action_refresh(),
            KeyCode::Char('n') => self.action_new_worktree_from_main(),
            KeyCode::Char('N') => self.action_new_worktree_from_selected(),
            KeyCode::Char('d') => self.action_delete_worktree(),
            KeyCode::Char('D') => self.action_delete_worktree(),
            KeyCode::Char('R') => self.action_rename_worktree(),
            KeyCode::Char('p') => self.action_pull_worktree(),
            KeyCode::Char('P') => self.action_push_worktree(),
            _ => {}
        }
    }

    fn handle_key_confirm(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc | KeyCode::Char('n') | KeyCode::Char('N') => {
                self.mode = Mode::Normal;
                self.status = "Delete cancelled.".to_string();
            }
            KeyCode::Char('y') | KeyCode::Char('Y') => {
                let mode = std::mem::replace(&mut self.mode, Mode::Normal);
                if let Mode::Confirm { action, .. } = mode {
                    self.run_confirm_action(action);
                }
            }
            _ => {}
        }
    }

    fn handle_key_input(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => {
                let mode = std::mem::replace(&mut self.mode, Mode::Normal);
                if let Mode::Input { action, .. } = mode {
                    self.status = match action {
                        InputAction::Rename { .. } => "Rename cancelled.".to_string(),
                        InputAction::NewWorktree { .. } => "Create cancelled.".to_string(),
                    };
                }
            }
            KeyCode::Enter => {
                let mode = std::mem::replace(&mut self.mode, Mode::Normal);
                if let Mode::Input { value, action, .. } = mode {
                    self.run_input_action(value, action);
                }
            }
            KeyCode::Backspace => {
                if let Mode::Input { ref mut value, .. } = self.mode {
                    value.pop();
                }
            }
            KeyCode::Char(ch) => {
                if let Mode::Input { ref mut value, .. } = self.mode {
                    value.push(ch);
                }
            }
            _ => {}
        }
    }

    fn run_confirm_action(&mut self, action: ConfirmAction) {
        match action {
            ConfirmAction::Delete {
                branch,
                path,
                ref_name,
            } => {
                let repo_root = self.repo_root.clone();
                self.start_operation(
                    format!("Deleting {branch}"),
                    format!("Deleted {branch}."),
                    "Delete failed".to_string(),
                    None,
                    PostSuccessAction::ReloadOnly,
                    move || {
                        git_ops::worktree_remove(&repo_root, &path)?;
                        git_ops::branch_delete(&repo_root, &ref_name)?;
                        Ok(())
                    },
                );
            }
        }
    }

    fn run_input_action(&mut self, value: String, action: InputAction) {
        let normalized = value.trim().to_string();

        match action {
            InputAction::Rename {
                old_ref_name,
                old_path,
            } => {
                if normalized.is_empty() {
                    self.status = "Rename cancelled.".to_string();
                    return;
                }

                if !git_ops::is_valid_branch_name(&self.repo_root, &normalized) {
                    self.status = "Invalid branch name.".to_string();
                    return;
                }

                if git_ops::branch_exists(&self.repo_root, &normalized) {
                    self.status = "Branch already exists.".to_string();
                    return;
                }

                let repo_root = self.repo_root.clone();
                let new_branch = normalized.clone();
                let new_path = repo_root.join(&new_branch);

                self.start_operation(
                    format!("Renaming to {new_branch}"),
                    format!("Renamed to {new_branch}."),
                    "Rename failed".to_string(),
                    Some(new_branch.clone()),
                    PostSuccessAction::ReloadOnly,
                    move || {
                        git_ops::branch_rename(&repo_root, &old_ref_name, &new_branch)?;
                        git_ops::worktree_move(&repo_root, &old_path, &new_path)?;
                        Ok(())
                    },
                );
            }
            InputAction::NewWorktree {
                base_branch,
                pull_before_create,
            } => {
                if normalized.is_empty() {
                    self.status = "Create cancelled.".to_string();
                    return;
                }

                if !git_ops::is_valid_branch_name(&self.repo_root, &normalized) {
                    self.status = "Invalid branch name.".to_string();
                    return;
                }

                if git_ops::branch_exists(&self.repo_root, &normalized) {
                    self.status = "Branch already exists locally.".to_string();
                    return;
                }

                let new_path = self.repo_root.join(&normalized);
                if new_path.exists() {
                    self.status = "Target worktree path already exists.".to_string();
                    return;
                }

                let repo_root = self.repo_root.clone();
                let new_branch = normalized.clone();

                self.start_operation(
                    format!("Creating {new_branch}"),
                    format!("Created {new_branch}."),
                    "Create failed".to_string(),
                    Some(new_branch.clone()),
                    PostSuccessAction::ReloadOnly,
                    move || {
                        if let Some(base_path) = pull_before_create {
                            git_ops::pull(&base_path)?;
                        }

                        let target = repo_root.join(&new_branch);
                        if git_ops::remote_branch_exists(&repo_root, &new_branch) {
                            git_ops::fetch_branch(&repo_root, &new_branch)?;
                            git_ops::branch_set_upstream(
                                &repo_root,
                                &new_branch,
                                &format!("origin/{new_branch}"),
                            )?;
                            git_ops::worktree_add(&repo_root, &target, &new_branch, None)?;
                        } else {
                            git_ops::worktree_add(
                                &repo_root,
                                &target,
                                &new_branch,
                                Some(&base_branch),
                            )?;
                        }
                        hooks::run_post_worktree_creation_hooks(&repo_root, Some(&target))?;
                        Ok(())
                    },
                );
            }
        }
    }

    fn action_choose(&mut self) {
        let Some(current) = self.current_item() else {
            self.should_quit = true;
            return;
        };

        self.selected_path = Some(current.path);
        self.should_quit = true;
    }

    fn action_refresh(&mut self) {
        if self.busy {
            self.status = "Another operation is in progress.".to_string();
            return;
        }
        self.start_refresh(true);
    }

    fn action_pull_worktree(&mut self) {
        if self.busy {
            self.status = "Another operation is in progress.".to_string();
            return;
        }

        let Some(current) = self.current_item() else {
            self.status = "No worktrees available.".to_string();
            return;
        };

        if current.is_detached() {
            self.status = "Cannot pull a detached worktree.".to_string();
            return;
        }

        let branch = current.branch.clone();
        let path = current.path.clone();

        self.start_operation(
            format!("Pulling {branch}"),
            format!("Pulled {branch}."),
            "Pull failed".to_string(),
            Some(branch),
            PostSuccessAction::ReloadAndRefresh,
            move || {
                git_ops::pull(&path)?;
                Ok(())
            },
        );
    }

    fn action_push_worktree(&mut self) {
        if self.busy {
            self.status = "Another operation is in progress.".to_string();
            return;
        }

        let Some(current) = self.current_item() else {
            self.status = "No worktrees available.".to_string();
            return;
        };

        if current.is_detached() {
            self.status = "Cannot push a detached worktree.".to_string();
            return;
        }

        let branch = current.branch.clone();
        let path = current.path.clone();
        let ref_name = current.ref_name.clone().unwrap_or_default();
        let has_upstream = current.has_upstream;

        self.start_operation(
            format!("Pushing {branch}"),
            format!("Pushed {branch}."),
            "Push failed".to_string(),
            Some(branch),
            PostSuccessAction::ReloadAndRefresh,
            move || {
                if has_upstream {
                    git_ops::push(&path)?;
                } else {
                    git_ops::push_set_upstream(&path, &ref_name)?;
                }
                Ok(())
            },
        );
    }

    fn action_delete_worktree(&mut self) {
        if self.busy {
            self.status = "Another operation is in progress.".to_string();
            return;
        }

        let Some(current) = self.current_item() else {
            self.status = "No worktrees available.".to_string();
            return;
        };

        if current.is_detached() {
            self.status = "Cannot delete a detached worktree.".to_string();
            return;
        }

        let ref_name = current.ref_name.clone().unwrap_or_default();
        let mut warn_parts = Vec::new();
        if current.dirty {
            warn_parts.push("working tree has uncommitted changes".to_string());
        }
        if git_ops::has_unpushed_commits(&self.repo_root, &ref_name) {
            warn_parts.push("branch has unpushed commits".to_string());
        }

        let mut prompt = format!("Delete {}?", current.branch);
        if !warn_parts.is_empty() {
            prompt = format!("Delete {} ({})?", current.branch, warn_parts.join("; "));
        }

        self.mode = Mode::Confirm {
            prompt,
            action: ConfirmAction::Delete {
                branch: current.branch,
                path: current.path,
                ref_name,
            },
        };
    }

    fn action_rename_worktree(&mut self) {
        if self.busy {
            self.status = "Another operation is in progress.".to_string();
            return;
        }

        let Some(current) = self.current_item() else {
            self.status = "No worktrees available.".to_string();
            return;
        };

        if current.is_detached() {
            self.status = "Cannot rename a detached worktree.".to_string();
            return;
        }

        self.mode = Mode::Input {
            prompt: format!("Rename {} to:", current.branch),
            value: String::new(),
            action: InputAction::Rename {
                old_ref_name: current.ref_name.unwrap_or_default(),
                old_path: current.path,
            },
        };
    }

    fn action_new_worktree_from_main(&mut self) {
        if self.busy {
            self.status = "Another operation is in progress.".to_string();
            return;
        }

        let Some(main_item) = self
            .snapshot_items()
            .into_iter()
            .find(|item| item.branch == "main")
        else {
            self.status = "Cannot create from main: no 'main' worktree is available.".to_string();
            return;
        };

        self.mode = Mode::Input {
            prompt: "New branch name:".to_string(),
            value: String::new(),
            action: InputAction::NewWorktree {
                base_branch: "main".to_string(),
                pull_before_create: Some(main_item.path),
            },
        };
    }

    fn action_new_worktree_from_selected(&mut self) {
        if self.busy {
            self.status = "Another operation is in progress.".to_string();
            return;
        }

        let Some(current) = self.current_item() else {
            self.status = "No worktrees available.".to_string();
            return;
        };

        if current.is_detached() {
            self.status = "Cannot create from a detached worktree.".to_string();
            return;
        }

        self.mode = Mode::Input {
            prompt: format!("New branch name (from {}):", current.branch),
            value: String::new(),
            action: InputAction::NewWorktree {
                base_branch: current.branch,
                pull_before_create: None,
            },
        };
    }

    fn current_item(&self) -> Option<WorktreeInfo> {
        let selected = self.table_state.selected()?;
        let guard = match self.items.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard.get(selected).cloned()
    }

    fn snapshot_items(&self) -> Vec<WorktreeInfo> {
        let guard = match self.items.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard.clone()
    }

    fn select_prev(&mut self) {
        let len = self.snapshot_items().len();
        if len == 0 {
            self.table_state.select(None);
            return;
        }

        let current = self.table_state.selected().unwrap_or(0);
        let new_index = current.saturating_sub(1);
        self.table_state.select(Some(new_index));
    }

    fn select_next(&mut self) {
        let len = self.snapshot_items().len();
        if len == 0 {
            self.table_state.select(None);
            return;
        }

        let current = self.table_state.selected().unwrap_or(0);
        let new_index = (current + 1).min(len - 1);
        self.table_state.select(Some(new_index));
    }

    fn reload_items(&mut self, selected_branch: Option<&str>) -> Result<()> {
        self.default_branch = git_ops::get_default_branch(&self.repo_root);
        let mut new_items = services::load_worktrees(&self.repo_root)?;
        if !self.gh_available {
            for item in &mut new_items {
                item.pr_validated = true;
                item.checks_validated = true;
            }
        }

        {
            let mut guard = match self.items.lock() {
                Ok(guard) => guard,
                Err(poisoned) => poisoned.into_inner(),
            };
            *guard = new_items.clone();
        }

        if new_items.is_empty() {
            self.table_state.select(None);
            return Ok(());
        }

        let mut selected_index = 0_usize;
        if let Some(branch) = selected_branch {
            if let Some(idx) = new_items.iter().position(|item| item.branch == branch) {
                selected_index = idx;
            }
        }

        self.table_state.select(Some(selected_index));
        Ok(())
    }

    fn start_refresh(&mut self, manual: bool) {
        if self.refresh_running.swap(true, Ordering::SeqCst) {
            if manual {
                self.status = "Refresh already in progress...".to_string();
            }
            return;
        }

        if manual {
            self.status = "Refreshing...".to_string();
        }

        let repo_root = self.repo_root.clone();
        let items = Arc::clone(&self.items);
        let gh_available = self.gh_available;
        let refresh_running = Arc::clone(&self.refresh_running);
        let (tx, rx) = mpsc::channel();
        self.refresh_rx = Some(rx);

        thread::spawn(move || {
            let snapshot = {
                let guard = match items.lock() {
                    Ok(guard) => guard,
                    Err(poisoned) => poisoned.into_inner(),
                };
                guard.clone()
            };

            let mut refreshed = snapshot;
            let result = services::refresh_from_upstream(&repo_root, &mut refreshed, gh_available)
                .err()
                .map(|err| err.to_string());

            let mut guard = match items.lock() {
                Ok(guard) => guard,
                Err(poisoned) => poisoned.into_inner(),
            };
            merge_refreshed_items(&mut guard, &refreshed);

            let _ = tx.send(result);
            refresh_running.store(false, Ordering::SeqCst);
        });
    }

    fn start_operation<F>(
        &mut self,
        spinner_message: String,
        success_message: String,
        failure_prefix: String,
        selected_branch_after: Option<String>,
        post_success_action: PostSuccessAction,
        action: F,
    ) where
        F: FnOnce() -> Result<()> + Send + 'static,
    {
        if self.busy {
            self.status = "Another operation is in progress.".to_string();
            return;
        }

        self.busy = true;
        self.spinner_index = 0;
        self.spinner_message = Some(spinner_message);

        let (tx, rx) = mpsc::channel();
        self.op_rx = Some(rx);

        thread::spawn(move || {
            let result = match action() {
                Ok(()) => OpResult {
                    status: success_message,
                    succeeded: true,
                    post_success_action,
                    selected_branch_after,
                },
                Err(err) => OpResult {
                    status: format!("{failure_prefix}: {err}"),
                    succeeded: false,
                    post_success_action: PostSuccessAction::None,
                    selected_branch_after: None,
                },
            };

            let _ = tx.send(result);
        });
    }

    fn status_line(&self) -> String {
        let spinner = SPINNER[self.spinner_index % SPINNER.len()];

        if let Some(message) = &self.spinner_message {
            return format!("{message} {spinner}");
        }

        if self.refresh_running.load(Ordering::SeqCst) {
            return format!("Refreshing {spinner}");
        }

        self.status.clone()
    }

    fn repo_line(&self) -> String {
        format!("Repo: {}", self.repo_root.display())
    }

    fn draw(&mut self, frame: &mut ratatui::Frame<'_>) {
        let area = frame.area();
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(1),
                Constraint::Length(1),
                Constraint::Length(1),
                Constraint::Length(1),
                Constraint::Min(1),
            ])
            .split(area);

        frame.render_widget(Paragraph::new(self.repo_line()), chunks[0]);
        frame.render_widget(Paragraph::new(COMMAND_BAR), chunks[1]);
        frame.render_widget(Paragraph::new(self.status_line()), chunks[2]);
        frame.render_widget(
            Paragraph::new(self.warning.clone().unwrap_or_default())
                .style(Style::default().fg(Color::Yellow)),
            chunks[3],
        );

        let items = self.snapshot_items();
        let rows = items.iter().map(|item| {
            let values = format_row(item, &self.default_branch);
            let cells: Vec<Cell<'_>> = values
                .into_iter()
                .map(|(text, cached)| {
                    if cached {
                        Cell::from(text).style(Style::default().fg(Color::DarkGray))
                    } else {
                        Cell::from(text)
                    }
                })
                .collect();
            Row::new(cells)
        });

        let table = Table::new(
            rows,
            [
                Constraint::Length(36),
                Constraint::Length(12),
                Constraint::Length(18),
                Constraint::Length(24),
                Constraint::Length(14),
                Constraint::Length(14),
            ],
        )
        .header(
            Row::new(HEADERS)
                .style(Style::default().add_modifier(Modifier::BOLD))
                .bottom_margin(0),
        )
        .row_highlight_style(Style::default().add_modifier(Modifier::REVERSED))
        .highlight_symbol(" > ")
        .block(Block::default().borders(Borders::TOP));

        frame.render_stateful_widget(table, chunks[4], &mut self.table_state);

        match &self.mode {
            Mode::Normal => {}
            Mode::Confirm { prompt, .. } => {
                let popup = centered_rect(70, 22, area);
                frame.render_widget(Clear, popup);
                let content = vec![
                    Line::from(prompt.as_str()),
                    Line::from(""),
                    Line::from("Press y to confirm, n or Esc to cancel."),
                ];
                let widget = Paragraph::new(Text::from(content))
                    .block(Block::default().borders(Borders::ALL).title("Confirm"));
                frame.render_widget(widget, popup);
            }
            Mode::Input { prompt, value, .. } => {
                let popup = centered_rect(70, 28, area);
                frame.render_widget(Clear, popup);
                let content = vec![
                    Line::from(prompt.as_str()),
                    Line::from(""),
                    Line::from(format!("> {value}")),
                    Line::from(""),
                    Line::from("Enter to submit, Esc to cancel."),
                ];
                let widget = Paragraph::new(Text::from(content))
                    .block(Block::default().borders(Borders::ALL).title("Input"));
                frame.render_widget(widget, popup);

                let cursor_x = popup.x + 3 + value.chars().count() as u16;
                let cursor_y = popup.y + 3;
                frame.set_cursor_position((cursor_x, cursor_y));
            }
        }
    }
}

fn setup_terminal() -> Result<Terminal<CrosstermBackend<Stderr>>> {
    enable_raw_mode()?;
    let mut stderr = io::stderr();
    execute!(stderr, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stderr);
    let terminal = Terminal::new(backend)?;
    Ok(terminal)
}

fn restore_terminal(terminal: &mut Terminal<CrosstermBackend<Stderr>>) -> Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(())
}

fn centered_rect(percent_x: u16, percent_y: u16, area: Rect) -> Rect {
    let popup_layout = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage((100 - percent_y) / 2),
            Constraint::Percentage(percent_y),
            Constraint::Percentage((100 - percent_y) / 2),
        ])
        .split(area);

    Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage((100 - percent_x) / 2),
            Constraint::Percentage(percent_x),
            Constraint::Percentage((100 - percent_x) / 2),
        ])
        .split(popup_layout[1])[1]
}

fn relative_time(ts: i64) -> String {
    if ts <= 0 {
        return "unknown".to_string();
    }

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(ts);
    let delta = (now - ts).max(0);

    if delta < 60 {
        format!("{delta}s ago")
    } else if delta < 3600 {
        format!("{}m ago", delta / 60)
    } else if delta < 86_400 {
        format!("{}h ago", delta / 3600)
    } else if delta < 604_800 {
        format!("{}d ago", delta / 86_400)
    } else if delta < 2_629_800 {
        format!("{}w ago", delta / 604_800)
    } else {
        format!("{}mo ago", delta / 2_629_800)
    }
}

fn format_pull_push(item: &WorktreeInfo) -> (String, bool) {
    let mut pull_push = String::new();
    if item.pr_state.as_deref() == Some("MERGED") {
        pull_push = "merged (remote deleted)".to_string();
    } else if item.has_upstream && (item.pull != 0 || item.push != 0) {
        pull_push = format!("{}↓ {}↑", item.pull, item.push);
    }

    if item.dirty {
        if pull_push.is_empty() {
            pull_push = "(dirty)".to_string();
        } else {
            pull_push.push_str(" (dirty)");
        }
    }

    (pull_push, !item.pull_push_validated)
}

fn format_pr(item: &WorktreeInfo, default_branch: &str) -> (String, bool) {
    let mut pr = String::new();
    if let Some(number) = item.pr_number {
        let state = item.pr_state.as_deref().unwrap_or("OPEN");
        if state == "MERGED" {
            pr = format!("#{number} merged (remote deleted)");
        } else if state == "CLOSED" {
            pr = format!("#{number} closed");
        } else {
            pr = format!("#{number}");
        }

        if let Some(base) = &item.pr_base {
            if base != default_branch {
                pr.push_str(&format!(" -> {base}"));
            }
        }
    }

    (pr, !item.pr_validated)
}

fn format_changes(item: &WorktreeInfo) -> (String, bool) {
    (
        format!("+{} -{}", item.additions, item.deletions),
        !item.changes_validated,
    )
}

fn format_row(item: &WorktreeInfo, default_branch: &str) -> Vec<(String, bool)> {
    let (pr, pr_cached) = format_pr(item, default_branch);
    let (pull_push, pull_push_cached) = format_pull_push(item);
    let (changes, changes_cached) = format_changes(item);
    let behind = item.behind;
    let ahead = item.ahead;

    vec![
        (item.branch.clone(), false),
        (relative_time(item.last_commit_ts), false),
        (pull_push, pull_push_cached),
        (pr, pr_cached),
        (format!("{behind:>6}|{ahead}"), false),
        (changes, changes_cached),
    ]
}

fn merge_refreshed_items(current: &mut [WorktreeInfo], refreshed: &[WorktreeInfo]) {
    let refreshed_by_key: HashMap<&str, &WorktreeInfo> = refreshed
        .iter()
        .map(|item| (item.cache_key.as_str(), item))
        .collect();

    for item in current.iter_mut() {
        let Some(new_item) = refreshed_by_key.get(item.cache_key.as_str()) else {
            continue;
        };

        item.pull = new_item.pull;
        item.push = new_item.push;
        item.pull_push_validated = new_item.pull_push_validated;
        item.has_upstream = new_item.has_upstream;
        item.additions = new_item.additions;
        item.deletions = new_item.deletions;
        item.dirty = new_item.dirty;
        item.pr_number = new_item.pr_number;
        item.pr_state = new_item.pr_state.clone();
        item.pr_base = new_item.pr_base.clone();
        item.pr_url = new_item.pr_url.clone();
        item.pr_validated = new_item.pr_validated;
        item.checks_passed = new_item.checks_passed;
        item.checks_total = new_item.checks_total;
        item.checks_state = new_item.checks_state.clone();
        item.checks_validated = new_item.checks_validated;
        item.changes_validated = new_item.changes_validated;
    }
}

fn mark_refresh_columns_validated(items: &mut [WorktreeInfo]) {
    for item in items {
        item.pull_push_validated = true;
        item.changes_validated = true;
        item.pr_validated = true;
        item.checks_validated = true;
    }
}

pub fn write_selected_path(selected_path: &Path) -> Result<()> {
    println!("{}", selected_path.display());
    Ok(())
}
