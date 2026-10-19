"""Everything the API can ask for or do, independent of HTTP and of Qt.

Methods return plain dicts (JSON-ready) or raise ApiError. In the window they
run on the GUI thread (see api.QtExecutor); on the command line they run
directly.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from . import __version__
from .blendfile import version_str
from .jobqueue import JobQueue
from .models import (AppSettings, Job, JobSettings, RUNNABLE, RENDERING, DONE, DONE_ERRORS, F_DONE, F_FAILED,
                     F_PENDING)
from .runner import Runner


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Controller:
    def __init__(self, queue: JobQueue, runner: Runner, settings: AppSettings):
        self.queue = queue
        self.runner = runner
        self.settings = settings
        self.on_changed: Optional[Callable[[], None]] = None      # refresh the window
        self.open_queue_file: Optional[Callable[[str], None]] = None
        self.queue_file_info: Optional[Callable[[], dict]] = None

    # ---- helpers ----------------------------------------------------------
    def _job(self, job_id: str) -> Job:
        j = self.queue.find(job_id)
        if not j:
            raise ApiError(404, f"no job with id {job_id}")
        return j

    def _changed(self):
        if self.on_changed:
            self.on_changed()

    def job_summary(self, j: Job) -> dict:
        g = self.queue.group(j.group)
        cur = self.runner.running and self.runner.current is j
        return {
            "id": j.id, "name": j.name, "status": j.status, "note": j.status_note,
            "enabled": j.enabled, "active": self.queue.is_active(j),
            "folder": {"id": g["id"], "name": g["name"]} if g else None,
            "blend": j.blend_path, "job_folder": j.folder,
            "file_version": version_str(j.file_version),
            "blender": (j.blender_used or {}).get("label"),
            "frames": {"done": j.count(F_DONE), "failed": j.count(F_FAILED), "total": j.total_frames},
            "progress": self.runner.job_progress(j),
            "eta_s": self.runner.job_eta(j) if j.status in RUNNABLE else None,
            "avg_frame_s": j.avg_frame_seconds(),
            "disk_used": j.used_bytes(), "disk_projected": j.projected_bytes(),
            "rendering": cur,
            "preview": j.preview_path if j.preview_path and os.path.exists(j.preview_path) else None,
            "last_frame": (sorted(j.done_records(), key=lambda r: r.get("finished_at") or 0)[-1]["frame"]
                           if j.count(F_DONE) else None),
        }

    # ---- reading ----------------------------------------------------------
    def status(self) -> dict:
        r = self.runner
        L = r.live
        known, unknown = r.batch_eta()
        jobs = self.queue.ordered_jobs()
        cur = r.current if r.running else None
        live = None
        if cur:
            live = {
                "job_id": cur.id, "job": cur.name, "phase": L.phase, "frame": L.frame,
                "sample": L.sample, "samples": L.samples, "frame_fraction": L.frame_fraction(),
                "frame_elapsed_s": (time.time() - L.frame_started) if L.frame_started else None,
                "frame_remaining_s": L.remaining_s, "memory_mb": L.rss_mb, "blender_peak_mb": L.peak_mb,
            }
        qf = self.queue_file_info() if self.queue_file_info else {"path": self.settings.current_queue_file}
        return {
            "app": "Blender Batch Renderer", "version": __version__, "time": time.time(),
            "running": r.running, "stopping": r.stopping, "keep_awake": r._keep.active,
            "live": live,
            "batch": {"jobs": len(jobs), "jobs_done": sum(1 for j in jobs if j.status in (DONE, DONE_ERRORS)),
                      "eta_s": known if (known or not unknown) else None, "jobs_without_estimate": unknown,
                      "finishes_at": (time.time() + known) if known else None},
            "queue_file": qf,
            "mode": self.settings.mode,
            "machines": self.machines() if self.settings.share_with_workers else None,
        }

    def machines(self) -> dict:
        return self.runner.scheduler.machines_public()

    def set_machine_enabled(self, mid: str, enabled: bool) -> dict:
        if mid not in self.runner.scheduler.machines:
            raise ApiError(404, f"no machine with id {mid}")
        self.runner.scheduler.set_enabled(mid, enabled)
        self._changed()
        return self.machines()

    def forget_machine(self, mid: str) -> dict:
        if mid == "local":
            raise ApiError(400, "this computer cannot be removed")
        self.runner.scheduler.forget(mid)
        self._changed()
        return self.machines()

    def queue_list(self) -> dict:
        return {
            "folders": [{"id": g["id"], "name": g["name"], "enabled": g.get("enabled", True),
                         "render_root": g.get("render_root", ""),
                         "jobs": [j.id for j in self.queue.jobs_in(g["id"])]} for g in self.queue.groups],
            "jobs": [self.job_summary(j) for j in self.queue.ordered_jobs()],
        }

    def job_detail(self, job_id: str) -> dict:
        j = self._job(job_id)
        d = self.job_summary(j)
        d.update({
            "settings": vars(j.settings).copy(),
            "scene": j.scene_info,
            "preflight": j.preflight,
            "problem_frames": [{"frame": int(k), "status": r.get("status"), "attempts": r.get("attempts"),
                                "error": r.get("last_error", "")}
                               for k, r in sorted(list(j.frames.items()), key=lambda kv: int(kv[0]))
                               if r.get("status") in (F_FAILED, F_PENDING)],
            "attempts": [{k: a.get(k) for k in ("n", "started", "ended", "exit_code", "reason",
                                                "frames_rendered", "blender")} for a in j.attempts],
            "by_machine": self.runner.scheduler.job_machine_stats(j),
            "frame_times": [{"frame": r["frame"], "render_s": r.get("render_s"), "machine": r.get("machine"),
                             "blender_peak_mb": r.get("blender_peak_mb"),
                             "process_peak_mb": r.get("process_peak_mb")} for r in j.done_records()],
        })
        return d

    def last_frame_path(self, job_id: str) -> Optional[str]:
        j = self._job(job_id)
        recs = sorted(j.done_records(), key=lambda r: r.get("finished_at") or 0)
        for r in reversed(recs):
            if os.path.exists(r["path"]):
                return r["path"]
        return None

    def console(self, since: int = 0, job_id: Optional[str] = None) -> dict:
        lines = [{"seq": s, "job_id": jid, "text": t} for s, jid, t in list(self.runner.console_tail)
                 if s > since and (job_id is None or jid == job_id)]
        return {"lines": lines[-400:], "last_seq": self.runner._console_seq}

    # ---- changing ---------------------------------------------------------
    def add_job(self, data: dict) -> dict:
        blend = os.path.abspath(os.path.expanduser(str(data.get("blend") or "")))
        if not blend.lower().endswith(".blend") or not os.path.isfile(blend):
            raise ApiError(400, f".blend file not found on this computer: {blend}")
        s = JobSettings.from_dict(self.settings.defaults)
        for k in ("frame_start", "frame_end", "samples", "resolution_pct", "max_attempts", "retry_delay_s",
                  "frame_timeout_min", "make_preview", "preflight", "restart_every", "blender"):
            if k in data and data[k] is not None:
                setattr(s, k, data[k])
        gid = ""
        folder = data.get("folder")
        if folder:
            g = self.queue.group(folder) or next((x for x in self.queue.groups if x["name"] == folder), None)
            if g is None:
                g = self.queue.add_group(str(folder))
            gid = g["id"]
        root = data.get("render_root")
        if root is None:
            g = self.queue.group(gid)
            root = (g or {}).get("render_root") or ""      # "" = render folder next to the .blend
        s.render_root = root
        j = self.queue.add(blend, s, group=gid)
        if data.get("start") and not self.runner.running:
            self.runner.start()
        self._changed()
        return self.job_summary(j)

    def start(self) -> dict:
        if not self.settings.installs:
            raise ApiError(409, "no Blender install configured")
        started = self.runner.start()
        self._changed()
        return {"ok": True, "started": started, "running": self.runner.running}

    def stop(self, mode: str = "after_frame") -> dict:
        if not self.runner.running:
            return {"ok": True, "running": False}
        if mode == "now":
            self.runner.stop_now()
        elif mode == "after_frame":
            self.runner.stop_after_frame()
        else:
            raise ApiError(400, "mode must be 'after_frame' or 'now'")
        self._changed()
        return {"ok": True, "mode": mode}

    def cancel_current(self) -> dict:
        if not (self.runner.running and self.runner.current):
            raise ApiError(409, "nothing is rendering")
        self.runner.cancel_current_job()
        return {"ok": True}

    def _not_rendering(self, j: Job):
        if self.runner.running and self.runner.current is j:
            raise ApiError(409, "this job is rendering – stop it first")
        if self.runner.scheduler.outstanding(j.id):
            raise ApiError(409, "other machines are rendering this job – stop the batch first")

    def retry(self, job_id: str) -> dict:
        j = self._job(job_id)
        self._not_rendering(j)
        self.queue.reset_job(j, keep_frames=True)
        self._changed()
        return self.job_summary(j)

    def reset(self, job_id: str) -> dict:
        j = self._job(job_id)
        self._not_rendering(j)
        self.queue.reset_job(j, keep_frames=False)
        self._changed()
        return self.job_summary(j)

    def set_enabled(self, job_id: str, enabled: bool) -> dict:
        j = self._job(job_id)
        self.queue.set_enabled(j, enabled)
        self._changed()
        return self.job_summary(j)

    def remove(self, job_id: str) -> dict:
        j = self._job(job_id)
        self._not_rendering(j)
        self.queue.remove(j)
        self._changed()
        return {"ok": True, "removed": job_id}

    def preview(self, job_id: str) -> dict:
        j = self._job(job_id)
        if not j.count(F_DONE):
            raise ApiError(409, "no finished frames yet")
        self.runner.make_preview(j)
        return {"ok": True}

    def open_queue(self, path: str) -> dict:
        if not self.open_queue_file:
            raise ApiError(501, "opening queue files is only possible in the window")
        if not os.path.isfile(path):
            raise ApiError(400, f"file not found: {path}")
        self.open_queue_file(path)
        return {"ok": True}
