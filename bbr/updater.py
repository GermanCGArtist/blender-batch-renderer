"""Updates of the app code from GitHub, without rebuilding the app.

Two channels:
  stable  the `release` branch – versions that were tried and approved
  alpha   the `main` branch    – every change as soon as it is made (experimental)

An update downloads only the app code of one commit (about 1-2 MB), checks every file against
the checksums GitHub keeps for that commit (git blob SHA-1), and installs it next to the current
version (see bbr_launcher.py). The previous version is kept for "Use previous version".
Nothing is installed while something renders; nothing is checked unless asked.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from . import __version__

REPO = "GermanCGArtist/blender-batch-renderer"
CHANNELS = {"stable": "release", "alpha": "main"}
API = os.environ.get("BBR_UPDATE_API", "https://api.github.com")
CODELOAD = os.environ.get("BBR_UPDATE_CODELOAD", "https://codeload.github.com")
INCLUDE = ("bbr/", "blender_addon/", "docs/Blender_Batch_Renderer_Documentation.pdf")


class UpdateError(Exception):
    pass


def _launcher():
    try:
        import bbr_launcher
        return bbr_launcher
    except ImportError:
        return None


def shell_version() -> int:
    L = _launcher()
    return L.SHELL_VERSION if L else 1


def _ctx():
    if os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "BlenderBatchRenderer-updater",
                                               "Accept": "application/vnd.github+json"})
    ctx = _ctx() if url.startswith("https:") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise UpdateError("GitHub is refusing requests for a while (too many checks) – try again later")
        raise UpdateError(f"GitHub answered {e.code} for {url}")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"cannot reach GitHub ({getattr(e, 'reason', e)})")


def _get_json(url: str):
    try:
        return json.loads(_get(url).decode("utf-8"))
    except ValueError:
        raise UpdateError("unexpected answer from GitHub")


def _version_tuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


# ---- what is running -------------------------------------------------------------------
def current() -> dict:
    """{"version", "sha", "channel", "name", "builtin", "note"} of the code that is running."""
    L = _launcher()
    name = L.ACTIVE["name"] if L else ""
    info = {"version": __version__, "sha": None, "channel": None, "name": name, "builtin": not name,
            "note": L.ACTIVE.get("fallback_note", "") if L else ""}
    if name:
        try:
            info.update(json.loads((L.app_dir() / name / "info.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    else:
        b = Path(__file__).resolve().parent / "_build.json"
        try:
            info.update({k: v for k, v in json.loads(b.read_text()).items() if k in ("sha", "channel")})
        except (OSError, ValueError):
            pass
    return info


def previous() -> Optional[str]:
    L = _launcher()
    if not L:
        return None
    st = L.read_state()
    prev = st.get("previous") or ""
    if st.get("active") and not prev:
        return "built-in"
    return prev or None


# ---- checking ----------------------------------------------------------------------------
def check(channel: str) -> dict:
    branch = CHANNELS.get(channel, "release")
    c = _get_json(f"{API}/repos/{REPO}/commits/{branch}")
    sha = c.get("sha") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise UpdateError(f"no '{branch}' version found on GitHub")
    import base64
    meta = _get_json(f"{API}/repos/{REPO}/contents/bbr/__init__.py?ref={sha}")
    try:
        init = base64.b64decode(meta.get("content") or "").decode("utf-8", "replace")
    except ValueError:
        raise UpdateError("unexpected answer from GitHub")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    req = re.search(r"^SHELL_REQUIRED\s*=\s*(\d+)", init, re.M)
    commit = c.get("commit") or {}
    cur = current()
    info = {
        "channel": channel, "branch": branch, "sha": sha, "version": m.group(1) if m else "?",
        "date": ((commit.get("committer") or {}).get("date") or ""),
        "message": (commit.get("message") or "").splitlines()[0][:200] if commit.get("message") else "",
        "shell_required": int(req.group(1)) if req else None,
    }
    info["fits"] = info["shell_required"] is not None and info["shell_required"] <= shell_version()
    info["is_current"] = bool(cur.get("sha")) and cur["sha"] == sha
    if info["is_current"]:
        info["newer"] = False
    elif channel == "stable":
        info["newer"] = _version_tuple(info["version"]) > _version_tuple(cur["version"]) or (
            _version_tuple(info["version"]) == _version_tuple(cur["version"]) and cur.get("channel") == "alpha")
    else:
        info["newer"] = True          # alpha: a different commit is newer by definition
    return info


def label(info: dict) -> str:
    return f"{info.get('version')}" + (" alpha" if info.get("channel") == "alpha" else "") + \
        (f" ({info['sha'][:7]})" if info.get("sha") else "")


# ---- installing ----------------------------------------------------------------------------
def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def install(info: dict, progress: Callable[[str], None] = lambda t: None) -> str:
    """Download, check and install one version. Returns its folder name."""
    L = _launcher()
    if L is None:
        raise UpdateError("updates work in the built app only (running from source: use git pull)")
    if not info.get("fits"):
        raise UpdateError("this version needs a newer app – download and build the app again")
    sha = info["sha"]
    progress("downloading …")
    blob = _get(f"{CODELOAD}/{REPO}/zip/{sha}", timeout=120)
    progress("checking …")
    tree = _get_json(f"{API}/repos/{REPO}/git/trees/{sha}?recursive=1")
    if tree.get("truncated"):
        raise UpdateError("GitHub sent an incomplete file list")
    want = {e["path"]: e["sha"] for e in tree.get("tree", []) if e.get("type") == "blob"
            and e["path"].startswith(INCLUDE) and "__pycache__" not in e["path"]}
    if "bbr/__init__.py" not in want:
        raise UpdateError("the download does not contain the app")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise UpdateError("the download is damaged – try again")
    name = f"{info['version']}-{sha[:7]}"
    root = L.app_dir()
    tmp = root / f".{name}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    got = set()
    try:
        for zi in z.infolist():
            if zi.is_dir():
                continue
            parts = zi.filename.split("/", 1)
            if len(parts) < 2:
                continue
            rel = parts[1]
            if rel not in want:
                continue
            if rel.startswith("/") or ".." in rel.split("/"):
                raise UpdateError("unsafe file name in the download")
            data = z.read(zi)
            if git_blob_sha(data) != want[rel]:
                raise UpdateError(f"{rel} does not match GitHub's checksum – not installed")
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            got.add(rel)
        missing = set(want) - got
        if missing:
            raise UpdateError(f"{len(missing)} file(s) missing in the download – not installed")
        if L.shell_required(tmp) > L.SHELL_VERSION:
            raise UpdateError("this version needs a newer app – download and build the app again")
        (tmp / "info.json").write_text(json.dumps({"version": info["version"], "sha": sha,
                                                   "channel": info["channel"], "installed_at": time.time()}))
        (tmp / ".ok").write_text("verified")
        final = root / name
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    st = L.read_state()
    if st.get("active") != name:
        st["previous"] = st.get("active") or ""
    st["active"] = name
    st.pop("starting", None)
    st["start_failures"] = 0
    st["broken"] = [b for b in st.get("broken", []) if b != name]
    L.write_state(st)
    _prune(L, st)
    progress("installed")
    return name


def _prune(L, st):
    keep = {st.get("active"), st.get("previous")}
    for d in sorted(L.app_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir() and d.name not in keep and not d.name.startswith("."):
            shutil.rmtree(d, ignore_errors=True)


def use_previous() -> str:
    """Go back to the version used before the last update. Returns its name ('' = built-in)."""
    L = _launcher()
    if L is None:
        raise UpdateError("not available when running from source")
    st = L.read_state()
    prev = st.get("previous") or ""
    if prev and not L.usable(prev):
        prev = ""
    st["active"], st["previous"] = prev, st.get("active") or ""
    st.pop("starting", None)
    st["start_failures"] = 0
    L.write_state(st)
    return prev


def restart_command() -> Optional[list]:
    """A command that starts the app again a moment after this one quit (None = start it yourself)."""
    if not getattr(sys, "frozen", False):
        return None
    exe = sys.executable
    if sys.platform == "darwin":
        app = exe
        while app and not app.endswith(".app"):
            app = os.path.dirname(app)
        if not app:
            return None
        return ["/bin/sh", "-c", f'sleep 2; open -n "{app}"']
    if sys.platform.startswith("win"):
        return ["cmd", "/c", f'timeout /t 2 /nobreak >nul & start "" "{exe}"']
    return ["/bin/sh", "-c", f'sleep 2; "{exe}" &']


def restart_later():
    import subprocess
    cmd = restart_command()
    if not cmd:
        return False
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x00000008 | 0x00000200     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    return True
