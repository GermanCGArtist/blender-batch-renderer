"""Parsing Blender's console output.

Lines from our own script start with 'BBR_<KIND> ' followed by JSON.
Blender's own status lines look like (Cycles / EEVEE, 2.9x - 5.x):

  Fra:1 Mem:21.30M (Peak 21.85M) | Time:00:00.53 | Remaining:00:01.23 | Mem:4.89M, Peak:4.89M | Scene, ViewLayer | Sample 16/128
  Fra:1 Mem:120.4M (Peak 150.2M) | Time:00:01.33 | Rendering 12 / 64 samples
"""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple

MARKER = "BBR_"

_TIME_RE = re.compile(r"\bTime:\s*([\d:.]+)")
_REMAIN_RE = re.compile(r"\bRemaining:\s*([\d:.]+)")
_SAMPLE_RE = re.compile(r"\bSample\s+(\d+)\s*/\s*(\d+)")
_EEVEE_RE = re.compile(r"Rendering\s+(\d+)\s*/\s*(\d+)\s+samples")
_FRA_RE = re.compile(r"^Fra:\s*(-?\d+)")
_MEM_RE = re.compile(r"(?:Peak[:\s]+)([\d.]+)\s*([KMG])", re.I)
_TILE_RE = re.compile(r"\bTile\s+(\d+)\s*/\s*(\d+)")


def parse_marker(line: str) -> Optional[Tuple[str, dict]]:
    if not line.startswith(MARKER):
        return None
    head, _, payload = line.partition(" ")
    kind = head[len(MARKER):]
    try:
        data = json.loads(payload) if payload.strip() else {}
    except ValueError:
        return None
    return kind, data


def parse_duration(s: str) -> Optional[float]:
    """'01:02:03.45' / '02:03.45' / '3.45' -> seconds"""
    try:
        parts = [float(p) for p in s.strip().split(":")]
    except ValueError:
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs


def _to_mb(value: str, unit: str) -> float:
    v = float(value)
    u = unit.upper()
    return v / 1024 if u == "K" else v * 1024 if u == "G" else v


def parse_status(line: str) -> Optional[dict]:
    """Return a dict with any of: frame, elapsed_s, remaining_s, sample, samples,
    tile, tiles, peak_mb. None if the line is not a status line."""
    if not line.startswith("Fra:"):
        return None
    out: dict = {}
    m = _FRA_RE.match(line)
    if m:
        out["frame"] = int(m.group(1))
    m = _TIME_RE.search(line)
    if m:
        out["elapsed_s"] = parse_duration(m.group(1))
    m = _REMAIN_RE.search(line)
    if m:
        out["remaining_s"] = parse_duration(m.group(1))
    m = _SAMPLE_RE.search(line) or _EEVEE_RE.search(line)
    if m:
        out["sample"], out["samples"] = int(m.group(1)), int(m.group(2))
    m = _TILE_RE.search(line)
    if m:
        out["tile"], out["tiles"] = int(m.group(1)), int(m.group(2))
    peaks = [_to_mb(v, u) for v, u in _MEM_RE.findall(line)]
    if peaks:
        out["peak_mb"] = max(peaks)
    return out


def split_lines(buffer: str):
    """Split on \\n and \\r. Returns (complete_lines, rest)."""
    parts = re.split(r"\r\n|\r|\n", buffer)
    return parts[:-1], parts[-1]


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "–"
    seconds = int(round(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_bytes(n: Optional[float]) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
