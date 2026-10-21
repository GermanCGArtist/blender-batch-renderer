//! Job and settings data structures. Field names match the Python app's
//! `job.json`; unknown keys are preserved so mixed use never loses data.
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::storage::{atomic_write_json, read_json};

pub const QUEUED: &str = "queued";
pub const RENDERING: &str = "rendering";
pub const STOPPED: &str = "stopped";
pub const DONE: &str = "done";
pub const DONE_ERRORS: &str = "done (with failed frames)";
pub const FAILED: &str = "failed";
pub const CANCELLED: &str = "cancelled";
pub const ON_HOLD: &str = "on hold";

pub const F_DONE: &str = "done";
pub const F_FAILED: &str = "failed";
pub const F_PENDING: &str = "pending";

fn d_blender() -> String { "auto".into() }
fn d_attempts() -> u32 { 3 }
fn d_delay() -> u32 { 30 }
fn d_true() -> bool { true }
fn d_transfer() -> String { "full".into() }
fn d_queued() -> String { QUEUED.into() }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct JobSettings {
    #[serde(default)] pub render_root: String,
    #[serde(default = "d_blender")] pub blender: String,
    #[serde(default)] pub frame_start: Option<i64>,
    #[serde(default)] pub frame_end: Option<i64>,
    #[serde(default)] pub samples: Option<i64>,
    #[serde(default)] pub resolution_pct: Option<i64>,
    #[serde(default = "d_attempts")] pub max_attempts: u32,
    #[serde(default = "d_delay")] pub retry_delay_s: u32,
    #[serde(default)] pub frame_timeout_min: f64,
    #[serde(default = "d_true")] pub make_preview: bool,
    #[serde(default)] pub preflight: bool,
    #[serde(default)] pub restart_every: u32,
    #[serde(default)] pub machines: Vec<String>,
    #[serde(default)] pub ignore_addons: bool,
    #[serde(default)] pub devices: BTreeMap<String, String>,
    #[serde(default = "d_transfer")] pub transfer: String,
    #[serde(default = "d_true")] pub consistency_check: bool,
    #[serde(flatten)] pub extra: Map<String, Value>,
}

impl Default for JobSettings {
    fn default() -> Self {
        serde_json::from_value(Value::Object(Map::new())).expect("defaults")
    }
}

/// One frame's record in `job.json`.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct FrameRecord {
    #[serde(default)] pub status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")] pub rel: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")] pub file: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")] pub render_s: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")] pub size: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")] pub finished_at: Option<f64>,
    #[serde(flatten)] pub extra: Map<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Job {
    pub blend_path: String,
    #[serde(default)] pub folder: String,
    pub name: String,
    #[serde(default)] pub settings: JobSettings,
    #[serde(default)] pub id: String,
    #[serde(default = "d_queued")] pub status: String,
    #[serde(default)] pub status_note: String,
    #[serde(default)] pub file_version: Option<Vec<i64>>,
    #[serde(default)] pub blender_used: Option<Value>,
    #[serde(default)] pub scene_info: Option<Value>,
    #[serde(default)] pub frames: BTreeMap<String, FrameRecord>,
    #[serde(default)] pub attempts: Vec<Value>,
    #[serde(default)] pub load_failures: u32,
    #[serde(default)] pub preflight: Option<Value>,
    #[serde(default)] pub created: f64,
    #[serde(default)] pub started: Option<f64>,
    #[serde(default)] pub finished: Option<f64>,
    #[serde(default)] pub preview_path: Option<String>,
    #[serde(default)] pub group: String,
    #[serde(default = "d_true")] pub enabled: bool,
    #[serde(default)] pub blend_stamp: Option<Vec<f64>>,
    #[serde(default)] pub package: Option<Value>,
    #[serde(default)] pub approvals: BTreeMap<String, Value>,
    #[serde(flatten)] pub extra: Map<String, Value>,
}

pub fn frame_filename(prefix: &str, frame: i64, ext: &str) -> String {
    let sign = if frame < 0 { "-" } else { "" };
    format!("{prefix}{sign}{:04}{ext}", frame.abs())
}

impl Job {
    pub fn folder_p(&self) -> &Path { Path::new(&self.folder) }
    pub fn frames_dir(&self) -> PathBuf { self.folder_p().join("frames") }
    pub fn job_file(&self) -> PathBuf { self.folder_p().join("job.json") }
    pub fn file_prefix(&self) -> String { format!("{}_", self.name) }

    pub fn ensure_folders(&self) -> std::io::Result<()> {
        for d in ["frames", "preview", "data", "logs", "logs/crashes", "logs/tmp"] {
            std::fs::create_dir_all(self.folder_p().join(d))?;
        }
        Ok(())
    }

    /// Frames of the scene as configured in the file (`None` until Blender reported it).
    pub fn frame_list(&self) -> Option<Vec<i64>> {
        let si = self.scene_info.as_ref()?;
        let start = si.get("frame_start")?.as_i64()?;
        let end = si.get("frame_end")?.as_i64()?;
        let step = si.get("frame_step").and_then(Value::as_i64).unwrap_or(1).max(1);
        Some((start..=end).step_by(step as usize).collect())
    }

    pub fn frame_path(&self, frame: i64) -> PathBuf {
        let ext = self.scene_info.as_ref()
            .and_then(|s| s.get("file_extension")).and_then(Value::as_str).unwrap_or(".png");
        self.frames_dir().join(frame_filename(&self.file_prefix(), frame, ext))
    }

    /// Where a finished frame lives; paths are relative to the job folder so
    /// moving the render folder does not lose frames.
    pub fn frame_file(&self, frame: i64) -> PathBuf {
        if let Some(rec) = self.frames.get(&frame.to_string()) {
            if let Some(rel) = &rec.rel {
                return self.folder_p().join(rel);
            }
            if let Some(f) = &rec.file {
                let p = PathBuf::from(f);
                if p.exists() { return p; }
                if let Some(n) = p.file_name() { return self.frames_dir().join(n); }
            }
        }
        self.frame_path(frame)
    }

    /// Mark `done` frames whose file vanished or is empty as pending. Returns the count.
    pub fn verify_done_frames(&mut self) -> usize {
        let keys: Vec<String> = self.frames.iter()
            .filter(|(_, r)| r.status == F_DONE).map(|(k, _)| k.clone()).collect();
        let mut dropped = 0;
        for k in keys {
            let Ok(n) = k.parse::<i64>() else { continue };
            let ok = std::fs::metadata(self.frame_file(n)).map(|m| m.len() > 0).unwrap_or(false);
            if !ok {
                if let Some(r) = self.frames.get_mut(&k) { r.status = F_PENDING.into(); }
                dropped += 1;
            }
        }
        dropped
    }

    pub fn count(&self, status: &str) -> usize {
        self.frames.values().filter(|r| r.status == status).count()
    }

    pub fn remaining_frames(&self) -> Option<Vec<i64>> {
        let all = self.frame_list()?;
        let skip: std::collections::HashSet<i64> = self.frames.iter()
            .filter(|(_, r)| r.status == F_DONE || r.status == F_FAILED)
            .filter_map(|(k, _)| k.parse().ok()).collect();
        Some(all.into_iter().filter(|f| !skip.contains(f)).collect())
    }

    pub fn save(&self) -> std::io::Result<()> {
        self.ensure_folders()?;
        atomic_write_json(&self.job_file(), self)
    }

    pub fn load(folder: &Path) -> Option<Job> {
        let mut j: Job = read_json(&folder.join("job.json"))?;
        j.folder = folder.to_string_lossy().into_owned(); // folder may have been moved
        Some(j)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A job.json as the Python app writes it (including a key V2 doesn't know).
    const PY_JOB: &str = r#"{
      "blend_path": "/p/shot_010.blend", "folder": "/old/place", "name": "shot_010",
      "settings": {"render_root": "", "blender": "auto", "frame_start": 1, "frame_end": 4,
                   "samples": null, "future_flag": 7},
      "id": "abc123def456", "status": "stopped", "status_note": "",
      "scene_info": {"frame_start": 1, "frame_end": 4, "frame_step": 1, "file_extension": ".png"},
      "frames": {"1": {"status": "done", "rel": "frames/shot_010_0001.png", "render_s": 12.5, "size": 3},
                 "2": {"status": "failed", "attempts": 3}},
      "attempts": [], "created": 1.0, "enabled": true, "something_new": {"a": 1}
    }"#;

    #[test]
    fn reads_python_job_and_keeps_unknown_keys() {
        let j: Job = serde_json::from_str(PY_JOB).unwrap();
        assert_eq!(j.status, STOPPED);
        assert_eq!(j.settings.max_attempts, 3); // default filled in
        assert_eq!(j.settings.extra["future_flag"], 7);
        assert_eq!(j.frame_list().unwrap(), vec![1, 2, 3, 4]);
        assert_eq!(j.remaining_frames().unwrap(), vec![3, 4]);
        let back: Value = serde_json::to_value(&j).unwrap();
        assert_eq!(back["something_new"]["a"], 1);
        assert_eq!(back["frames"]["2"]["attempts"], 3);
    }

    #[test]
    fn save_load_and_verify_missing_frames() {
        let dir = tempfile::tempdir().unwrap();
        let mut j: Job = serde_json::from_str(PY_JOB).unwrap();
        j.folder = dir.path().to_string_lossy().into_owned();
        j.save().unwrap();
        // frame 1 is "done" but its file does not exist -> back to pending
        let mut l = Job::load(dir.path()).unwrap();
        assert_eq!(l.verify_done_frames(), 1);
        assert_eq!(l.frames["1"].status, F_PENDING);
        std::fs::write(dir.path().join("frames/shot_010_0001.png"), b"x").unwrap();
        l.frames.get_mut("1").unwrap().status = F_DONE.into();
        assert_eq!(l.verify_done_frames(), 0);
    }

    #[test]
    fn frame_names_match_python() {
        assert_eq!(frame_filename("shot_", 7, ".png"), "shot_0007.png");
        assert_eq!(frame_filename("shot_", -3, ".exr"), "shot_-0003.exr");
    }
}
