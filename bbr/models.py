"""Job / settings data structures and their persistence.

Each job lives in its own folder:

    <render root>/<job name>/
        frames/          image sequence (+ compositor File Output sub-folders)
        preview/         <job name>.mp4
        data/            frames.csv, attempts.csv, report.xlsx, preflight.json
        logs/            attempt_NNN.log  (full Blender console per attempt)
        logs/crashes/    copies of logs + Blender crash reports of failed attempts
        logs/tmp/        Blender's temp folder (crash reports land here first)
        job.json         full job state - the source of truth for resuming
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Dict, List, Optional

from .storage import atomic_write_json, read_json, app_data_dir

# Job states
QUEUED = "queued"
RENDERING = "rendering"
STOPPED = "stopped"          # stopped by the user / app quit; resumable
DONE = "done"
DONE_ERRORS = "done (with failed frames)"
FAILED = "failed"            # file could not be loaded/rendered at all
CANCELLED = "cancelled"
ON_HOLD = "on hold"          # needs a decision (file changed / missing) - skipped by the batch

RUNNABLE = (QUEUED, STOPPED, RENDERING)

# Frame states
F_DONE = "done"
F_FAILED = "failed"          # gave up after max attempts
F_PENDING = "pending"        # had failed attempts but will be retried


@dataclass
class JobSettings:
    render_root: str = ""
    blender: str = "auto"                 # "auto" or the install path
    frame_start: Optional[int] = None     # None = use the file's setting
    frame_end: Optional[int] = None
    samples: Optional[int] = None
    resolution_pct: Optional[int] = None
    max_attempts: int = 3                 # per frame (and for loading the file)
    retry_delay_s: int = 30
    frame_timeout_min: float = 0          # 0 = no timeout
    make_preview: bool = True
    preflight: bool = False
    restart_every: int = 0                # restart Blender every N frames (0 = never)

    @classmethod
    def from_dict(cls, d: dict) -> "JobSettings":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


@dataclass
class AppSettings:
    installs: List[dict] = field(default_factory=list)   # BlenderInstall dicts
    defaults: dict = field(default_factory=lambda: asdict(JobSettings()))
    keep_awake: bool = True
    auto_resume: bool = False
    last_render_root: str = ""            # only used as the start folder of "Browse…" dialogs
    last_blend_dir: str = ""              # start folder of the "Add .blend…" dialog
    console_lines: int = 5000
    min_free_gb: float = 5.0              # stop the batch when the render disk gets this full
    current_queue_file: str = ""          # the .bbrqueue file the queue was last opened from / saved to
    recent_queue_files: List[str] = field(default_factory=list)
    api_enabled: bool = True              # local web API + status page
    api_port: int = 8642
    api_allow_lan: bool = False           # also reachable from other devices on the home network
    api_key: str = ""                     # needed for access from other devices
    settings_version: int = 1

    @property
    def path(self) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "AppSettings":
        d = read_json(app_data_dir() / "settings.json", {}) or {}
        names = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in d.items() if k in names})
        base = asdict(JobSettings())
        base.update(s.defaults or {})
        s.defaults = base
        changed = False
        if s.settings_version < 2:
            # v2: the default render folder is a "render" folder next to each .blend file
            s.defaults["render_root"] = ""
            s.settings_version = 2
            changed = True
        if not s.api_key:
            import secrets
            s.api_key = secrets.token_urlsafe(18)
            changed = True
        if changed:
            try:
                s.save()
            except OSError:
                pass
        if os.environ.get("BBR_API_PORT"):          # used by the build self-test
            s.api_port = int(os.environ["BBR_API_PORT"])
        return s

    def add_recent(self, path: str) -> None:
        path = os.path.abspath(path)
        self.recent_queue_files = [path] + [p for p in self.recent_queue_files if p != path]
        self.recent_queue_files = self.recent_queue_files[:10]

    def save(self) -> None:
        atomic_write_json(self.path, asdict(self))


def frame_filename(prefix: str, frame: int, ext: str) -> str:
    sign = "-" if frame < 0 else ""
    return f"{prefix}{sign}{abs(frame):04d}{ext}"


@dataclass
class Job:
    blend_path: str
    folder: str
    name: str
    settings: JobSettings = field(default_factory=JobSettings)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = QUEUED
    status_note: str = ""
    file_version: Optional[List[int]] = None
    blender_used: Optional[dict] = None       # install dict + reason
    scene_info: Optional[dict] = None         # reported by Blender on first run
    frames: Dict[str, dict] = field(default_factory=dict)   # str(frame) -> record
    attempts: List[dict] = field(default_factory=list)
    load_failures: int = 0
    preflight: Optional[dict] = None
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    preview_path: Optional[str] = None
    group: str = ""                           # queue folder id ("" = top level)
    enabled: bool = True                      # unchecked jobs are skipped by the batch
    blend_stamp: Optional[List[float]] = None # [mtime, size] of the .blend when frames were rendered

    # ---- folders -------------------------------------------------------
    @property
    def folder_p(self) -> Path:
        return Path(self.folder)

    @property
    def frames_dir(self) -> Path:
        return self.folder_p / "frames"

    @property
    def preview_dir(self) -> Path:
        return self.folder_p / "preview"

    @property
    def data_dir(self) -> Path:
        return self.folder_p / "data"

    @property
    def logs_dir(self) -> Path:
        return self.folder_p / "logs"

    @property
    def crash_dir(self) -> Path:
        return self.logs_dir / "crashes"

    @property
    def tmp_dir(self) -> Path:
        return self.logs_dir / "tmp"

    @property
    def job_file(self) -> Path:
        return self.folder_p / "job.json"

    @property
    def file_prefix(self) -> str:
        return f"{self.name}_"

    def ensure_folders(self) -> None:
        for d in (self.frames_dir, self.preview_dir, self.data_dir, self.logs_dir,
                  self.crash_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- frames --------------------------------------------------------
    def frame_list(self) -> Optional[List[int]]:
        si = self.scene_info
        if not si:
            return None
        start = si.get("frame_start")
        end = si.get("frame_end")
        step = max(1, int(si.get("frame_step", 1) or 1))
        if start is None or end is None:
            return None
        return list(range(int(start), int(end) + 1, step))

    def frame_path(self, frame: int) -> Path:
        ext = (self.scene_info or {}).get("file_extension", ".png")
        return self.frames_dir / frame_filename(self.file_prefix, frame, ext)

    def frame_file(self, frame: int, rec: Optional[dict] = None) -> Path:
        """Where a finished frame lives. Paths are stored relative to the job folder,
        so moving or renaming the render folder (or the drive) does not lose frames."""
        rec = rec if rec is not None else self.frames.get(str(frame), {})
        rel = rec.get("rel")
        if rel:
            return self.folder_p / rel
        f = rec.get("file")
        if f:
            p = Path(f)
            if p.exists():
                return p
            return self.frames_dir / p.name
        return self.frame_path(frame)

    def verify_done_frames(self) -> int:
        """Drop 'done' records whose file vanished or is empty. Returns count dropped."""
        dropped = 0
        for key, rec in list(self.frames.items()):
            if rec.get("status") == F_DONE:
                p = str(self.frame_file(int(key), rec))
                try:
                    ok = os.path.getsize(p) > 0
                except OSError:
                    ok = False
                if not ok:
                    rec["status"] = F_PENDING
                    dropped += 1
        return dropped

    def count(self, status: str) -> int:
        return sum(1 for r in list(self.frames.values()) if r.get("status") == status)

    @property
    def total_frames(self) -> Optional[int]:
        fl = self.frame_list()
        return len(fl) if fl is not None else None

    def skip_frames(self) -> List[int]:
        return [int(k) for k, r in list(self.frames.items()) if r.get("status") in (F_DONE, F_FAILED)]

    def remaining_frames(self) -> Optional[List[int]]:
        fl = self.frame_list()
        if fl is None:
            return None
        skip = set(self.skip_frames())
        return [f for f in fl if f not in skip]

    def done_records(self) -> List[dict]:
        return [dict(r, frame=int(k), path=str(self.frame_file(int(k), r)))
                for k, r in sorted(list(self.frames.items()), key=lambda kv: int(kv[0]))
                if r.get("status") == F_DONE]

    # ---- the .blend file -------------------------------------------------
    def current_blend_stamp(self) -> Optional[List[float]]:
        try:
            st = os.stat(self.blend_path)
        except OSError:
            return None
        return [round(st.st_mtime, 3), st.st_size]

    def blend_changed(self) -> bool:
        """True if the .blend was saved again after frames were rendered from it."""
        cur = self.current_blend_stamp()
        return bool(self.blend_stamp and cur and self.count(F_DONE)
                    and (abs(cur[0] - self.blend_stamp[0]) > 0.01 or cur[1] != self.blend_stamp[1]))

    # ---- estimates -----------------------------------------------------
    def avg_frame_seconds(self, last_n: int = 10) -> Optional[float]:
        recs = [r for r in self.done_records() if r.get("render_s")]
        recs.sort(key=lambda r: r.get("finished_at") or 0)
        recs = recs[-last_n:]
        if not recs:
            return None
        return sum(r["render_s"] for r in recs) / len(recs)

    def avg_frame_bytes(self) -> Optional[float]:
        recs = [r for r in self.done_records() if r.get("size")]
        if not recs:
            return None
        return sum(r["size"] for r in recs) / len(recs)

    def eta_seconds(self, current_frame_remaining: Optional[float] = None) -> Optional[float]:
        rem = self.remaining_frames()
        avg = self.avg_frame_seconds()
        if rem is None or avg is None:
            return None
        n = len(rem)
        if current_frame_remaining is not None and n > 0:
            return current_frame_remaining + (n - 1) * avg
        return n * avg

    def projected_bytes(self) -> Optional[float]:
        avg = self.avg_frame_bytes()
        total = self.total_frames
        if avg is None or total is None:
            return None
        return avg * (total - self.count(F_FAILED))

    def used_bytes(self) -> int:
        return sum(int(r.get("size") or 0) for r in self.done_records())

    # ---- persistence ---------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["settings"] = asdict(self.settings)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        names = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in names}
        d["settings"] = JobSettings.from_dict(d.get("settings") or {})
        return cls(**d)

    def save(self) -> None:
        self.ensure_folders()
        atomic_write_json(self.job_file, self.to_dict())

    @classmethod
    def load(cls, folder: str) -> Optional["Job"]:
        d = read_json(Path(folder) / "job.json")
        if not d:
            return None
        j = cls.from_dict(d)
        j.folder = str(folder)   # folder may have been moved
        return j
