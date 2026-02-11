mod cache_db;
mod cli;
mod gh_ops;
mod git_ops;
mod hooks;
mod models;
mod services;
mod tui;

fn main() {
    if let Err(err) = cli::run() {
        eprintln!("{err}");
        std::process::exit(1);
    }
}
