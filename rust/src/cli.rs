//! Command line use (no window):
//!
//!   bbr add FILE.blend [--start N --end N ...]
//!   bbr list
//!   bbr run                  # render the queue; Ctrl+C = stop now
//!   bbr scan                 # find Blender installs and save them
//!   bbr requeue NAME [--keep-frames]
//!   bbr remove NAME
//!   bbr report NAME
//!   bbr gui                  # desktop window (build with --features gui)

use crate::installs;
use crate::jobqueue::JobQueue;
use crate::models::{JobSettings, Status};
use crate::reports;
use crate::runner::{fmt_duration, run_session, RunConfig};
use anyhow::{bail, Result};
use clap::{Parser, Subcommand};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

#[derive(Parser)]
#[command(name = "bbr", version, about = "Blender Batch Renderer")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Add .blend files to the queue.
    Add {
        files: Vec<String>,
        /// Render root for the job folders (defaults to the saved root).
        #[arg(long)]
        root: Option<String>,
        /// Blender executable or "auto".
        #[arg(long, default_value = "auto")]
        blender: String,
        #[arg(long)]
        start: Option<i32>,
        #[arg(long)]
        end: Option<i32>,
        #[arg(long)]
        step: Option<i32>,
        #[arg(long)]
        samples: Option<u32>,
        /// Resolution percentage.
        #[arg(long)]
        pct: Option<u32>,
        /// Maximum render attempts.
        #[arg(long)]
        attempts: Option<u32>,
        /// Pause between attempts (seconds).
        #[arg(long)]
        delay: Option<u64>,
        /// Kill Blender after this many minutes (0 = off).
        #[arg(long)]
        timeout: Option<u64>,
        #[arg(long)]
        engine: Option<String>,
        #[arg(long)]
        format: Option<String>,
    },
    /// Show the queue.
    List,
    /// Render the queue; Ctrl+C = stop now.
    Run {
        /// Parallel Blender workers.
        #[arg(long, default_value = "1")]
        workers: usize,
        #[arg(long, default_value = "auto")]
        blender: String,
        /// Exit after the current jobs finish instead of draining the queue.
        #[arg(long)]
        stop_after_job: bool,
    },
    /// Find Blender installs and save them.
    Scan,
    /// Put a job back in the queue.
    Requeue {
        name: String,
        #[arg(long)]
        keep_frames: bool,
    },
    /// Remove a job (deletes its folder).
    Remove { name: String },
    /// Build CSV + XLSX reports for a job.
    Report { name: String },
    /// Desktop window (requires the "gui" feature).
    Gui,
}

pub fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Add {
            files,
            root,
            blender: _,
            start,
            end,
            step,
            samples,
            pct,
            attempts,
            delay,
            timeout,
            engine,
            format,
        } => {
            if files.is_empty() {
                bail!("no files given");
            }
            let mut queue = JobQueue::load();
            if let Some(r) = root {
                queue.root = r;
            }
            let mut settings = JobSettings::default();
            if let Some(v) = start { settings.start = v; }
            if let Some(v) = end { settings.end = v; }
            if let Some(v) = step { settings.step = v; }
            if let Some(v) = samples { settings.samples = v; }
            if let Some(v) = pct { settings.pct = v; }
            if let Some(v) = attempts { settings.attempts = v; }
            if let Some(v) = delay { settings.delay_s = v; }
            if let Some(v) = timeout { settings.timeout_min = v; }
            if let Some(v) = engine { settings.engine = v; }
            if let Some(v) = format { settings.format = v; }
            for f in &files {
                let job = queue.add(Path::new(f), settings.clone(), None)?;
                println!(
                    "added {} (frames {}-{} step {})",
                    job.name, job.settings.start, job.settings.end, job.settings.step
                );
            }
            println!("root: {}", queue.root);
        }
        Cmd::List => {
            let queue = JobQueue::load();
            println!("root: {}", queue.root);
            let jobs = queue.load_jobs();
            if jobs.is_empty() {
                println!("queue is empty");
            }
            for job in jobs {
                println!(
                    "{:<40} {:<12} {}/{} frames done  {}",
                    job.name,
                    job.status.as_str(),
                    job.frames_done(),
                    job.frame_numbers().len(),
                    job.status_note
                );
            }
        }
        Cmd::Run {
            workers,
            blender,
            stop_after_job,
        } => {
            let queue = Arc::new(Mutex::new(JobQueue::load()));
            let stop = Arc::new(AtomicBool::new(false));
            {
                let stop = stop.clone();
                ctrlc::set_handler(move || {
                    stop.store(true, Ordering::SeqCst);
                })?;
            }
            let cfg = RunConfig {
                workers,
                blender,
                stop_after_job,
                stop_after_frame: Default::default(),
            };
            println!("rendering with {} worker(s) - Ctrl+C stops", cfg.workers);
            let summary = run_session(queue, &cfg, &stop, &Default::default(), None)?;
            println!(
                "session: {} jobs ok, {} failed, {} frames, {} failed frames, {} rendering",
                summary.jobs_done,
                summary.jobs_failed,
                summary.frames,
                summary.failed,
                fmt_duration(summary.render_secs)
            );
        }
        Cmd::Scan => {
            let found = installs::scan();
            if found.is_empty() {
                println!("no Blender installs found");
            }
            for i in &found {
                println!("{}  (Blender {})", i.path, i.version);
            }
        }
        Cmd::Requeue { name, keep_frames } => {
            let mut queue = JobQueue::load();
            if queue.requeue(&name, keep_frames)? {
                println!("{} queued again", name);
            } else {
                bail!("no job named {}", name);
            }
        }
        Cmd::Remove { name } => {
            let mut queue = JobQueue::load();
            if queue.remove(&name)? {
                println!("{} removed", name);
            } else {
                bail!("no job named {}", name);
            }
        }
        Cmd::Report { name } => {
            let queue = JobQueue::load();
            let job = queue
                .load_job(&name)
                .ok_or_else(|| anyhow::anyhow!("no job named {}", name))?;
            let path = reports::build_all(&job)?;
            println!("report: {}", path.display());
        }
        Cmd::Gui => {
            #[cfg(feature = "gui")]
            {
                crate::gui::run_gui()?;
                return Ok(());
            }
            #[cfg(not(feature = "gui"))]
            bail!("built without the gui feature - rebuild with: cargo build --features gui");
        }
    }
    Ok(())
}

// keep the Status import referenced even on minimal feature sets
#[allow(dead_code)]
fn _unused(_s: Status) {}