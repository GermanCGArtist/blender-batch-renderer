"""The ordered list of jobs. queue.json only stores the job folders in order;
each job's full state lives in its own job.json."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import List, Optional

from .blendfile import read_blend_version
from .models import Job, JobSettings, QUEUED, RENDERING, STOPPED, DONE, DONE_ERRORS, CANCELLED, FAILED
from .storage import app_data_dir, atomic_write_json, read_json


def _safe_name(s: str) -> str:
    s = re.sub(r"[^\w.\- ]+", "_", s).strip().replace(" ", "_")
    return s or "job"


class JobQueue:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else app_data_dir() / "queue.json"
        self.jobs: List[Job] = []
        self.lock = threading.RLock()

    # ---- persistence -----------------------------------------------------
    def load(self) -> List[str]:
        """Load all jobs. Returns warnings (missing folders etc.)."""
        warnings = []
        data = read_json(self.path, {}) or {}
        self.jobs = []
        for folder in data.get("jobs", []):
            j = Job.load(folder)
            if j is None:
                warnings.append(f"Job folder missing or unreadable, dropped from queue: {folder}")
                continue
            if j.status == RENDERING:          # app quit or crashed mid-render
                j.status = STOPPED
                j.status_note = "interrupted (app was closed)"
                j.save()
            self.jobs.append(j)
        return warnings

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, {"jobs": [j.folder for j in self.jobs]})

    def save_job(self, job: Job) -> None:
        with self.lock:
            job.save()

    # ---- editing ---------------------------------------------------------
    def find(self, job_id: str) -> Optional[Job]:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, blend_path: str, settings: JobSettings) -> Job:
        """Create (or re-attach to) the job folder for a .blend file."""
        blend_path = os.path.abspath(os.path.expanduser(blend_path))
        root = settings.render_root or os.path.join(os.path.dirname(blend_path), "render")
        settings.render_root = root
        base = _safe_name(Path(blend_path).stem)
        with self.lock:
            name, n = base, 1
            while True:
                folder = os.path.join(root, name)
                existing = Job.load(folder)
                in_queue = any(os.path.abspath(j.folder) == os.path.abspath(folder) for j in self.jobs)
                if existing and os.path.abspath(existing.blend_path) == blend_path and not in_queue:
                    # Same file rendered here before: re-attach so finished frames are kept.
                    existing.settings = settings
                    if existing.status in (DONE, DONE_ERRORS, CANCELLED, FAILED):
                        existing.status = QUEUED
                        existing.status_note = "re-added; finished frames are kept"
                    job = existing
                    break
                if not existing and not in_queue and not os.path.exists(os.path.join(folder, "job.json")):
                    job = Job(blend_path=blend_path, folder=folder, name=name, settings=settings)
                    break
                n += 1
                name = f"{base}_{n}"
            v = read_blend_version(blend_path)
            job.file_version = list(v) if v else None
            job.save()
            self.jobs.append(job)
            self.save()
            return job

    def remove(self, job: Job) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if j.id != job.id]
            self.save()

    def move(self, job: Job, delta: int) -> None:
        with self.lock:
            i = self.jobs.index(job)
            k = max(0, min(len(self.jobs) - 1, i + delta))
            self.jobs.insert(k, self.jobs.pop(i))
            self.save()

    def reset_job(self, job: Job, keep_frames: bool = True) -> None:
        """keep_frames=True: only retry failed frames. False: forget all progress
        (frame files stay on disk but will be overwritten)."""
        with self.lock:
            if keep_frames:
                for r in job.frames.values():
                    if r.get("status") != "done":
                        r["status"] = "pending"
                        r["attempts"] = 0
            else:
                job.frames = {}
                job.attempts = []
            job.load_failures = 0
            job.status = QUEUED
            job.status_note = ""
            job.finished = None
            job.save()
