//! Crash-safe file writes and app data locations.
use std::io::Write;
use std::path::{Path, PathBuf};

/// Per-user folder for settings.json and queue.json (same locations as the Python app).
pub fn app_data_dir() -> std::io::Result<PathBuf> {
    let p = if let Some(o) = std::env::var_os("BBR_DATA_DIR") {
        PathBuf::from(o)
    } else if cfg!(target_os = "macos") {
        home().join("Library/Application Support/BlenderBatchRenderer")
    } else if cfg!(windows) {
        std::env::var_os("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(home)
            .join("BlenderBatchRenderer")
    } else {
        std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join(".local/share"))
            .join("BlenderBatchRenderer")
    };
    std::fs::create_dir_all(&p)?;
    Ok(p)
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_default()
}

/// Write to a temp file in the same folder, fsync, then rename over the target.
/// A crash leaves either the old or the new file, never a half-written one.
pub fn atomic_write(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let dir = path.parent().unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(dir)?;
    let mut tmp = tempfile::Builder::new().prefix(".tmp_").tempfile_in(dir)?;
    tmp.write_all(bytes)?;
    tmp.as_file().sync_all()?;
    tmp.persist(path).map_err(|e| e.error)?;
    Ok(())
}

pub fn atomic_write_json<T: serde::Serialize>(path: &Path, value: &T) -> std::io::Result<()> {
    let text = serde_json::to_string_pretty(value)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    atomic_write(path, text.as_bytes())
}

/// Read JSON; `None` if the file is missing or unreadable (like the Python `read_json`).
pub fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Option<T> {
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}
