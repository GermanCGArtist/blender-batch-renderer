"""Hands out frames to machines (this computer and remote workers).

Every frame that is being rendered somewhere is *leased* to one machine. A
lease stays valid while the machine sends heartbeats; when a machine goes
silent (crash, sleep, Wi-Fi drop) its leases expire and the frames go back
into the pool. Finished and failed frames are recorded in the job exactly like
local frames, plus the name of the machine.

The local renderer (runner.py) and remote workers (api.py → worker.py) use the
same scheduler, so this computer is simply one more machine.
"""
from __future__ import annotations

import platform
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional

from .models import (Job, RUNNABLE, RENDERING, STOPPED, F_DONE, F_FAILED, F_PENDING)

LEASE_SECONDS = 45            # a lease survives this long without a heartbeat
OFFLINE_AFTER = 20            # a machine counts as offline after this many seconds of silence
LOCAL_ID = "local"


def addon_key(module: str) -> str:
    """Extensions (Blender 4.2+) are named bl_ext.<repository>.<name>; the same add-on installed from
    disk on one computer and from a repository on another must still match."""
    parts = module.split(".")
    if len(parts) >= 3 and parts[0] == "bl_ext":
        return parts[-1].lower()
    return module.lower()


def local_machine_name() -> str:
    n = socket.gethostname().split(".")[0]
    return n or platform.node() or "This computer"


@dataclass
class Lease:
    job_id: str
    frame: int
    machine_id: str
    token: str
    started: float
    expires: float


@dataclass
class Machine:
    id: str
    name: str
    kind: str = "remote"                  # "local" or "remote"
    os: str = ""
    app_version: str = ""
    cpu: str = ""
    ram_gb: Optional[float] = None
    gpu: str = ""                         # filled from Blender's device report
    installs: List[dict] = field(default_factory=list)   # [{"version": "5.2", "label", "addons": {mod: {...}}}]
    enabled: bool = True                  # coordinator's checkbox
    last_seen: float = 0.0
    state: str = "idle"                   # idle / downloading / loading / rendering / error / offline
    note: str = ""
    live: dict = field(default_factory=dict)
    excluded_jobs: Dict[str, str] = field(default_factory=dict)   # job id -> reason (this batch)
    batch: dict = field(default_factory=lambda: {"frames": 0, "failed": 0, "render_s": 0.0, "bytes": 0})
    total: dict = field(default_factory=lambda: {"frames": 0, "failed": 0, "render_s": 0.0, "bytes": 0})

    @property
    def online(self) -> bool:
        return self.kind == "local" or (time.time() - self.last_seen) < OFFLINE_AFTER

    def to_public(self) -> dict:
        d = asdict(self)
        d["online"] = self.online
        d["excluded_jobs"] = dict(self.excluded_jobs)
        for k in ("batch", "total"):
            fr = d[k]["frames"]
            d[k]["avg_frame_s"] = (d[k]["render_s"] / fr) if fr else None
        return d


class Scheduler:
    def __init__(self, queue, settings, emit: Callable[[str, dict], None] = lambda k, p: None):
        self.queue = queue
        self.settings = settings
        self.emit = emit
        self.lock = threading.RLock()
        self.leases: Dict[tuple, Lease] = {}
        self.machines: Dict[str, Machine] = {}
        self.batch_running = False        # set by the runner
        self.draining = False             # "stop after frame": no new leases
        self.abort_gen = 0                # bumped by "stop now": workers drop everything
        self.aborted_jobs: Dict[str, float] = {}   # job id -> time (cancelled jobs)
        self.request_package: Optional[Callable[[Job], None]] = None   # runner builds packages
        self.on_job_complete: Optional[Callable[[Job], None]] = None   # runner finalizes jobs
        self.local_busy_job: Optional[str] = None
        self._finalizing = set()
        self.local = Machine(id=LOCAL_ID, name=local_machine_name(), kind="local",
                             os=f"{platform.system()} {platform.release()}", last_seen=time.time())
        self.machines[LOCAL_ID] = self.local
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._expire_loop, name="bbr-leases", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()

    # ---- machines -----------------------------------------------------------
    def register(self, info: dict) -> Machine:
        mid = str(info.get("id") or uuid.uuid4().hex[:12])
        with self.lock:
            m = self.machines.get(mid)
            if m is None:
                m = Machine(id=mid, name=str(info.get("name") or mid))
                saved = (self.settings.machines or {}).get(mid, {})
                m.enabled = saved.get("enabled", True)
                m.total.update(saved.get("total", {}))
                self.machines[mid] = m
            for k in ("name", "os", "app_version", "cpu", "ram_gb"):
                if info.get(k) is not None:
                    setattr(m, k, info[k])
            if info.get("installs") is not None:
                m.installs = list(info["installs"])
            m.last_seen = time.time()
            m.state = "idle"
            m.note = ""
        self.emit("machines", {})
        return m

    def set_enabled(self, mid: str, enabled: bool):
        with self.lock:
            m = self.machines.get(mid)
            if m:
                m.enabled = bool(enabled)
        self._persist()
        self.emit("machines", {})

    def forget(self, mid: str):
        with self.lock:
            if mid != LOCAL_ID:
                self.machines.pop(mid, None)
                for k in [k for k, l in self.leases.items() if l.machine_id == mid]:
                    self.leases.pop(k)
                (self.settings.machines or {}).pop(mid, None)
        self._persist()
        self.emit("machines", {})

    def heartbeat(self, mid: str, state: str = "", live: Optional[dict] = None, note: str = "") -> dict:
        now = time.time()
        with self.lock:
            m = self.machines.get(mid)
            if m is None:
                return {"register": True}
            m.last_seen = now
            if state:
                m.state = state
            m.note = note or ""
            if live is not None:
                m.live = live
                if live.get("device"):
                    m.gpu = live["device"]
            mine = [l for l in self.leases.values() if l.machine_id == mid]
            for l in mine:
                l.expires = now + LEASE_SECONDS
            return {
                "abort_gen": self.abort_gen,
                "abort_jobs": [jid for jid in self.aborted_jobs],
                "stop_after_frame": self.draining or not self.batch_running,
                "leases": [{"job_id": l.job_id, "frame": l.frame, "token": l.token} for l in mine],
            }

    def local_live(self, live: dict, state: str):
        with self.lock:
            self.local.live = live
            self.local.state = state
            self.local.last_seen = time.time()
            if live.get("device"):
                self.local.gpu = live["device"]

    # ---- leases ---------------------------------------------------------------
    def _leased_frames(self, job_id: str) -> set:
        return {f for (jid, f) in self.leases if jid == job_id}

    def leasable(self, job: Job) -> Optional[List[int]]:
        rem = job.remaining_frames()
        if rem is None:
            return None
        with self.lock:
            taken = self._leased_frames(job.id)
        return [f for f in rem if f not in taken]

    def outstanding(self, job_id: Optional[str] = None, exclude: Optional[str] = None) -> int:
        with self.lock:
            return sum(1 for l in self.leases.values()
                       if (job_id is None or l.job_id == job_id) and (exclude is None or l.machine_id != exclude))

    def _pick_frame(self, job: Job, machine: Machine) -> Optional[int]:
        free = self.leasable(job)
        if not free:
            return None
        # prefer frames that did not already fail on this machine
        for f in free:
            rec = job.frames.get(str(f)) or {}
            if machine.name not in (rec.get("failed_on") or []):
                return f
        return free[0]

    def _new_lease(self, job: Job, frame: int, mid: str) -> Lease:
        now = time.time()
        l = Lease(job.id, frame, mid, uuid.uuid4().hex[:10], now, now + LEASE_SECONDS)
        self.leases[(job.id, frame)] = l
        return l

    def lease_local(self, job: Job) -> Optional[int]:
        """Next frame of this job for this computer (None = nothing left for us)."""
        with self.lock:
            if self.draining or job.id in self.aborted_jobs:
                return None
            f = self._pick_frame(job, self.local)
            if f is None:
                return None
            self._new_lease(job, f, LOCAL_ID)
            return f

    def compatible(self, job: Job, m: Machine) -> Optional[str]:
        """None if the machine can render this job, otherwise the reason why not."""
        pkg = job.package or {}
        if not pkg.get("hash"):
            return "package not built yet"
        if job.id in m.excluded_jobs:
            return m.excluded_jobs[job.id]
        want = pkg.get("blender")
        inst = next((i for i in m.installs if i.get("version") == want), None)
        if inst is None:
            return f"needs Blender {want}"
        have = {addon_key(k) for k in (inst.get("addons") or {})}
        missing = [] if getattr(job.settings, "ignore_addons", False) else \
            [a for a in (pkg.get("addons_required") or []) if addon_key(a) not in have]
        if missing:
            names = {a["module"]: a.get("name") or a["module"] for a in pkg.get("addons", [])}
            return "missing add-on(s): " + ", ".join(names.get(a, a) for a in missing)
        allowed = getattr(job.settings, "machines", None)
        if allowed and m.name not in allowed:
            return "not allowed for this job"
        return None

    def lease_remote(self, mid: str) -> dict:
        """Find work for a remote machine. Returns {"lease": {...}} or {"lease": None, "reason": …}."""
        with self.lock:
            m = self.machines.get(mid)
            if m is None:
                return {"lease": None, "reason": "unknown machine – register first", "register": True}
            m.last_seen = time.time()
            if not self.batch_running:
                return {"lease": None, "reason": "the coordinator is not rendering"}
            if self.draining:
                return {"lease": None, "reason": "the coordinator is stopping"}
            if not m.enabled:
                return {"lease": None, "reason": "this machine is switched off on the coordinator"}
            reasons = []
            for job in self.queue.ordered_jobs():
                if job.status not in RUNNABLE or not self.queue.is_active(job) or job.id in self.aborted_jobs:
                    continue
                if job.blend_changed():
                    reasons.append(f"{job.name}: the .blend was saved again – decide on the coordinator")
                    continue
                pkg = job.package or {}
                if pkg.get("error") and pkg.get("blend_stamp") == job.current_blend_stamp():
                    reasons.append(f"{job.name}: could not be prepared for other machines")
                    continue
                if not pkg.get("hash") or pkg.get("blend_stamp") != job.current_blend_stamp():
                    if self.request_package:
                        self.request_package(job)
                    reasons.append(f"{job.name}: preparing the job for other machines")
                    continue
                why = self.compatible(job, m)
                if why:
                    reasons.append(f"{job.name}: {why}")
                    continue
                f = self._pick_frame(job, m)
                if f is None:
                    continue
                l = self._new_lease(job, f, mid)
                if not job.blend_stamp or not job.count(F_DONE):
                    job.blend_stamp = pkg.get("blend_stamp")
                if job.status != RENDERING:
                    job.status, job.status_note = RENDERING, ""
                    job.started = job.started or time.time()
                    job.save()
                    self.emit("job", {"job_id": job.id})
                s = job.settings
                return {"lease": {
                    "token": l.token, "job_id": job.id, "job": job.name, "frame": f,
                    "package": pkg["hash"], "package_size": pkg.get("size"), "blender": pkg.get("blender"),
                    "prefix": job.file_prefix,
                    "overrides": {"frame_start": s.frame_start, "frame_end": s.frame_end, "samples": s.samples,
                                  "resolution_pct": s.resolution_pct},
                    "frame_timeout_min": s.frame_timeout_min, "restart_every": s.restart_every,
                    "abort_gen": self.abort_gen,
                }}
            return {"lease": None, "reason": "; ".join(reasons) or "no frames left to hand out"}

    def next_frame_same_job(self, mid: str, job_id: str) -> Optional[int]:
        """A worker with Blender already open asks for another frame of the same job."""
        with self.lock:
            m = self.machines.get(mid)
            job = self.queue.find(job_id)
            if (m is None or job is None or not self.batch_running or self.draining or not m.enabled
                    or job.status not in RUNNABLE or not self.queue.is_active(job) or job.id in self.aborted_jobs
                    or self.compatible(job, m)):
                return None
            m.last_seen = time.time()
            f = self._pick_frame(job, m)
            if f is None:
                return None
            return self._new_lease(job, f, mid).frame

    def lease_token(self, job_id: str, frame: int, mid: str) -> Optional[str]:
        with self.lock:
            l = self.leases.get((job_id, int(frame)))
            return l.token if l and l.machine_id == mid else None

    def valid(self, job_id: str, frame: int, mid: str) -> bool:
        with self.lock:
            l = self.leases.get((job_id, int(frame)))
            return bool(l and l.machine_id == mid)

    def release(self, job_id: str, frame: int, mid: str) -> None:
        with self.lock:
            l = self.leases.get((job_id, int(frame)))
            if l and l.machine_id == mid:
                del self.leases[(job_id, int(frame))]

    def release_machine(self, mid: str, job_id: Optional[str] = None) -> List[int]:
        with self.lock:
            keys = [k for k, l in self.leases.items() if l.machine_id == mid and (job_id is None or k[0] == job_id)]
            for k in keys:
                del self.leases[k]
            return [k[1] for k in keys]

    # ---- results --------------------------------------------------------------
    def _machine(self, mid: str) -> Machine:
        return self.machines.get(mid) or self.local

    def frame_done(self, mid: str, job: Job, frame: int, render_s: Optional[float], size: Optional[int]):
        """Called after the frame record was written (locally or for an upload)."""
        with self.lock:
            self.leases.pop((job.id, int(frame)), None)
            m = self._machine(mid)
            for bucket in (m.batch, m.total):
                bucket["frames"] += 1
                bucket["render_s"] += float(render_s or 0)
                bucket["bytes"] += int(size or 0)
        self._persist()
        self.emit("machines", {})
        self.maybe_complete(job)

    def frame_failed(self, mid: str, job: Job, frame: int, error: str) -> str:
        """Count a failed attempt on a frame. Returns the frame's new status."""
        with self.lock:
            self.leases.pop((job.id, int(frame)), None)
            m = self._machine(mid)
            for bucket in (m.batch, m.total):
                bucket["failed"] += 1
            rec = job.frames.setdefault(str(frame), {"status": F_PENDING, "attempts": 0})
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            rec["last_error"] = f"[{m.name}] {error}"
            rec.setdefault("failed_on", [])
            if m.name not in rec["failed_on"]:
                rec["failed_on"].append(m.name)
            rec["status"] = F_FAILED if rec["attempts"] >= max(1, int(job.settings.max_attempts)) else F_PENDING
            job.save()
            status = rec["status"]
        self._persist()
        self.emit("machines", {})
        self.maybe_complete(job)
        return status

    def exclude(self, mid: str, job: Job, reason: str):
        """This machine cannot render this job (e.g. the package does not open there)."""
        with self.lock:
            m = self.machines.get(mid)
            if m:
                m.excluded_jobs[job.id] = reason
            self.release_machine(mid, job.id)
        self.emit("machines", {})

    def maybe_complete(self, job: Job) -> bool:
        """Finish the job when every frame is done/failed and nobody holds a lease on it."""
        with self.lock:
            if job.id in self._finalizing:
                return False
            rem = job.remaining_frames()
            if rem is None or rem or self.outstanding(job.id):
                return False
            if self.local_busy_job == job.id:
                return False          # the local renderer finishes it itself
            if job.status not in (RENDERING,) and not job.status.startswith("rendering"):
                return False
            self._finalizing.add(job.id)
        try:
            if self.on_job_complete:
                self.on_job_complete(job)
        finally:
            with self.lock:
                self._finalizing.discard(job.id)
        return True

    # ---- batch control -------------------------------------------------------
    def start_batch(self):
        with self.lock:
            self.batch_running = True
            self.draining = False
            self.aborted_jobs.clear()
            for m in self.machines.values():
                m.excluded_jobs.clear()
                m.batch = {"frames": 0, "failed": 0, "render_s": 0.0, "bytes": 0}
        self.emit("machines", {})

    def end_batch(self):
        with self.lock:
            self.batch_running = False
            self.draining = False
        self.emit("machines", {})

    def drain(self):
        with self.lock:
            self.draining = True

    def abort_all(self):
        """'Stop now': every machine drops its frame; nothing is counted as failed."""
        with self.lock:
            self.abort_gen += 1
            self.draining = True
            self.leases = {k: l for k, l in self.leases.items() if l.machine_id == LOCAL_ID}

    def abort_job(self, job_id: str):
        with self.lock:
            self.aborted_jobs[job_id] = time.time()
            self.leases = {k: l for k, l in self.leases.items() if not (k[0] == job_id and l.machine_id != LOCAL_ID)}

    def remote_working(self) -> bool:
        with self.lock:
            return any(l.machine_id != LOCAL_ID for l in self.leases.values())

    def remote_available(self) -> bool:
        """Is any remote machine online and switched on?"""
        with self.lock:
            return any(m.kind == "remote" and m.online and m.enabled for m in self.machines.values())

    # ---- estimates & statistics -------------------------------------------------
    def job_rates(self, job: Job) -> Dict[str, float]:
        """Frames per second per machine for this job, from its finished frames."""
        per: Dict[str, list] = {}
        for r in job.done_records():
            if r.get("render_s"):
                per.setdefault(r.get("machine") or self.local.name, []).append(r["render_s"])
        return {name: len(t) / sum(t) for name, t in per.items() if sum(t) > 0}

    def job_eta(self, job: Job, current_remaining: Optional[float] = None) -> Optional[float]:
        rem = job.remaining_frames()
        if rem is None:
            return None
        if not rem:
            return 0.0 if not self.outstanding(job.id) else None
        rates = self.job_rates(job)
        with self.lock:
            active = {m.name for m in self.machines.values() if m.online and m.enabled
                      and (m.kind == "local" or not self.compatible(job, m))}
        rate = sum(r for name, r in rates.items() if name in active) or sum(rates.values())
        if not rate:
            return None
        eta = len(rem) / rate
        if current_remaining is not None:
            eta = max(eta, current_remaining)
        return eta

    def job_machine_stats(self, job: Job) -> List[dict]:
        out: Dict[str, dict] = {}
        for k, r in list(job.frames.items()):
            name = r.get("machine") or (self.local.name if r.get("status") == F_DONE else None)
            if r.get("status") == F_DONE:
                d = out.setdefault(name, {"machine": name, "frames": 0, "render_s": 0.0, "failed_attempts": 0})
                d["frames"] += 1
                d["render_s"] += float(r.get("render_s") or 0)
            for fm in r.get("failed_on") or []:
                d = out.setdefault(fm, {"machine": fm, "frames": 0, "render_s": 0.0, "failed_attempts": 0})
                d["failed_attempts"] += 1
        total = sum(d["frames"] for d in out.values()) or 1
        for d in out.values():
            d["avg_frame_s"] = d["render_s"] / d["frames"] if d["frames"] else None
            d["share"] = d["frames"] / total
        return sorted(out.values(), key=lambda d: -d["frames"])

    def machines_public(self) -> dict:
        with self.lock:
            ms = [m.to_public() for m in self.machines.values()]
        comb = {k: sum(m["batch"][k] for m in ms) for k in ("frames", "failed", "render_s", "bytes")}
        tot = {k: sum(m["total"][k] for m in ms) for k in ("frames", "failed", "render_s", "bytes")}
        for d in (comb, tot):
            d["avg_frame_s"] = d["render_s"] / d["frames"] if d["frames"] else None
        online = [m for m in ms if m["online"] and m["enabled"]]
        return {"machines": ms, "combined": {"batch": comb, "total": tot, "online": len(online),
                                             "rendering": sum(1 for m in ms if m["state"] == "rendering")},
                "leases": len(self.leases), "batch_running": self.batch_running, "draining": self.draining}

    # ---- housekeeping ----------------------------------------------------------
    def _persist(self):
        try:
            with self.lock:
                self.settings.machines = {
                    mid: {"name": m.name, "enabled": m.enabled, "total": dict(m.total)}
                    for mid, m in self.machines.items() if m.kind == "remote"}
            self.settings.save()
        except Exception:
            pass

    def _expire_loop(self):
        while not self._stop.wait(2.0):
            now = time.time()
            expired = []
            with self.lock:
                for k, l in list(self.leases.items()):
                    if l.machine_id != LOCAL_ID and l.expires < now:
                        expired.append(l)
                        del self.leases[k]
                for m in self.machines.values():
                    if m.kind == "remote" and not m.online and m.state != "offline":
                        m.state = "offline"
            for l in expired:
                m = self.machines.get(l.machine_id)
                self.emit("message", {"level": "warn", "job_id": l.job_id,
                                      "text": f"{m.name if m else l.machine_id} stopped answering – frame {l.frame} "
                                              "goes back into the pool"})
            if expired:
                self.emit("machines", {})
