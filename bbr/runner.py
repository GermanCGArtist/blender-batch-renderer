"""The batch engine. Runs jobs one at a time in a background thread.

Nothing here depends on Qt: the window talks to it through method calls and
receives events via the `emit(kind, payload)` callback, so the same engine
can be driven from the command line (see cli.py).
"""
from __future__ import annotations

import collections
import glob
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import reports
from .installs import BlenderInstall, choose_install
from .jobqueue import JobQueue
from .keepawake import KeepAwake
from .models import (AppSettings, Job, RUNNABLE, QUEUED, RENDERING, STOPPED, DONE, DONE_ERRORS,
                     FAILED, CANCELLED, ON_HOLD, F_DONE, F_FAILED, F_PENDING)
from .parse import parse_marker, parse_status, parse_stats, fmt_bytes
from .process import BlenderProcess
from .scheduler import Scheduler, LOCAL_ID, device_for
from .storage import atomic_write_json, free_bytes

DISK_FULL_HINTS = ("No space left", "Errno 28", "disk full", "ENOSPC")

SCRIPTS = Path(__file__).resolve().parent / "blender_scripts"
RENDER_SCRIPT = str(SCRIPTS / "bbr_render.py")
PACKAGE_SCRIPT = str(SCRIPTS / "bbr_package.py")
COMPARE_SCRIPT = str(SCRIPTS / "bbr_compare.py")
PREFLIGHT_SCRIPT = str(SCRIPTS / "bbr_preflight.py")
PREVIEW_SCRIPT = str(SCRIPTS / "bbr_preview.py")

Emit = Callable[[str, dict], None]


class Live:
    """What is happening right now (read by the window every second)."""

    def __init__(self):
        self.reset()

    def reset(self, job_id: Optional[str] = None):
        self.job_id = job_id
        self.phase = ""                 # "loading", "rendering", "preflight", "preview", "waiting"
        self.frame: Optional[int] = None
        self.frame_started: Optional[float] = None
        self.sample: Optional[int] = None
        self.samples: Optional[int] = None
        self.remaining_s: Optional[float] = None    # Blender's own estimate for this frame
        self.elapsed_s: Optional[float] = None
        self.peak_mb: Optional[float] = None        # Blender-reported peak, this frame
        self.rss_mb: Optional[float] = None
        self.wait_until: Optional[float] = None

    def frame_fraction(self) -> Optional[float]:
        if self.sample and self.samples:
            return min(1.0, self.sample / self.samples)
        if self.elapsed_s is not None and self.remaining_s:
            tot = self.elapsed_s + self.remaining_s
            return self.elapsed_s / tot if tot else None
        return None


def env_for(job: Job) -> dict:
    env = dict(os.environ)
    tmp = str(job.tmp_dir)
    env.update({"TMPDIR": tmp, "TEMP": tmp, "TMP": tmp, "PYTHONUNBUFFERED": "1"})
    return env


class Runner:
    def __init__(self, queue: JobQueue, settings: AppSettings, emit: Emit):
        self.queue = queue
        self.settings = settings
        self._emit_ext = emit
        self.console_tail = collections.deque(maxlen=400)   # (seq, job_id, line) for the API
        self._console_seq = 0
        self.live = Live()
        self.running = False
        self.current: Optional[Job] = None
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[BlenderProcess] = None
        self._stop_batch = False          # stop after the current job reacts
        self._stop_after_frame = False
        self._cancel_job = False
        self._stop_note = ""
        self._wake = threading.Event()
        self._keep = KeepAwake()
        self._preview_threads = {}
        self.scheduler = Scheduler(queue, settings, self.emit)
        self.scheduler.request_package = self.request_package
        self.scheduler.on_job_complete = self._complete_job_async
        self.scheduler.on_probe_uploaded = self._compare_probe
        self._packaging = {}          # job id -> thread
        self._finalize_lock = threading.Lock()

    def emit(self, kind: str, payload: dict) -> None:
        if kind == "console":
            self._console_seq += 1
            self.console_tail.append((self._console_seq, payload.get("job_id"), payload.get("text", "")))
        self._emit_ext(kind, payload)

    # ---- estimates --------------------------------------------------------
    def job_eta(self, job: Job) -> Optional[float]:
        """Seconds until the job is done (uses Blender's own estimate for the current frame,
        and the combined speed of all machines that render it)."""
        if any(r.get("machine") and r.get("machine") != self.scheduler.local.name
               for r in list(job.frames.values())) or self.scheduler.outstanding(job.id, exclude=LOCAL_ID):
            cur = self.live.remaining_s if (self.running and self.current is job) else None
            return self.scheduler.job_eta(job, cur)
        if self.running and self.current is job:
            L = self.live
            rem = None
            if L.frame is not None:
                if L.remaining_s is not None:
                    rem = L.remaining_s
                elif L.frame_started and job.avg_frame_seconds():
                    rem = max(0.0, job.avg_frame_seconds() - (time.time() - L.frame_started))
            return job.eta_seconds(rem)
        return job.eta_seconds()

    def job_progress(self, job: Job) -> Optional[float]:
        """0..1 including the fraction of the frame being rendered, None if unknown."""
        total = job.total_frames
        if not total:
            return None
        frac = (job.count(F_DONE) + job.count(F_FAILED)) / total
        if self.running and self.current is job and self.live.frame is not None:
            ff = self.live.frame_fraction()
            if ff:
                frac += ff / total
        return min(1.0, frac)

    def batch_eta(self):
        """(seconds for jobs with an estimate, number of jobs without one)."""
        known, unknown = 0.0, 0
        for j in self.queue.ordered_jobs():
            if j.status in RUNNABLE and self.queue.is_active(j):
                e = self.job_eta(j)
                if e is None:
                    unknown += 1
                else:
                    known += e
        return known, unknown

    # ---- public control ---------------------------------------------------
    def start(self) -> bool:
        if self.running:
            return False
        self._stop_batch = self._stop_after_frame = self._cancel_job = False
        self._stop_note = ""
        self.running = True
        self.scheduler.start_batch()
        if self.settings.share_with_workers and self.scheduler.remote_available():
            for j in self.queue.ordered_jobs():      # get the copies for the workers ready early
                if j.status in RUNNABLE and self.queue.is_active(j):
                    if not self.scheduler.package_ok(j):
                        self.request_package(j)
                        break                        # one at a time; the next ones follow on demand
        self._thread = threading.Thread(target=self._run_batch, name="bbr-runner", daemon=True)
        self._thread.start()
        return True

    def stop_now(self) -> None:
        """Kill Blender immediately; the current frame will be rendered again."""
        self._stop_batch = True
        self._stop_after_frame = False
        self.scheduler.abort_all()
        self._wake.set()
        if self._proc:
            self._proc.kill("stop")

    def stop_after_frame(self) -> None:
        """Let the current frame finish, then stop."""
        self._stop_batch = True
        self._stop_after_frame = True
        self.scheduler.drain()
        self._wake.set()
        if self._proc and self.live.phase != "rendering":
            self._proc.kill("stop")    # still loading the file - nothing to wait for

    def cancel_current_job(self) -> None:
        """Kill Blender, mark the current job cancelled, carry on with the next job."""
        self._cancel_job = True
        if self.current is not None:
            self.scheduler.abort_job(self.current.id)
        self._wake.set()
        if self._proc:
            self._proc.kill("cancel")

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def stopping(self) -> bool:
        return self.running and self._stop_batch

    # ---- helpers ----------------------------------------------------------
    def _msg(self, level: str, text: str, job: Optional[Job] = None):
        self.emit("message", {"level": level, "text": text, "job_id": job.id if job else None})

    def _job_changed(self, job: Job, save: bool = True):
        if save:
            self.queue.save_job(job)
        self.emit("job", {"job_id": job.id})

    def installs(self):
        return [BlenderInstall.from_dict(d) for d in self.settings.installs]

    def resolve_install(self, job: Job):
        insts = self.installs()
        if job.settings.blender and job.settings.blender != "auto":
            for i in insts:
                if i.path == job.settings.blender or i.executable == job.settings.blender:
                    return i, "chosen in job settings"
            return None, f"Blender install not found: {job.settings.blender}"
        fv = tuple(job.file_version) if job.file_version else None
        return choose_install(insts, fv)

    def _next_job(self) -> Optional[Job]:
        """Next job this computer should work on: frame range unknown yet, or frames left to lease."""
        if not self.settings.render_locally:
            return None
        for j in self.queue.ordered_jobs():
            if j.status not in RUNNABLE or not self.queue.is_active(j):
                continue
            if j.id in self.scheduler.aborted_jobs:
                continue
            if device_for(j, self.scheduler.local.name) == "off" and j.scene_info is not None:
                continue        # other machines only (frame range is known, nothing to load here)
            free = self.scheduler.leasable(j)
            if free is None or free:
                return j
        return None

    def _work_elsewhere(self) -> bool:
        """Are other machines still busy, or is there work only they can do?"""
        if self.scheduler.remote_working():
            return True
        if not (self.settings.share_with_workers and self.scheduler.remote_available()):
            return False
        for j in self.queue.ordered_jobs():
            if j.status in RUNNABLE and self.queue.is_active(j) and j.id not in self.scheduler.aborted_jobs:
                free = self.scheduler.leasable(j)
                if free is None or free:
                    return True
        return False

    # ---- disk space -------------------------------------------------------
    def _disk_ok(self, job: Job) -> bool:
        """False (and the batch is stopped) when the render drive is nearly full."""
        free = free_bytes(job.frames_dir)
        if free is None:
            return True
        need = max(float(self.settings.min_free_gb or 0) * 1024 ** 3, 3 * (job.avg_frame_bytes() or 0))
        if free >= need:
            return True
        self._disk_full(job, f"only {fmt_bytes(free)} free on the render drive")
        return False

    def _disk_full(self, job: Job, why: str):
        self._stop_batch = True
        self._stop_note = f"disk almost full – {why}. Free some space, then press Start"
        self._msg("error", f"{job.name}: stopping the batch, {why}", job)
        if self._proc:
            self._proc.kill("disk")

    # ---- batch loop -------------------------------------------------------
    def _run_batch(self):
        self.emit("batch", {"running": True})
        if self.settings.keep_awake:
            self._keep.start()
        try:
            while not self._stop_batch:
                job = self._next_job()
                if job is None:
                    if self._work_elsewhere() or (not self.settings.render_locally and self._has_open_work()):
                        self.live.reset()
                        self.live.phase = "other machines"
                        self._wake.clear()
                        self._wake.wait(1.0)
                        continue
                    break
                self._cancel_job = False
                try:
                    self._run_job(job)
                except Exception as e:      # a bug must not leave the job "rendering"
                    import traceback
                    job.status = STOPPED
                    job.status_note = f"internal error: {e!r}"
                    self._job_changed(job)
                    self._msg("error", f"{job.name}: internal error {e!r}\n{traceback.format_exc()}", job)
                    break
            # "stop after frame": let the other machines finish their frames too
            while self.scheduler.remote_working() and self.scheduler.draining:
                self.live.phase = "other machines finishing"
                self._wake.clear()
                self._wake.wait(1.0)
        finally:
            self._keep.stop()
            self.current = None
            self.live.reset()
            self.scheduler.end_batch()
            for j in self.queue.ordered_jobs():
                if j.status == RENDERING and not self.scheduler.outstanding(j.id):
                    if j.remaining_frames() == []:
                        self._finalize_job(j)
                    else:
                        j.status, j.status_note = STOPPED, self._stop_note or "stopped"
                        self._job_changed(j)
            self.running = False
            self._stop_after_frame = False
            self.emit("batch", {"running": False})

    def _has_open_work(self) -> bool:
        for j in self.queue.ordered_jobs():
            if j.status in RUNNABLE and self.queue.is_active(j) and j.id not in self.scheduler.aborted_jobs:
                free = j.remaining_frames()
                if free is None or free:
                    return True
        return False

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Returns False if interrupted by stop/cancel."""
        self.live.phase = "waiting"
        self.live.wait_until = time.time() + seconds
        self._wake.clear()
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_batch or self._cancel_job:
                return False
            self._wake.wait(min(1.0, end - time.time()))
        self.live.wait_until = None
        return not (self._stop_batch or self._cancel_job)

    def _run_job(self, job: Job):
        self.scheduler.local_busy_job = job.id
        try:
            self._run_job_inner(job)
        finally:
            self.scheduler.local_busy_job = None
            self.scheduler.release_machine(LOCAL_ID, job.id)

    def _run_job_inner(self, job: Job):
        self.current = job
        self.live.reset(job.id)
        job.ensure_folders()
        dropped = job.verify_done_frames()
        if dropped:
            self._msg("warn", f"{job.name}: {dropped} finished frame(s) missing on disk, they will be rendered again", job)
        if not os.path.exists(job.blend_path):
            job.status, job.status_note = ON_HOLD, f".blend file not found: {job.blend_path}"
            self._job_changed(job)
            self._msg("error", f"{job.name}: {job.status_note} – skipping it", job)
            return
        if job.blend_changed():
            job.status = ON_HOLD
            job.status_note = (".blend was saved again after frames were rendered – choose "
                               "'Continue with changed file' or 'Start job over'")
            self._job_changed(job)
            self._msg("warn", f"{job.name}: {job.status_note}. Skipping it for now.", job)
            return
        if not job.blend_stamp or not job.count(F_DONE):
            job.blend_stamp = job.current_blend_stamp()
        inst, why = self.resolve_install(job)
        if inst is None:
            job.status, job.status_note = FAILED, why
            self._job_changed(job)
            self._msg("error", f"{job.name}: {why}", job)
            return
        job.blender_used = dict(inst.to_dict(), reason=why)
        job.status, job.status_note = RENDERING, ""
        job.started = job.started or time.time()
        self._job_changed(job)
        self._msg("info", f"{job.name}: using {inst.label} ({why})", job)

        if job.settings.preflight and job.preflight is None:
            self._run_preflight(job, inst)
            if self._handle_interrupt(job):
                return

        while True:
            if self._handle_interrupt(job):
                return
            rem = self.scheduler.leasable(job)
            if rem is not None and not rem:
                break
            if not self._disk_ok(job):
                continue        # -> _handle_interrupt stops the job
            outcome = self._run_attempt(job, inst)
            if outcome == "complete":
                break
            if outcome == "chunk":
                continue        # planned Blender restart, not a failure
            if self._handle_interrupt(job):
                return
            if outcome == "failed":
                if job.status == FAILED:
                    self._finish_reports(job)
                    return
                delay = max(0, int(job.settings.retry_delay_s or 0))
                if delay:
                    self._msg("info", f"{job.name}: retrying in {delay}s", job)
                    if not self._interruptible_sleep(delay):
                        continue   # _handle_interrupt at loop top deals with it

        if job.remaining_frames() == [] and not self.scheduler.outstanding(job.id, exclude=LOCAL_ID):
            self._finalize_job(job, inst)
        else:
            job.status_note = "finishing on other machines"
            self._job_changed(job)

    def _finalize_job(self, job: Job, inst: Optional[BlenderInstall] = None):
        """All frames done or failed: set the final state, write reports, make the preview."""
        with self._finalize_lock:
            if job.status != RENDERING:
                return
            failed = job.count(F_FAILED)
            job.status = DONE_ERRORS if failed else DONE
            job.status_note = f"{failed} frame(s) failed" if failed else ""
            job.finished = time.time()
            self._job_changed(job)
        self._finish_reports(job)
        if job.settings.make_preview and job.count(F_DONE):
            if inst is None:
                inst, _ = self.resolve_install(job)
            if inst is not None:
                self.make_preview(job, inst, blocking=True)
        self._msg("info", f"{job.name}: {job.status}", job)

    def _complete_job_async(self, job: Job):
        threading.Thread(target=self._finalize_job, args=(job,), daemon=True).start()

    def _handle_interrupt(self, job: Job) -> bool:
        if self._cancel_job:
            job.status, job.status_note = CANCELLED, "cancelled by user"
            self._job_changed(job)
            self._finish_reports(job)
            self._cancel_job = False
            return True
        if self._stop_batch:
            job.status, job.status_note = STOPPED, self._stop_note or "stopped by user"
            self._job_changed(job)
            self._finish_reports(job)
            return True
        return False

    def _finish_reports(self, job: Job):
        try:
            reports.write_xlsx(job)
        except Exception as e:
            self._msg("warn", f"{job.name}: could not write report.xlsx ({e})", job)

    # ---- one Blender run --------------------------------------------------
    def _clean_tmp_frames(self, job: Job):
        for p in glob.glob(str(job.frames_dir / ".tmp_*")):
            try:
                os.remove(p)
            except OSError:
                pass

    def _run_attempt(self, job: Job, inst: BlenderInstall) -> str:
        n = len(job.attempts) + 1
        self._clean_tmp_frames(job)
        s = job.settings
        args = {
            "frames_dir": str(job.frames_dir),
            "prefix": job.file_prefix,
            "skip": job.skip_frames(),
            "dynamic": True,
            "max_frames": int(s.restart_every or 0),
            "overrides": {"frame_start": s.frame_start, "frame_end": s.frame_end,
                          "samples": s.samples, "resolution_pct": s.resolution_pct,
                          "device": device_for(job, self.scheduler.local.name)},
        }
        args_path = job.tmp_dir / f"args_{n:03d}.json"
        atomic_write_json(args_path, args)
        log_path = job.logs_dir / f"attempt_{n:03d}.log"
        cmd = [inst.executable, "-b", job.blend_path, "--python-exit-code", "2",
               "--python", RENDER_SCRIPT, "--", str(args_path)]
        attempt = {"n": n, "started": time.time(), "ended": None, "exit_code": None,
                   "reason": "", "log": str(log_path), "frames_rendered": 0,
                   "blender": inst.label, "peak_rss_mb": None}
        job.attempts.append(attempt)
        self._job_changed(job)

        state = {"all_done": False, "chunk_done": False, "error": "", "last_emit": 0.0}
        self.live.reset(job.id)
        self.live.phase = "loading"

        proc = BlenderProcess(cmd, env_for(job), str(log_path), lambda l: None,
                              cwd=os.path.dirname(job.blend_path) or None)
        timeout_s = float(s.frame_timeout_min or 0) * 60

        def watch():
            self.live.rss_mb = proc.rss_mb
            L = self.live
            self.scheduler.local_live({"job_id": job.id, "job": job.name, "frame": L.frame, "sample": L.sample,
                                       "samples": L.samples, "remaining_s": L.remaining_s, "mem_mb": L.rss_mb,
                                       "device": (job.scene_info or {}).get("device")},
                                      "rendering" if L.frame is not None else "loading")
            if timeout_s and self.live.frame_started and time.time() - self.live.frame_started > timeout_s:
                proc.kill("timeout")

        def on_line(line: str):
            self.emit("console", {"job_id": job.id, "text": line})
            mk = parse_marker(line)
            if mk:
                self._on_marker(job, attempt, proc, state, *mk)
                return
            st = parse_status(line)
            if st:
                self._apply_stats(job, st, state)
            elif line.startswith("Error") or "Traceback" in line:
                state["error"] = (state["error"] + "\n" + line).strip()[-2000:]

        proc.on_line = on_line
        proc.watch_hook = watch
        proc.interactive = True
        self._proc = proc
        # a stop/cancel may have arrived between the check and the spawn
        if self._cancel_job or (self._stop_batch and not self._stop_after_frame):
            self._proc = None
            job.attempts.pop()
            return "stopped"
        try:
            rc = proc.run()
        except OSError as e:
            rc, state["error"] = -999, f"could not start Blender: {e}"
        finally:
            self._proc = None
        attempt["ended"] = time.time()
        attempt["exit_code"] = rc
        attempt["peak_rss_mb"] = proc.rss_peak_total_mb
        reason = proc.kill_reason

        try:
            if reason == "disk" or any(h in state["error"] for h in DISK_FULL_HINTS):
                attempt["reason"] = "disk full"
                if reason != "disk":
                    self._disk_full(job, "Blender could not save a frame (disk full)")
                return "stopped"
            if reason == "stop" and job.remaining_frames() == []:
                attempt["reason"] = "completed (stop requested after last frame)"
                return "complete"
            if reason in ("stop", "cancel"):
                attempt["reason"] = "stopped by user" if reason == "stop" else "cancelled by user"
                return "stopped"
            if state["all_done"] and rc == 0:
                attempt["reason"] = "completed"
                return "complete"
            if state["chunk_done"] and rc == 0:
                attempt["reason"] = f"planned restart after {attempt['frames_rendered']} frames"
                return "chunk"
            # ---- failure ----
            why = "frame timeout" if reason == "timeout" else f"Blender exited with code {rc}"
            if state["error"]:
                why += f": {state['error'].splitlines()[-1][:300]}"
            attempt["reason"] = why
            self._collect_crash(job, n, log_path)
            f = self.live.frame
            if state.get("fatal"):
                job.status, job.status_note = FAILED, state["fatal"]
                self._msg("error", f"{job.name}: {state['fatal']}", job)
            elif f is not None:
                status = self.scheduler.frame_failed(LOCAL_ID, job, f, why)
                rec = job.frames[str(f)]
                if status == F_FAILED:
                    self._msg("error", f"{job.name}: frame {f} failed {rec['attempts']}x, skipping it ({why})", job)
                else:
                    self._msg("warn", f"{job.name}: frame {f} failed (attempt {rec['attempts']}/{s.max_attempts}): {why}", job)
                reports.append_frame(job, f, rec, n)
            elif attempt["frames_rendered"] == 0:
                job.load_failures += 1
                if job.load_failures >= max(1, int(s.max_attempts)):
                    job.status, job.status_note = FAILED, f"could not start rendering: {why}"
                    self._msg("error", f"{job.name}: gave up after {job.load_failures} failed starts ({why})", job)
                else:
                    self._msg("warn", f"{job.name}: Blender failed before rendering "
                                      f"({job.load_failures}/{s.max_attempts}): {why}", job)
            return "failed"
        finally:
            self.scheduler.release_machine(LOCAL_ID, job.id)
            reports.append_attempt(job, attempt)
            self.live.reset(job.id)
            self.scheduler.local_live({}, "idle")
            self._job_changed(job)

    def _apply_stats(self, job: Job, st: dict, state: dict):
        L = self.live
        L.sample = st.get("sample", L.sample)
        L.samples = st.get("samples", L.samples)
        if "remaining_s" in st:
            L.remaining_s = st["remaining_s"]
        if "elapsed_s" in st:
            L.elapsed_s = st["elapsed_s"]
        if "peak_mb" in st:
            L.peak_mb = max(L.peak_mb or 0, st["peak_mb"])
        now = time.time()
        if now - state["last_emit"] > 0.25:
            state["last_emit"] = now
            self.emit("progress", {"job_id": job.id})

    def _on_marker(self, job: Job, attempt: dict, proc: BlenderProcess, state: dict, kind: str, data: dict):
        L = self.live
        if kind == "NEED_FRAME":
            f = None
            if not (self._stop_after_frame or self._cancel_job or self._stop_batch):
                f = self.scheduler.lease_local(job)
            proc.send(f"FRAME {f}" if f is not None else "DONE")
        elif kind == "STATS":
            self._apply_stats(job, parse_stats(str(data.get("s", ""))), state)
        elif kind == "CHUNK_DONE":
            state["chunk_done"] = True
        elif kind == "JOB_INFO":
            job.scene_info = data
            self._job_changed(job)
        elif kind == "WARN":
            self._msg("warn", f"{job.name}: {data.get('text', '')}", job)
        elif kind == "FRAME_START":
            L.phase = "rendering"
            L.frame = int(data["frame"])
            L.frame_started = time.time()
            L.sample = L.samples = L.remaining_s = L.elapsed_s = L.peak_mb = None
            proc.reset_peak()
            self.emit("progress", {"job_id": job.id})
        elif kind == "FRAME_DONE":
            f = int(data["frame"])
            self.scheduler.lock.acquire()
            rec = job.frames.setdefault(str(f), {"attempts": 0})
            rec.update({
                "status": F_DONE,
                "attempts": int(rec.get("attempts", 0)) + 1,
                "render_s": data.get("render_s"),
                "save_s": data.get("save_s"),
                "file": data.get("file"),
                "rel": os.path.relpath(data["file"], job.folder) if data.get("file") else None,
                "size": data.get("size"),
                "finished_at": time.time(),
                "blender_peak_mb": data.get("peak_mb") or L.peak_mb,
                "process_peak_mb": proc.rss_peak_mb,
                "samples": data.get("samples") or L.samples,
                "stats": data.get("stats", "")[:300],
                "machine": self.scheduler.local.name,
            })
            rec.pop("last_error", None)
            try:
                self.queue.save_job(job)
            finally:
                self.scheduler.lock.release()
            attempt["frames_rendered"] += 1
            L.frame = None
            L.frame_started = None
            L.phase = "loading"
            reports.append_frame(job, f, rec, attempt["n"])
            self.scheduler.frame_done(LOCAL_ID, job, f, rec.get("render_s"), rec.get("size"))
            self.emit("frame_done", {"job_id": job.id, "frame": f})
            if not self._disk_ok(job):
                return
            if self._stop_after_frame or self._cancel_job:
                proc.kill("stop" if not self._cancel_job else "cancel")
        elif kind == "FRAME_ERROR":
            state["error"] = str(data.get("error", ""))[-2000:]
        elif kind == "FATAL":
            state["fatal"] = str(data.get("error", "fatal error"))
            state["error"] = state["fatal"]
        elif kind == "ALL_DONE":
            state["all_done"] = True

    def _collect_crash(self, job: Job, n: int, log_path: Path):
        try:
            job.crash_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(log_path, job.crash_dir / f"attempt_{n:03d}.log")
            for p in glob.glob(str(job.tmp_dir / "*.crash.txt")):
                shutil.move(p, str(job.crash_dir / f"attempt_{n:03d}_{os.path.basename(p)}"))
        except OSError:
            pass

    # ---- preflight --------------------------------------------------------
    def _run_preflight(self, job: Job, inst: BlenderInstall):
        self.live.phase = "preflight"
        result = {}

        def on_line(line):
            self.emit("console", {"job_id": job.id, "text": line})
            mk = parse_marker(line)
            if mk and mk[0] == "PREFLIGHT":
                result.update(mk[1])

        log_path = job.logs_dir / "preflight.log"
        cmd = [inst.executable, "-b", job.blend_path, "--python-exit-code", "2",
               "--python", PREFLIGHT_SCRIPT]
        proc = BlenderProcess(cmd, env_for(job), str(log_path), on_line,
                              cwd=os.path.dirname(job.blend_path) or None)
        self._proc = proc
        try:
            proc.run()
        except OSError as e:
            result = {"problems": [f"could not start Blender: {e}"]}
        finally:
            self._proc = None
        if proc.kill_reason:
            return
        job.preflight = result or {"problems": ["preflight produced no result, see logs/preflight.log"]}
        if result.get("scene_info"):
            job.scene_info = result["scene_info"]
        atomic_write_json(job.data_dir / "preflight.json", job.preflight)
        for p in job.preflight.get("problems", []):
            self._msg("warn", f"{job.name} preflight: {p}", job)
        self._job_changed(job)

    # ---- packages for other machines -----------------------------------------
    def request_package(self, job: Job):
        """Build the self-contained copy of a job that workers download (in the background)."""
        if not self.settings.share_with_workers:
            return
        pkg = job.package or {}
        if pkg.get("error") and pkg.get("blend_stamp") == job.current_blend_stamp():
            return                      # failed for this version of the file - don't retry endlessly
        t = self._packaging.get(job.id)
        if t and t.is_alive():
            return
        t = threading.Thread(target=self._build_package, args=(job,), name="bbr-package", daemon=True)
        self._packaging[job.id] = t
        t.start()

    def _build_package(self, job: Job):
        from .package import build_package
        inst, why = self.resolve_install(job)
        if inst is None:
            self._msg("error", f"{job.name}: cannot prepare it for other machines – {why}", job)
            return
        self._msg("info", f"{job.name}: preparing a copy for the other machines (linked files, textures)…", job)
        try:
            manifest = build_package(job, inst, lambda l: self.emit("console", {"job_id": job.id, "text": l}))
        except Exception as e:
            job.package = {"error": str(e), "blend_stamp": job.current_blend_stamp()}
            self.queue.save_job(job)
            self._msg("error", f"{job.name}: could not prepare it for other machines – {e}", job)
            return
        job.package = manifest
        self.scheduler.invalidate_package(job)
        if job.scene_info is None and manifest.get("scene_info"):
            job.scene_info = manifest["scene_info"]
        self._job_changed(job)
        warn = manifest.get("warnings") or []
        need = manifest.get("addons_required") or []
        per = manifest.get("stream_bytes_per_frame") or 0
        self._msg("info", f"{job.name}: ready for other machines – {manifest.get('files')} files, "
                          f"{fmt_bytes(manifest.get('size_upfront'))} before the first frame"
                          + (f", about {fmt_bytes(per)} per frame streamed" if per else "")
                          + (f", needs add-ons: {', '.join(need)}" if need else ""), job)
        if manifest.get("unbaked"):
            self._msg("warn", f"{job.name}: not shared with other computers – unbaked simulation: "
                              + "; ".join(manifest["unbaked"][:5]), job)
        for w in warn[:20]:
            self._msg("warn", f"{job.name}: {w}", job)

    # ---- consistency check ------------------------------------------------------
    def _compare_probe(self, job: Job, mid: str, probe_path: str, ref_path: str):
        threading.Thread(target=self._compare_probe_now, args=(job, mid, probe_path, ref_path),
                         name="bbr-compare", daemon=True).start()

    def _compare_probe_now(self, job: Job, mid: str, probe_path: str, ref_path: str):
        res = {"ok": None, "reason": "no Blender install for the comparison"}
        inst, _ = self.resolve_install(job)
        if inst is not None:
            args = {"a": ref_path, "b": probe_path}
            ap = job.tmp_dir / f"compare_{mid}.json"
            atomic_write_json(ap, args)
            out = {}

            def on_line(line):
                mk = parse_marker(line)
                if mk and mk[0] == "COMPARE":
                    out.update(mk[1])
            cmd = [inst.executable, "-b", "--factory-startup", "--python-exit-code", "2",
                   "--python", COMPARE_SCRIPT, "--", str(ap)]
            try:
                BlenderProcess(cmd, env_for(job), str(job.logs_dir / "compare.log"), on_line).run()
                res = out or {"ok": None, "reason": "Blender did not report a result"}
            except OSError as e:
                res = {"ok": None, "reason": str(e)}
        res["image"] = probe_path
        if res.get("ok") is None:
            self._msg("warn", f"{job.name}: could not compare the test frame ({res.get('reason')}) – "
                              "letting the computer join anyway", job)
            res["ok"] = True
        try:
            atomic_write_json(Path(probe_path).with_suffix(".json"), res)
        except OSError:
            pass
        self.scheduler.probe_result(job, mid, bool(res["ok"]), res)

    # ---- preview ----------------------------------------------------------
    def make_preview(self, job: Job, inst: Optional[BlenderInstall] = None, blocking: bool = False):
        """Encode the finished frames into preview/<name>.mp4 with Blender's video editor."""
        if job.id in self._preview_threads and self._preview_threads[job.id].is_alive():
            self._msg("info", f"{job.name}: preview is already being made", job)
            return
        if inst is None:
            inst, _ = self.resolve_install(job)
            if inst is None:
                self._msg("error", f"{job.name}: no Blender install for the preview", job)
                return

        def work():
            from .preview import build_preview
            self.emit("preview", {"job_id": job.id, "state": "started"})
            try:
                path = build_preview(job, inst, lambda l: self.emit("console", {"job_id": job.id, "text": l}))
                job.preview_path = path
                self.queue.save_job(job)
                self._msg("info", f"{job.name}: preview saved to {path}", job)
                self.emit("preview", {"job_id": job.id, "state": "done", "path": path})
            except Exception as e:
                self._msg("error", f"{job.name}: preview failed ({e})", job)
                self.emit("preview", {"job_id": job.id, "state": "failed"})

        if blocking:
            self.live.phase = "preview"
            work()
        else:
            t = threading.Thread(target=work, daemon=True)
            self._preview_threads[job.id] = t
            t.start()
