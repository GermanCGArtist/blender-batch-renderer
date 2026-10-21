//! App data directory, JSON helpers with atomic writes.

use anyhow::Result;
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};

/// Where queue / installs / settings live (same layout as the Python version).
pub fn app_data_dir() -> PathBuf {
    if let Ok(p) = std::env::var("BBR_DATA_DIR") {
        return PathBuf::from(p);
    }
    if cfg!(target_os = "macos") {
        if let Ok(home) = std::env::var("HOME") {
            return Path::new(&home)
                .join("Library")
                .join("Application Support")
                .join("BlenderBatchRenderer");
        }
    } else if cfg!(windows) {
        if let Ok(appdata) = std::env::var("APPDATA") {
            return Path::new(&appdata).join("BlenderBatchRenderer");
        }
    } else if let Ok(xdg) = std::env::var("XDG_DATA_HOME") {
        return Path::new(&xdg).join("BlenderBatchRenderer");
    } else if let Ok(home) = std::env::var("HOME") {
        return Path::new(&home)
            .join(".local")
            .join("share")
            .join("BlenderBatchRenderer");
    }
    PathBuf::from(".")
}

pub fn read_json(path: &Path) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

/// Write JSON atomically: temp file first, then rename over the target.
pub fn atomic_write_json(path: &Path, value: &Value) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("tmp");
    let data = serde_json::to_string_pretty(value)?;
    fs::write(&tmp, data)?;
    fs::rename(&tmp, path)?;
    Ok(())
}