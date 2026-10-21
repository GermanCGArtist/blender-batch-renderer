//! Finding Blender installs on the machine and probing their version.

use crate::storage::{app_data_dir, atomic_write_json, read_json};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Install {
    pub path: String,
    pub version: String,
}

/// Places Blender usually lives, per platform.
pub fn candidate_paths() -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    let home = std::env::var("HOME").unwrap_or_default();
    if cfg!(target_os = "macos") {
        for base in ["/Applications", &format!("{}/Applications", home)] {
            if let Ok(entries) = fs::read_dir(base) {
                for e in entries.flatten() {
                    let p = e.path().join("Contents/MacOS/blender");
                    if e.file_name().to_string_lossy().starts_with("Blender") && p.exists() {
                        out.push(p);
                    }
                }
            }
        }
    } else if cfg!(windows) {
        for base in [r"C:\Program Files\Blender Foundation", r"C:\Program Files (x86)\Blender Foundation"] {
            if let Ok(entries) = fs::read_dir(base) {
                for e in entries.flatten() {
                    let p = e.path().join("blender.exe");
                    if p.exists() {
                        out.push(p);
                    }
                }
            }
        }
        if let Ok(path) = std::env::var("PATH") {
            for dir in path.split(';') {
                let p = Path::new(dir).join("blender.exe");
                if p.exists() {
                    out.push(p);
                }
            }
        }
    } else {
        let fixed = [
            "/usr/bin/blender",
            "/usr/local/bin/blender",
            "/opt/blender/blender",
            "/snap/bin/blender",
        ];
        for f in fixed {
            if Path::new(f).exists() {
                out.push(PathBuf::from(f));
            }
        }
        if let Ok(entries) = fs::read_dir("/opt") {
            for e in entries.flatten() {
                let p = e.path().join("blender");
                if e.file_name().to_string_lossy().starts_with("blender") && p.exists() {
                    out.push(p);
                }
            }
        }
        for d in [
            format!("{}/.local/bin", home),
            format!("{}/bin", home),
            "/usr/local/blender",
        ] {
            let p = Path::new(&d).join("blender");
            if p.exists() {
                out.push(p);
            }
        }
        if let Ok(path) = std::env::var("PATH") {
            for dir in path.split(':') {
                let p = Path::new(dir).join("blender");
                if p.exists() && !out.contains(&p) {
                    out.push(p);
                }
            }
        }
    }
    out.sort();
    out.dedup();
    out
}

/// Run "blender --version" and parse the first line.
pub fn probe(path: &Path) -> Option<Install> {
    let out = Command::new(path)
        .arg("--version")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let first = text.lines().next()?;
    let version = first
        .replace("Blender ", "")
        .trim()
        .to_string();
    Some(Install {
        path: path.to_string_lossy().to_string(),
        version,
    })
}

/// Scan the usual locations, probe each, save to installs.json.
pub fn scan() -> Vec<Install> {
    let installs: Vec<Install> = candidate_paths()
        .iter()
        .filter_map(|p| probe(p))
        .collect();
    let _ = save_installs(&installs);
    installs
}

pub fn installs_file() -> PathBuf {
    app_data_dir().join("installs.json")
}

pub fn save_installs(installs: &[Install]) -> Result<()> {
    let v = serde_json::to_value(installs)?;
    atomic_write_json(&installs_file(), &v)
}

pub fn load_installs() -> Vec<Install> {
    read_json(&installs_file())
        .and_then(|v| serde_json::from_value(v).ok())
        .unwrap_or_default()
}

/// The blender executable to use: explicit choice, newest install, or "blender"
/// from PATH.
pub fn resolve_blender(choice: &str) -> PathBuf {
    if choice != "auto" && !choice.is_empty() {
        return PathBuf::from(choice);
    }
    let installs = load_installs();
    if let Some(best) = installs.iter().max_by(|a, b| a.version.cmp(&b.version)) {
        return PathBuf::from(&best.path);
    }
    PathBuf::from("blender")
}