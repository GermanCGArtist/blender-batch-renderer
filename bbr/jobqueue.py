"""The ordered list of jobs, organised in optional folders.

queue.json stores the folders and the job folders in order; each job's full
state lives in its own job.json. Render order = the order shown in the window:
jobs at the top level first, then each folder's jobs, top to bottom.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from pathlib import Path
from typing import List, Optional

from .blendfile import read_blend_version
from .models import (Job, JobSettings, QUEUED, RENDERING, STOPPED, DONE, DONE_ERRORS, CANCELLED, FAILED,
                     ON_HOLD, RUNNABLE)
from .storage import app_data_dir, atomic_write_json, read_json


QUEUE_FORMAT = "bbr-queue"
QUEUE_EXT = ".bbrqueue"


def _safe_name(s: str) -> str:
    s = re.sub(r"[^\w.\- ]+", "_", s).strip().replace(" ", "_")
    return s or "job"


class JobQueue:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else app_data_dir() / "queue.json"
        self.jobs: List[Job] = []
        self.groups: List[dict] = []     # {"id", "name", "enabled", "expanded", "render_root"}
        self.lock = threading.RLock()
        self.on_change = None            # called after every change of the queue's structure

    # ---- persistence -----------------------------------------------------
    def load(self) -> List[str]:
        """Load all jobs. Returns warnings (missing folders etc.)."""
        warnings = []
        data = read_json(self.path, {}) or {}
        self.groups = [dict({"enabled": True, "expanded": True, "render_root": ""}, **g)
                       for g in data.get("groups", [])]
        self.jobs = []
        for folder in data.get("jobs", []):
            j = Job.load(folder)
            if j is None:
                warnings.append(f"Job folder missing or unreadable, dropped from the queue: {folder} "
                                "(use 'Add existing job folder…' if you moved it)")
                continue
            if j.status == RENDERING:          # app quit or crashed mid-render
                j.status = STOPPED
                j.status_note = "interrupted (app was closed)"
                j.save()
            if j.group and not self.group(j.group):
                j.group = ""
            self.jobs.append(j)
        return warnings

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, {"groups": self.groups, "jobs": [j.folder for j in self.jobs]})
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # ---- queue files (.bbrqueue) -------------------------------------------
    def snapshot(self) -> dict:
        """What a queue file stores: folders, job folders in order, on/off switches.
        Used to save, and to tell whether the queue differs from the saved file."""
        with self.lock:
            return {
                "groups": [{k: g.get(k) for k in ("id", "name", "enabled", "render_root")} for g in self.groups],
                "jobs": [{"folder": os.path.abspath(j.folder), "group": j.group or "", "enabled": bool(j.enabled)}
                         for j in self.jobs],
            }

    def save_file(self, path: str) -> None:
        path = os.path.abspath(path)
        snap = self.snapshot()
        base = os.path.dirname(path)
        for e in snap["jobs"]:
            try:
                e["rel"] = os.path.relpath(e["folder"], base)
            except ValueError:          # different drive on Windows
                e["rel"] = None
        data = {"format": QUEUE_FORMAT, "version": 1, "app": "Blender Batch Renderer",
                "groups": [dict(g, expanded=next((x.get("expanded", True) for x in self.groups
                                                  if x["id"] == g["id"]), True)) for g in snap["groups"]],
                "jobs": snap["jobs"]}
        atomic_write_json(path, data)

    @staticmethod
    def read_file_snapshot(path: str) -> Optional[dict]:
        """The snapshot stored in a queue file (job folders resolved), or None."""
        d = read_json(path)
        if not d or d.get("format") != QUEUE_FORMAT:
            return None
        base = os.path.dirname(os.path.abspath(path))
        jobs = []
        for e in d.get("jobs", []):
            folder = e.get("folder") or ""
            if not os.path.exists(os.path.join(folder, "job.json")) and e.get("rel"):
                alt = os.path.normpath(os.path.join(base, e["rel"]))
                if os.path.exists(os.path.join(alt, "job.json")):
                    folder = alt         # queue file and render folders were moved together
            jobs.append({"folder": os.path.abspath(folder) if folder else folder,
                         "group": e.get("group", ""), "enabled": bool(e.get("enabled", True))})
        groups = [{k: g.get(k) for k in ("id", "name", "enabled", "render_root")} for g in d.get("groups", [])]
        return {"groups": groups, "jobs": jobs, "_expanded": {g.get("id"): g.get("expanded", True)
                                                           for g in d.get("groups", [])}}

    def load_file(self, path: str) -> List[str]:
        """Replace the queue with the contents of a queue file. Returns warnings."""
        snap = self.read_file_snapshot(path)
        if snap is None:
            raise ValueError("Not a Blender Batch Renderer queue file")
        warnings = []
        with self.lock:
            self.groups = [dict(g, expanded=snap["_expanded"].get(g["id"], True)) for g in snap["groups"]]
            self.jobs = []
            for e in snap["jobs"]:
                j = Job.load(e["folder"])
                if j is None:
                    warnings.append(f"Job folder not found: {e['folder']}")
                    continue
                if j.status == RENDERING:
                    j.status, j.status_note = STOPPED, "interrupted (app was closed)"
                j.group = e["group"] if self.group(e["group"]) else ""
                j.enabled = e["enabled"]
                j.save()
                self.jobs.append(j)
        self.save()
        return warnings

    def clear(self) -> None:
        """Empty queue (job folders stay on disk)."""
        with self.lock:
            self.jobs, self.groups = [], []
        self.save()

    def save_job(self, job: Job) -> None:
        with self.lock:
            job.save()

    # ---- folders ---------------------------------------------------------
    def group(self, gid: str) -> Optional[dict]:
        return next((g for g in self.groups if g["id"] == gid), None) if gid else None

    def add_group(self, name: str, render_root: str = "") -> dict:
        with self.lock:
            g = {"id": uuid.uuid4().hex[:10], "name": name or "Folder", "enabled": True,
                 "expanded": True, "render_root": render_root}
            self.groups.append(g)
            self.save()
            return g

    def remove_group(self, gid: str) -> None:
        """Delete a folder; its jobs move to the top level."""
        with self.lock:
            for j in self.jobs:
                if j.group == gid:
                    j.group = ""
                    j.save()
            self.groups = [g for g in self.groups if g["id"] != gid]
            self.save()

    def jobs_in(self, gid: str) -> List[Job]:
        return [j for j in self.jobs if (j.group or "") == gid]

    def ordered_jobs(self) -> List[Job]:
        """Render/display order."""
        with self.lock:
            out = self.jobs_in("")
            for g in self.groups:
                out += self.jobs_in(g["id"])
            return out

    def is_active(self, job: Job) -> bool:
        g = self.group(job.group)
        return job.enabled and (g is None or g.get("enabled", True))

    def next_runnable(self) -> Optional[Job]:
        for j in self.ordered_jobs():
            if j.status in RUNNABLE and self.is_active(j):
                return j
        return None

    def set_order(self, top_level: List[str], groups: List[tuple]) -> None:
        """Apply an order coming from the window's tree:
        top_level = job ids, groups = [(group id, [job ids])]."""
        with self.lock:
            by_id = {j.id: j for j in self.jobs}
            new_jobs, changed = [], []
            for jid in top_level:
                j = by_id.pop(jid, None)
                if j:
                    if j.group:
                        j.group = ""
                        changed.append(j)
                    new_jobs.append(j)
            gmap = {g["id"]: g for g in self.groups}
            new_groups = []
            for gid, jids in groups:
                if gid in gmap:
                    new_groups.append(gmap.pop(gid))
                for jid in jids:
                    j = by_id.pop(jid, None)
                    if j:
                        if j.group != gid:
                            j.group = gid
                            changed.append(j)
                        new_jobs.append(j)
            new_groups += list(gmap.values())
            new_jobs += list(by_id.values())
            self.groups, self.jobs = new_groups, new_jobs
            for j in changed:
                j.save()
            self.save()

    # ---- editing ---------------------------------------------------------
    def find(self, job_id: str) -> Optional[Job]:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, blend_path: str, settings: JobSettings, group: str = "") -> Job:
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
            job.group = group if self.group(group) else ""
            job.save()
            self.jobs.append(job)
            self.save()
            return job

    def add_existing(self, folder: str, group: str = "") -> Job:
        """Put a job folder (e.g. one that was moved) back into the queue."""
        folder = os.path.abspath(os.path.expanduser(folder))
        with self.lock:
            if any(os.path.abspath(j.folder) == folder for j in self.jobs):
                raise ValueError("This job is already in the queue")
            job = Job.load(folder)
            if job is None:
                raise ValueError("No job.json in this folder")
            job.settings.render_root = os.path.dirname(folder)
            if any(j.id == job.id for j in self.jobs):   # a copy of a queued job
                job.id = uuid.uuid4().hex[:12]
            if job.status == RENDERING:
                job.status = STOPPED
            job.group = group if self.group(group) else ""
            job.save()
            self.jobs.append(job)
            self.save()
            return job

    def remove(self, job: Job) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if j.id != job.id]
            self.save()

    def move(self, job: Job, delta: int) -> None:
        """Move a job up/down among the jobs of its own folder."""
        with self.lock:
            siblings = self.jobs_in(job.group or "")
            i = siblings.index(job)
            k = max(0, min(len(siblings) - 1, i + delta))
            if k == i:
                return
            other = siblings[k]
            a, b = self.jobs.index(job), self.jobs.index(other)
            self.jobs[a], self.jobs[b] = self.jobs[b], self.jobs[a]
            self.save()

    def move_group(self, gid: str, delta: int) -> None:
        with self.lock:
            idx = next(i for i, g in enumerate(self.groups) if g["id"] == gid)
            k = max(0, min(len(self.groups) - 1, idx + delta))
            self.groups.insert(k, self.groups.pop(idx))
            self.save()

    def set_group(self, job: Job, gid: str) -> None:
        with self.lock:
            job.group = gid if self.group(gid) else ""
            # move it to the end of its new folder
            self.jobs.remove(job)
            self.jobs.append(job)
            job.save()
            self.save()

    def set_enabled(self, job: Job, enabled: bool) -> None:
        with self.lock:
            job.enabled = bool(enabled)
            job.save()
        self.save()

    def set_group_enabled(self, gid: str, enabled: bool) -> None:
        g = self.group(gid)
        if g:
            g["enabled"] = bool(enabled)
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
                job.blend_stamp = None
            job.load_failures = 0
            job.status = QUEUED
            job.status_note = ""
            job.finished = None
            job.save()

    def accept_changed_blend(self, job: Job) -> None:
        """Continue a job on hold although its .blend changed since rendering began."""
        with self.lock:
            job.blend_stamp = job.current_blend_stamp()
            job.status, job.status_note = QUEUED, "continuing with the changed .blend"
            job.save()
