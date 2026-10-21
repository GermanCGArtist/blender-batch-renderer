//! Blender Batch Renderer (bbr) - Rust rewrite.
//!
//! Renders a queue of .blend files with one or more parallel Blender workers:
//! retries, per-frame tracking, logs, reports and a desktop GUI (feature "gui").

mod cli;
#[cfg(feature = "gui")]
mod gui;
mod installs;
mod jobqueue;
mod models;
mod process;
mod reports;
mod runner;
mod storage;

fn main() -> anyhow::Result<()> {
    cli::main()
}