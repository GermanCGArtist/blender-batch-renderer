//! The ordered list of jobs, organised in optional folders.
//!
//! queue.json (in the app data dir) stores the render root, the job folders in
//! order and the folder groups. Each job's full state lives in its own
//! job.json. Render order = the order jobs appear in here: top-level jobs
//! first, then each folder's jobs top to bottom.

use crate::models::{Job, JobSettings, Status};
use crate::storage::{app_data_dir, atomic_write_json, read_json};
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn safe_name(s: &str) -> String {
    let cleaned: String = s
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '.' || c == '-' || c == ' ' {
                c
            } else {
                '_'
            }
        })
        .collect();
    let trimmed = cleaned.trim().replace(' ', "_");
    if trimmed.is_empty() {
        "job".to_string()
    } else {
        trimmed
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct QueueFolder {
    pub name: String,
    pub jobs: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct JobQueue {
    /// Directory the job folders are created in.
    #[serde(default)]
    pub root: String,
    /// Job folder paths in render order.
    #[serde(default)]
    pub jobs: Vec<String>,
    #[serde(default)]
    pub folders: Vec<QueueFolder>,
}

impl JobQueue {
    pub fn queue_file() -> PathBuf {
        app_data_dir().join("queue.json")
    }

    pub fn load() -> JobQueue {
        let mut q: JobQueue = read_json(&Self::queue_file())
            .and_then(|v| serde_json::from_value(v).ok())
            .unwrap_or_default();
        if q.root.is_empty() {
            q.root = default_root().to_string_lossy().to_string();
        }
        q
    }

    pub fn save(&self) -> Result<()> {
        let v = serde_json::to_value(self)?;
        atomic_write_json(&Self::queue_file(), &v)
    }

    /// Flat list of all job folders in render order.
    pub fn ordered_jobs(&self) -> Vec<String> {
        let mut all = self.jobs.clone();
        for f in &self.folders {
            all.extend(f.jobs.iter().cloned());
        }
        all
    }

    pub fn load_jobs(&self) -> Vec<Job> {
        self.ordered_jobs()
            .iter()
            .filter_map(|p| Job::load(Path::new(p)))
            .collect()
    }

    pub fn load_job(&self, name: &str) -> Option<Job> {
        self.load_jobs().into_iter().find(|j| j.name == name)
    }

    /// Create a new job folder under the root and add it to the queue.
    pub fn add(&mut self, blend: &Path, settings: JobSettings, folder_group: Option<&str>) -> Result<Job> {
        if !blend.exists() {
            bail!("file not found: {}", blend.display());
        }
        fs::create_dir_all(&self.root)?;
        let base = safe_name(
            blend
                .file_stem()
                .map(|s| s.to_string_lossy().to_string())
                .as_deref()
                .unwrap_or("job"),
        );
        let mut name = base.clone();
        let mut job_dir = Path::new(&self.root).join(&name);
        let mut i = 1;
        while job_dir.join("job.json").exists() {
            i += 1;
            name = format!("{}-{}", base, i);
            job_dir = Path::new(&self.root).join(&name);
        }
        let mut job = Job::new(&job_dir, blend, settings);
        job.ensure_folders();
        job.save()?;
        let entry = job_dir.to_string_lossy().to_string();
        match folder_group {
            Some(g) => {
                if let Some(f) = self.folders.iter_mut().find(|f| f.name == g) {
                    f.jobs.push(entry);
                } else {
                    self.folders.push(QueueFolder {
                        name: g.to_string(),
                        jobs: vec![entry],
                    });
                }
            }
            None => self.jobs.push(entry),
        }
        self.save()?;
        Ok(job)
    }

    pub fn remove(&mut self, name: &str) -> Result<bool> {
        let jobs = self.load_jobs();
        let Some(job) = jobs.iter().find(|j| j.name == name) else {
            return Ok(false);
        };
        let path = job.folder.clone();
        self.jobs.retain(|p| p != &path);
        for f in &mut self.folders {
            f.jobs.retain(|p| p != &path);
        }
        self.folders.retain(|f| !f.jobs.is_empty());
        self.save()?;
        let _ = fs::remove_dir_all(&path);
        Ok(true)
    }

    /// Put a job back in the queue for re-rendering.
    pub fn requeue(&mut self, name: &str, keep_frames: bool) -> Result<bool> {
        let jobs = self.load_jobs();
        let Some(mut job) = jobs.into_iter().find(|j| j.name == name) else {
            return Ok(false);
        };
        if keep_frames {
            for (_, r) in job.frames.iter_mut() {
                if r.status != "done" {
                    r.status = "pending".to_string();
                    r.attempts = 0;
                }
            }
        } else {
            job.frames.clear();
            job.attempts.clear();
            job.blend_stamp = None;
        }
        job.status = Status::Queued;
        job.status_note.clear();
        job.finished = None;
        job.save()?;
        Ok(true)
    }

    /// Enable/disable a job. Disabled jobs are skipped by the runner.
    pub fn set_enabled(&mut self, name: &str, enabled: bool) -> Result<bool> {
        let jobs = self.load_jobs();
        let Some(mut job) = jobs.into_iter().find(|j| j.name == name) else {
            return Ok(false);
        };
        if enabled {
            if job.status == Status::OnHold {
                job.status = Status::Queued;
                job.status_note.clear();
            }
        } else if job.status.runnable() {
            job.status = Status::OnHold;
            job.status_note = "disabled".to_string();
        }
        job.enabled = enabled;
        job.save()?;
        Ok(true)
    }

    /// The .blend was saved again after frames were rendered: accept the
    /// change, keep the finished frames and queue the job again.
    pub fn accept_changed_blend(&mut self, name: &str) -> bool {
        let Some(mut job) = self.load_job(name) else {
            return false;
        };
        job.blend_stamp = job.current_blend_stamp();
        job.status = Status::Queued;
        job.status_note.clear();
        job.save().is_ok()
    }

    /// Create a new empty folder group.
    pub fn new_folder(&mut self, name: &str) -> Result<()> {
        let name = safe_name(name);
        if self.folders.iter().any(|f| f.name == name) {
            bail!("folder already exists: {}", name);
        }
        self.folders.push(QueueFolder {
            name,
            jobs: Vec::new(),
        });
        self.save()
    }

    /// Move a job one position up (up = true) or down within its group.
    pub fn move_job(&mut self, name: &str, up: bool) -> Result<bool> {
        let Some(job) = self.load_job(name) else {
            return Ok(false);
        };
        let path = job.folder.clone();
        let swap = |list: &mut Vec<String>| -> bool {
            if let Some(i) = list.iter().position(|p| p == &path) {
                let j = if up {
                    i.saturating_sub(1)
                } else {
                    (i + 1).min(list.len() - 1)
                };
                if i != j {
                    list.swap(i, j);
                    return true;
                }
            }
            false
        };
        let moved = if self.jobs.iter().any(|p| p == &path) {
            swap(&mut self.jobs)
        } else {
            let mut moved = false;
            for f in &mut self.folders {
                if f.jobs.iter().any(|p| p == &path) {
                    moved = swap(&mut f.jobs);
                    break;
                }
            }
            moved
        };
        if moved {
            self.save()?;
        }
        Ok(moved)
    }

    /// Move a job into a folder group (None = top level).
    pub fn move_to_folder(&mut self, name: &str, folder: Option<&str>) -> Result<bool> {
        let Some(job) = self.load_job(name) else {
            return Ok(false);
        };
        let path = job.folder.clone();
        self.jobs.retain(|p| p != &path);
        for f in &mut self.folders {
            f.jobs.retain(|p| p != &path);
        }
        match folder {
            Some(g) => {
                if let Some(f) = self.folders.iter_mut().find(|f| f.name == g) {
                    f.jobs.push(path);
                } else {
                    self.folders.push(QueueFolder {
                        name: g.to_string(),
                        jobs: vec![path],
                    });
                }
            }
            None => self.jobs.push(path),
        }
        self.save()?;
        Ok(true)
    }
}

fn default_root() -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    app_data_dir().join(format!("renders-{}", unique))
}