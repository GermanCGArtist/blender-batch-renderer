"""Checksums (SHA-256) of files, remembered so big files are only read again when they change."""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Optional

from .storage import app_data_dir, atomic_write_json, read_json

CHUNK = 4 << 20


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class HashCache:
    """path -> (size, mtime_ns, sha256). A file is hashed again only when its size or date changes."""

    def __init__(self, path=None):
        self.path = path or (app_data_dir() / "hash_cache.json")
        self._lock = threading.Lock()
        self._data = read_json(self.path, {}) or {}
        self._dirty = 0
        self._last_save = time.time()

    def lookup(self, path: str) -> Optional[str]:
        """The checksum if it is known and the file has not changed, else None (no reading)."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        with self._lock:
            e = self._data.get(os.path.abspath(path))
        if e and e[0] == st.st_size and e[1] == st.st_mtime_ns:
            return e[2]
        return None

    def get(self, path: str) -> str:
        path = os.path.abspath(path)
        known = self.lookup(path)
        if known:
            return known
        st = os.stat(path)
        digest = sha256_file(path)
        st2 = os.stat(path)
        if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise OSError(f"{path} changed while it was being read")
        with self._lock:
            self._data[path] = [st.st_size, st.st_mtime_ns, digest]
            self._dirty += 1
        if self._dirty >= 200 or time.time() - self._last_save > 30:
            self.save()
        return digest

    def save(self):
        with self._lock:
            if not self._dirty:
                return
            # forget entries of files that no longer exist (keeps the file small)
            if len(self._data) > 200_000:
                self._data = {p: e for p, e in self._data.items() if os.path.exists(p)}
            data = dict(self._data)
            self._dirty = 0
            self._last_save = time.time()
        try:
            atomic_write_json(self.path, data)
        except OSError:
            pass


_shared: Optional[HashCache] = None


def shared() -> HashCache:
    global _shared
    if _shared is None:
        _shared = HashCache()
    return _shared
