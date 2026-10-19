"""Small helpers for crash-safe file writes and app data locations."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def app_data_dir() -> Path:
    """Per-user folder for settings.json and queue.json."""
    override = os.environ.get("BBR_DATA_DIR")
    if override:
        p = Path(override)
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "BlenderBatchRenderer"
    elif sys.platform.startswith("win"):
        p = Path(os.environ.get("APPDATA", Path.home())) / "BlenderBatchRenderer"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "BlenderBatchRenderer"
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_text(path: Path | str, text: str) -> None:
    """Write to a temp file in the same folder, then rename over the target.

    A crash or power loss mid-write leaves either the old or the new file,
    never a half-written one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=path.suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, default=str))


def read_json(path: Path | str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
