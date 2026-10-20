"""Asks Blender which scenes a .blend has (for the job node's scene sockets)."""
from __future__ import annotations

import os
import subprocess
from typing import Optional

from ..parse import parse_marker

SCENES_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "blender_scripts", "bbr_scenes.py")


def list_scenes(executable: str, blend: str, timeout: float = 120) -> Optional[dict]:
    """{"scenes": [{"name", "active", "frame_start", "frame_end", "view_layers", "cameras", ...}],
    "blender_version"} or None if Blender could not read the file."""
    cmd = [executable, "-b", blend, "--python-exit-code", "2", "--python", SCENES_SCRIPT]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW: no console window per probe
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=os.path.dirname(blend) or None, errors="replace", **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (p.stdout or "").splitlines():
        mk = parse_marker(line)
        if mk and mk[0] == "SCENES":
            return mk[1]
    return None
