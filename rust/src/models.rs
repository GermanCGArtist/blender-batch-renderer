//! Job / settings data structures and persistence.
//!
//! Each job lives in its own folder (same layout as the Python version):
//!
//!   <render root>/<job name>/
//!       frames/       image sequence
//!       preview/      preview video
//!       data/        frames.csv, attempts.csv, report.xlsx
//!       logs/        attempt_NNN.log
//!       job.json     full job state - source of truth for resuming

use crate::storage::{atomic_write_json, read_json};
use anyhow::Result;
use chrono::Local;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

// ---- status ------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Queued,
    Rendering,
    Stopped,
    Done,
    DoneErrors,
    Cancelled,
    Failed,
    OnHold,
}

impl Default for Status {
    fn default() -> Self {
        Status::Queued
    }
}

impl Status {
    pub fn as_str(&self) -> &'static str {
        match self {
            Status::Queued => "queued",
            Status::Rendering => "rendering",
            Status::Stopped => "stopped",
            Status::Done => "done",
            Status::DoneErrors => "done_errors",
            Status::Cancelled => "cancelled",
            Status::Failed => "failed",
            Status::OnHold => "on_hold",
        }
    }

    /// True when the runner may pick this job up.
    pub fn runnable(&self) -> bool {
        matches!(self, Status::Queued | Status::Stopped)
    }
}

// ---- settings ----------------------------------------------------------

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct JobSettings {
    #[serde(default = "d_start")]
    pub start: i32,
    #[serde(default = "d_end")]
    pub end: i32,
    #[serde(default = "d_step")]
    pub step: i32,
    /// Cycles samples override, 0 = keep what the .blend uses.
    #[serde(default)]
    pub samples: u32,
    /// Resolution percentage override, 0 = keep.
    #[serde(default = "d_pct")]
    pub pct: u32,
    /// Maximum render attempts per job.
    #[serde(default = "d_attempts")]
    pub attempts: u32,
    /// Pause between attempts (seconds).
    #[serde(default)]
    pub delay_s: u64,
    /// Kill Blender after this many minutes, 0 = no timeout.
    #[serde(default)]
    pub timeout_min: u64,
    /// "auto" or engine id like "CYCLES" / "BLENDER_EEVEE_NEXT".
    #[serde(default = "d_auto")]
    pub engine: String,
    /// Output image format, e.g. PNG, OPEN_EXR, FFMPEG.
    #[serde(default = "d_png")]
    pub format: String,
    /// Blender executable for this job, or "auto".
    #[serde(default = "d_auto")]
    pub blender: String,
}

fn d_start() -> i32 { 1 }
fn d_end() -> i32 { 250 }
fn d_step() -> i32 { 1 }
fn d_pct() -> u32 { 100 }
fn d_attempts() -> u32 { 3 }
fn d_auto() -> String { "auto".to_string() }
fn d_png() -> String { "PNG".to_string() }
fn d_true() -> bool { true }

impl Default for JobSettings {
    fn default() -> Self {
        JobSettings {
            start: 1,
            end: 250,
            step: 1,
            samples: 0,
            pct: 100,
            attempts: 3,
            delay_s: 0,
            timeout_min: 0,
            engine: d_auto(),
            format: d_png(),
            blender: d_auto(),
        }
    }
}

// ---- records -----------------------------------------------------------

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct FrameRecord {
    #[serde(default = "d_pending")]
    pub status: String,
    #[serde(default)]
    pub attempts: u32,
    #[serde(default)]
    pub seconds: Option<f64>,
}

fn d_pending() -> String { "pending".to_string() }

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AttemptRecord {
    pub index: u32,
    pub started: String,
    pub ended: String,
    pub ok: bool,
    pub exit_code: Option<i32>,
    pub frames_done: u32,
    pub frames_failed: u32,
    pub log: String,
}

// ---- job ---------------------------------------------------------------

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Job {
    pub name: String,
    pub folder: String,
    pub blend: String,
    #[serde(default)]
    pub status: Status,
    #[serde(default)]
    pub status_note: String,
    /// Disabled jobs stay in the queue but are skipped by the runner.
    #[serde(default = "d_true")]
    pub enabled: bool,
    #[serde(default)]
    pub settings: JobSettings,
    #[serde(default)]
    pub frames: BTreeMap<i32, FrameRecord>,
    #[serde(default)]
    pub attempts: Vec<AttemptRecord>,
    /// Change stamp of the .blend when rendering began (size + mtime).
    #[serde(default)]
    pub blend_stamp: Option<String>,
    #[serde(default)]
    pub finished: Option<String>,
}

impl Job {
    pub fn new(folder: &Path, blend: &Path, settings: JobSettings) -> Job {
        Job {
            name: folder
                .file_name()
                .map(|s| s.to_string_lossy().to_string())
                .unwrap_or_else(|| "job".to_string()),
            folder: folder.to_string_lossy().to_string(),
            blend: blend.to_string_lossy().to_string(),
            status: Status::Queued,
            status_note: String::new(),
            enabled: true,
            settings,
            frames: BTreeMap::new(),
            attempts: Vec::new(),
            blend_stamp: None,
            finished: None,
        }
    }

    pub fn job_file(&self) -> PathBuf {
        Path::new(&self.folder).join("job.json")
    }

    pub fn frames_dir(&self) -> PathBuf {
        Path::new(&self.folder).join("frames")
    }

    pub fn data_dir(&self) -> PathBuf {
        Path::new(&self.folder).join("data")
    }

    pub fn logs_dir(&self) -> PathBuf {
        Path::new(&self.folder).join("logs")
    }

    pub fn ensure_folders(&self) {
        for d in [
            self.frames_dir(),
            Path::new(&self.folder).join("preview"),
            self.data_dir(),
            self.logs_dir(),
        ] {
            let _ = fs::create_dir_all(d);
        }
    }

    pub fn save(&self) -> Result<()> {
        self.ensure_folders();
        let v = serde_json::to_value(self)?;
        atomic_write_json(&self.job_file(), &v)
    }

    pub fn load(folder: &Path) -> Option<Job> {
        let v = read_json(&folder.join("job.json"))?;
        let mut job: Job = serde_json::from_value(v).ok()?;
        // the folder may have been moved
        job.folder = folder.to_string_lossy().to_string();
        Some(job)
    }

    /// All frame numbers of the configured range.
    pub fn frame_numbers(&self) -> Vec<i32> {
        let s = &self.settings;
        let (lo, hi) = if s.start <= s.end {
            (s.start, s.end)
        } else {
            (s.end, s.start)
        };
        let step = if s.step == 0 { 1 } else { s.step.abs() };
        (lo..=hi).step_by(step as usize).collect()
    }

    pub fn frames_done(&self) -> usize {
        self.frames
            .values()
            .filter(|r| r.status == "done")
            .count()
    }

    pub fn frames_missing(&self) -> Vec<i32> {
        self.frame_numbers()
            .into_iter()
            .filter(|n| self.frames.get(n).map(|r| r.status == "done") != Some(true))
            .collect()
    }

    /// Cheap change stamp of the .blend: size + modification time.
    pub fn current_blend_stamp(&self) -> Option<String> {
        let meta = fs::metadata(&self.blend).ok()?;
        let size = meta.len();
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(0);
        Some(format!("{}-{}", size, mtime))
    }

    pub fn blend_changed(&self) -> bool {
        match (&self.blend_stamp, self.current_blend_stamp()) {
            (Some(a), Some(b)) => a != &b,
            _ => false,
        }
    }

    pub fn mark_started(&mut self) {
        self.status = Status::Rendering;
        self.status_note.clear();
        if self.blend_stamp.is_none() {
            self.blend_stamp = self.current_blend_stamp();
        }
    }

    pub fn record_frame_attempt(&mut self, frame: i32) {
        let e = self.frames.entry(frame).or_default();
        e.attempts += 1;
    }

    pub fn record_frame_done(&mut self, frame: i32, seconds: Option<f64>) {
        let e = self.frames.entry(frame).or_default();
        e.status = "done".to_string();
        if let Some(s) = seconds {
            e.seconds = Some(s);
        }
    }

    pub fn push_attempt(&mut self, rec: AttemptRecord) {
        self.attempts.push(rec);
    }

    pub fn now() -> String {
        Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
    }
}