"""Starts the app with the newest installed app code ("shell" side – never updated itself).

The built app (the shell) contains Python, Qt and the libraries plus a built-in copy of the app
code. Updates install newer app code into the app data folder:

    <app data>/app/<version>-<commit>/bbr/...     one folder per installed version
    <app data>/app/state.json                     {"active": "...", "previous": "...", "starting": ...}

At start the launcher uses the active version if it fits this shell (SHELL_REQUIRED of the code
<= SHELL_VERSION here). If that version did not finish starting last time, it goes back to the
previous one (or the built-in code). Running from source (not built) ignores installed updates
unless BBR_USE_UPDATES=1.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import json
import os
import re
import sys
import time
from pathlib import Path

SHELL_VERSION = 1          # raise when the shell changes in a way new app code depends on


def data_dir() -> Path:
    override = os.environ.get("BBR_DATA_DIR")
    if override:
        p = Path(override)
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "BlenderBatchRenderer"
    elif sys.platform.startswith("win"):
        p = Path(os.environ.get("APPDATA", Path.home())) / "BlenderBatchRenderer"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "BlenderBatchRenderer"
    return p


def app_dir() -> Path:
    return data_dir() / "app"


def read_state() -> dict:
    try:
        with open(app_dir() / "state.json", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    d = app_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".state.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, d / "state.json")


def shell_required(code_root: Path) -> int:
    try:
        text = (code_root / "bbr" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return 10 ** 6
    m = re.search(r"^SHELL_REQUIRED\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else 10 ** 6     # code without the marker is older than updates


def usable(name: str) -> bool:
    if not name:
        return False
    root = app_dir() / name
    return (root / ".ok").exists() and shell_required(root) <= SHELL_VERSION


class _CodeFinder(importlib.abc.MetaPathFinder):
    """Makes `import bbr` load the installed version instead of the built-in one."""

    def __init__(self, root: str):
        self.root = root

    def find_spec(self, name, path=None, target=None):
        if name == "bbr":
            return importlib.machinery.PathFinder.find_spec(name, [self.root])
        if name.startswith("bbr."):
            return importlib.machinery.PathFinder.find_spec(name, path)
        return None


ACTIVE = {"name": "", "fallback_note": ""}


def choose() -> str:
    """Pick the code to run; returns the folder name ('' = built-in)."""
    if not getattr(sys, "frozen", False) and os.environ.get("BBR_USE_UPDATES") != "1":
        return ""
    st = read_state()
    name = st.get("active") or ""
    note = ""
    if name and st.get("starting") == name and st.get("start_failures", 0) >= 1:
        # it did not finish starting last time: go back
        note = f"Version {name} did not start properly – went back to the previous version."
        st["broken"] = sorted(set(st.get("broken", [])) | {name})
        name = st.get("previous") if usable(st.get("previous") or "") else ""
        st["active"], st["previous"] = name, ""
        st["start_failures"] = 0
    elif name and not usable(name):
        note = f"Version {name} does not fit this app – using the built-in version. Download the new app."
        name = ""
    if name:
        st["start_failures"] = st.get("start_failures", 0) + (1 if st.get("starting") == name else 0)
        st["starting"] = name
        st["started_at"] = time.time()
    try:
        write_state(st)
    except OSError:
        pass
    ACTIVE["name"], ACTIVE["fallback_note"] = name, note
    return name


def activate() -> None:
    name = choose()
    if name:
        root = str(app_dir() / name)
        sys.meta_path.insert(0, _CodeFinder(root))


def mark_started() -> None:
    """Called by the app once its window is up: this version starts fine."""
    st = read_state()
    if st.get("starting"):
        st.pop("starting", None)
        st["start_failures"] = 0
        try:
            write_state(st)
        except OSError:
            pass
