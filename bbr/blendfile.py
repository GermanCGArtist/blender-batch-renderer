"""Read the Blender version a .blend file was saved with, without opening Blender.

Header layouts:
  pre-5.0 : b"BLENDER" + pointer('_'/'-') + endian('v'/'V') + 3 digits  e.g. BLENDER-v402
  5.0+    : b"BLENDER" + 2-digit header size + '-' + 2-digit format + 'v' + 4 digits
            e.g. BLENDER17-01v0500
Files may be gzip (old) or zstd (3.0+) compressed.
"""
from __future__ import annotations

import gzip
import re
from typing import Optional, Tuple

_HEADER_RE = re.compile(rb"BLENDER(?:[_-][vV](\d{3})|(\d\d)-(\d\d)[vV](\d{4}))")

GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _decompress_head(path: str, n: int = 64) -> Optional[bytes]:
    with open(path, "rb") as f:
        magic = f.read(4)
        f.seek(0)
        if magic.startswith(b"BLENDER"[:4]):
            return f.read(n)
        if magic[:2] == GZIP_MAGIC:
            with gzip.GzipFile(fileobj=f) as g:
                return g.read(n)
        if magic == ZSTD_MAGIC:
            try:
                import zstandard  # type: ignore
                return zstandard.ZstdDecompressor().stream_reader(f).read(n)
            except ImportError:
                pass
            try:  # Python 3.14+
                from compression import zstd  # type: ignore
                return zstd.ZstdFile(f).read(n)
            except ImportError:
                return None
    return None


def read_blend_version(path: str) -> Optional[Tuple[int, int]]:
    """Return (major, minor) or None if unknown."""
    try:
        head = _decompress_head(path)
    except OSError:
        return None
    if not head:
        return None
    m = _HEADER_RE.match(head)
    if not m:
        return None
    digits = m.group(1) or m.group(4)
    v = int(digits)
    return v // 100, v % 100


def version_str(v: Optional[Tuple[int, ...]]) -> str:
    if not v:
        return "?"
    return ".".join(str(x) for x in v)
