"""Finding Blender installs, reading their versions and matching them to files."""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Tuple

_VERSION_RE = re.compile(r"Blender\s+(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass
class BlenderInstall:
    path: str            # what the user picked (.app bundle, folder or executable)
    executable: str      # the actual binary
    version: Tuple[int, int, int]
    label: str           # e.g. "Blender 4.2.3 LTS"

    @property
    def version_short(self) -> str:
        return f"{self.version[0]}.{self.version[1]}"

    def to_dict(self):
        d = asdict(self)
        d["version"] = list(self.version)
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(d["path"], d["executable"], tuple(d["version"]), d.get("label", ""))


def resolve_executable(path: str) -> Optional[str]:
    """Turn an .app bundle / install folder / binary path into the binary."""
    p = Path(path).expanduser()
    candidates = []
    if p.suffix == ".app" or (p.is_dir() and (p / "Contents" / "MacOS").is_dir()):
        candidates += [p / "Contents" / "MacOS" / "Blender", p / "Contents" / "MacOS" / "blender"]
    if p.is_dir():
        candidates += [p / "blender.exe", p / "blender", p / "Blender"]
    else:
        candidates.append(p)
    for c in candidates:
        if c.is_file() and os.access(str(c), os.X_OK):
            return os.path.abspath(str(c))
    return None


def probe(path: str, timeout: float = 90) -> Optional[BlenderInstall]:
    exe = resolve_executable(path)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=timeout, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search(out)
    if not m:
        return None
    ver = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    first = next((l.strip() for l in out.splitlines() if l.strip().startswith("Blender")), f"Blender {ver}")
    return BlenderInstall(os.path.abspath(str(Path(path).expanduser())), exe, ver, first)


def default_search_paths() -> List[str]:
    found: List[str] = []
    if sys.platform == "darwin":
        for base in ("/Applications", str(Path.home() / "Applications")):
            found += sorted(glob.glob(os.path.join(base, "Blender*.app")))
            found += sorted(glob.glob(os.path.join(base, "Blender*", "Blender*.app")))
    elif sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            found += sorted(glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe")))
        steam = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                             "Steam", "steamapps", "common", "Blender", "blender.exe")
        if os.path.isfile(steam):
            found.append(steam)
    else:
        w = shutil.which("blender")
        if w:
            found.append(w)
        for pattern in ("/opt/blender*/blender", str(Path.home() / "blender*" / "blender"),
                        "/snap/bin/blender"):
            found += sorted(glob.glob(pattern))
    # de-duplicate, keep order
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def scan() -> List[BlenderInstall]:
    res = []
    for p in default_search_paths():
        inst = probe(p)
        if inst:
            res.append(inst)
    return res


def choose_install(installs: List[BlenderInstall], file_version: Optional[Tuple[int, int]]
                   ) -> Tuple[Optional[BlenderInstall], str]:
    """Pick the best install for a file. Returns (install, explanation)."""
    if not installs:
        return None, "No Blender installs configured"
    newest = max(installs, key=lambda i: i.version)
    if not file_version:
        return newest, f"File version unknown, using newest ({newest.version_short})"
    same = [i for i in installs if i.version[:2] == tuple(file_version)]
    if same:
        best = max(same, key=lambda i: i.version)
        return best, f"Exact match {best.version_short}"
    newer = [i for i in installs if i.version[:2] > tuple(file_version)]
    if newer:
        lowest = min(i.version[:2] for i in newer)
        best = max((i for i in newer if i.version[:2] == lowest), key=lambda i: i.version)
        return best, (f"No {file_version[0]}.{file_version[1]} install, "
                      f"using next newer {best.version_short}")
    return newest, (f"WARNING: file saved in {file_version[0]}.{file_version[1]}, newer than "
                    f"any install; using {newest.version_short} (may lose features)")
