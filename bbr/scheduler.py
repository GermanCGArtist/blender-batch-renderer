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

import os
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
SMART_ENABLED = False         # Smart transfer (frame files streamed) is switched off for now; tests turn it on


def addon_key(module: str) -> str:
    """Extensions (Blender 4.2+) are named bl_ext.<repository>.<name>; the same add-on installed from
    disk on one computer and from a repository on another must still match."""
    parts = module.split(".")
    if len(parts) >= 3 and parts[0] == "bl_ext":
        return parts[-1].lower()
    return module.lower()


def device_for(job, machine_name: str) -> str:
    """'gpu', 'cpu', 'off' or '' (= as saved in the file) for this machine and job."""
    d = (getattr(job.settings, "devices", None) or {}).get(machine_name, "")
    return d if d in ("gpu", "cpu", "off") else ""


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
    rate: float = 0.0                     # measured transfer speed to this machine (bytes/s), 0 = unknown
    key_hash: str = ""                    # SHA-256 of this machine's own key (given when it paired)
    protocol: int = 0
    excluded_jobs: Dict[str, str] = field(default_factory=dict)   # job id -> reason (this batch)
    batch: dict = field(default_factory=lambda: {"frames": 0, "failed": 0, "render_s": 0.0, "bytes": 0})
    total: dict = field(default_factory=lambda: {"frames": 0, "failed": 0, "render_s": 0.0, "bytes": 0})

    @property
    def online(self) -> bool:
        return self.kind == "local" or (time.time() - self.last_seen) < OFFLINE_AFTER

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("key_hash", None)
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
        self.commands: Dict[str, str] = {}       # machine id -> "pause" / "stop" / "resume" (next heartbeat)
        self.probes: Dict[tuple, dict] = {}       # (job id, machine id) -> {"frame", "ref", "started", "state"}
        self.on_probe_uploaded: Optional[Callable] = None    # runner compares the images
        self._pkg_ok: Dict[str, tuple] = {}
        self._finalizing = set()
        self.local = Machine(id=LOCAL_ID, name=local_machine_name(), kind="local",
                             os=f"{platform.system()} {platform.release()}", last_seen=time.time())
        self.machines[LOCAL_ID] = self.local
        # workers known from earlier sessions: listed (offline) until they connect again
        for mid, d in list((settings.machines or {}).items()):
            if not isinstance(d, dict) or mid == LOCAL_ID:
                continue
            m = Machine(id=mid, name=str(d.get("name") or mid), os=d.get("os", ""), gpu=d.get("gpu", ""),
                        installs=list(d.get("installs") or []), enabled=d.get("enabled", True),
                        state="offline", last_seen=0.0, key_hash=str(d.get("key_hash") or ""))
            m.total.update(d.get("total") or {})
            self.machines[mid] = m
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
                "command": self.commands.pop(mid, None),
                "abort_gen": self.abort_gen,
                "abort_jobs": [jid for jid in self.aborted_jobs],
                "stop_after_frame": self.draining or not self.batch_running,
                "leases": [{"job_id": l.job_id, "frame": l.frame, "token": l.token} for l in mine],
            }

    def command(self, mid: str, cmd: str) -> bool:
        """Ask a worker to 'pause' (finish its frame, then take no more), 'stop' (drop its frame at once –
        it goes to another computer) or 'resume'. Delivered with its next heartbeat (a few seconds)."""
        if cmd not in ("pause", "stop", "resume"):
            raise ValueError(cmd)
        with self.lock:
            m = self.machines.get(mid)
            if m is None or m.kind == "local":
                return False
            self.commands[mid] = cmd
            if cmd == "stop":
                # hand its frames out again right away; a frame it still delivers is accepted if nobody
                # else finished it first
                for k in [k for k, l in self.leases.items() if l.machine_id == mid]:
                    del self.leases[k]
            m.note = {"pause": "pausing after this frame", "stop": "stopping", "resume": "resuming"}[cmd]
        self.emit("machines", {})
        return True

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
        if machine.kind != "local" and self._others_finish_first(job, machine, len(free)):
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
            if self.draining or job.id in self.aborted_jobs or device_for(job, self.local.name) == "off":
                return None
            f = self._pick_frame(job, self.local)
            if f is None:
                return None
            self._new_lease(job, f, LOCAL_ID)
            return f

    def compatible(self, job: Job, m: Machine) -> Optional[str]:
        """None if the machine can render this job, otherwise the reason why not."""
        if device_for(job, m.name) == "off":
            return "switched off for this job"
        pkg = job.package or {}
        if not pkg.get("hash"):
            return "package not built yet"
        if pkg.get("unbaked") and m.kind != "local":
            return "unbaked simulation (%s) – bake it to share this job" % "; ".join(pkg["unbaked"][:2])
        ap = (job.approvals or {}).get(m.name)
        if (ap and ap.get("ok") is False and ap.get("version") == pkg.get("hash")
                and ap.get("device") == device_for(job, m.name) and job.settings.consistency_check):
            return ap.get("reason") or "frames look different from the other computers"
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
                if device_for(job, m.name) == "off":
                    reasons.append(f"{job.name}: switched off for this job")
                    continue
                pkg = job.package or {}
                if pkg.get("error") and pkg.get("blend_stamp") == job.current_blend_stamp():
                    reasons.append(f"{job.name}: could not be prepared for other machines")
                    continue
                if not self.package_ok(job):
                    if self.request_package:
                        self.request_package(job)
                    reasons.append(f"{job.name}: preparing the job for other machines")
                    continue
                why = self.compatible(job, m)
                if why:
                    reasons.append(f"{job.name}: {why}")
                    continue
                probe = None
                if job.settings.consistency_check:
                    st, probe = self._consistency(job, m)
                    if st == "wait":
                        reasons.append(f"{job.name}: {probe}")
                        continue
                if probe is not None:
                    f = probe
                    l = Lease(job.id, f, mid, uuid.uuid4().hex[:10], time.time(), time.time() + LEASE_SECONDS)
                else:
                    f = self._pick_frame(job, m)
                    if f is None:
                        tail = self._tail_reason(job, m)
                        if tail:
                            reasons.append(f"{job.name}: {tail}")
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
                                  "resolution_pct": s.resolution_pct, "device": device_for(job, m.name)},
                    "frame_timeout_min": s.frame_timeout_min, "restart_every": s.restart_every,
                    "abort_gen": self.abort_gen, "transfer": (s.transfer if s.transfer in ("smart", "full") else "full") if SMART_ENABLED else "full",
                    "probe": probe is not None,
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
            if job.settings.consistency_check and m.kind != "local":
                ap = (job.approvals or {}).get(m.name) or {}
                if not ap.get("ok") or ap.get("version") != (job.package or {}).get("hash"):
                    return None          # finish the check first
            f = self._pick_frame(job, m)
            if f is None:
                return None
            return self._new_lease(job, f, mid).frame

    # ---- job copy, transfer speed, "is it worth it" -------------------------------------------
    def package_ok(self, job: Job) -> bool:
        from .package import package_current
        now = time.time()
        c = self._pkg_ok.get(job.id)
        if c and now - c[0] < 5 and c[2] == (job.package or {}).get("hash"):
            return c[1]
        ok = package_current(job)
        self._pkg_ok[job.id] = (now, ok, (job.package or {}).get("hash"))
        return ok

    def invalidate_package(self, job: Job):
        self._pkg_ok.pop(job.id, None)

    def record_transfer(self, mid: str, nbytes: int, seconds: float):
        if nbytes < (1 << 20) or seconds <= 0:
            return
        with self.lock:
            m = self.machines.get(mid)
            if m:
                r = nbytes / seconds
                m.rate = r if not m.rate else 0.7 * m.rate + 0.3 * r

    def _others_finish_first(self, job: Job, m: Machine, n_free: int) -> bool:
        """True when the other computers working on this job would finish all remaining frames
        before this one finishes a single frame (render + sending its frame files)."""
        rates = self.job_rates(job)
        t_m = (1.0 / rates[m.name]) if rates.get(m.name) else None
        if t_m is None:
            return False                       # no experience yet: let it try
        pkg = job.package or {}
        if SMART_ENABLED and job.settings.transfer != "full" and pkg.get("stream_bytes_per_frame"):
            t_m += pkg["stream_bytes_per_frame"] / (m.rate or 20e6)
        others = 0.0
        for x in self.machines.values():
            if x.id == m.id or not x.online or not x.enabled or not rates.get(x.name):
                continue
            busy = (x.kind == "local" and self.local_busy_job == job.id) or any(
                l.machine_id == x.id and l.job_id == job.id for l in self.leases.values())
            if busy:
                others += rates[x.name]
        if not others:
            return False
        return n_free / others < t_m

    def _tail_reason(self, job: Job, m: Machine) -> Optional[str]:
        free = self.leasable(job)
        if free and self._others_finish_first(job, m, len(free)):
            return "the other computers finish the last frames sooner"
        return None

    # ---- consistency check ------------------------------------------------------------------
    def _consistency(self, job: Job, m: Machine):
        """('ok', None) / ('wait', reason) / ('probe', frame to render for the comparison)."""
        if m.kind == "local":
            return "ok", None
        pkg = job.package or {}
        ver = pkg.get("hash")
        dev = device_for(job, m.name)
        aps = job.approvals if job.approvals is not None else {}
        ap = aps.get(m.name) or {}
        if ap.get("ok") and ap.get("version") == ver and ap.get("device") == dev:
            return "ok", None
        pr = self.probes.get((job.id, m.id))
        if pr:
            return "wait", "checking that its frames match the other computers (%s)" % pr["state"]
        ref = next((n for n, a in aps.items() if a.get("reference") and a.get("version") == ver), None)
        if ref is None:
            done = sorted((r for r in job.done_records()), key=lambda r: r.get("finished_at") or 0)
            if done:
                ref = done[0].get("machine") or self.local.name
            elif (self.settings.render_locally and device_for(job, self.local.name) != "off"
                  and self.local_busy_job == job.id and not self.draining):
                return "wait", f"waiting for the first frame from {self.local.name} to compare with"
            else:
                ref = m.name                      # nobody else renders it: this one is the reference
            if True:
                aps[ref] = {"ok": True, "reference": True, "version": ver, "device": device_for(job, ref),
                            "at": time.time()}
                job.approvals = aps
                job.save()
            if ref == m.name:
                return "ok", None
        cands = [r for r in job.done_records() if (r.get("machine") or self.local.name) == ref
                 and r.get("path") and os.path.exists(r["path"])]
        if not cands:
            return "wait", f"waiting for a frame from {ref} to compare with"
        r = sorted(cands, key=lambda r: r.get("finished_at") or 0)[-1]
        self.probes[(job.id, m.id)] = {"frame": int(r["frame"]), "ref": ref, "ref_path": r["path"],
                                       "started": time.time(), "state": "rendering a test frame",
                                       "machine": m.name}
        self.emit("message", {"level": "info", "job_id": job.id,
                              "text": f"{job.name}: {m.name} first renders frame {r['frame']} to compare it "
                                      f"with {ref}'s frame"})
        return "probe", int(r["frame"])

    def probe_for(self, job_id: str, mid: str, frame: int) -> Optional[dict]:
        with self.lock:
            pr = self.probes.get((job_id, mid))
            return pr if pr and pr["frame"] == int(frame) else None

    def probe_result(self, job: Job, mid: str, ok: bool, info: dict):
        with self.lock:
            pr = self.probes.pop((job.id, mid), None)
            m = self.machines.get(mid)
            name = m.name if m else mid
            aps = job.approvals if job.approvals is not None else {}
            reason = info.get("reason") or ""
            aps[name] = {"ok": bool(ok), "version": (job.package or {}).get("hash"),
                         "device": device_for(job, name), "diff": info.get("diff"), "ref": (pr or {}).get("ref"),
                         "reason": reason, "image": info.get("image"), "at": time.time()}
            job.approvals = aps
            job.save()
        if ok:
            self.emit("message", {"level": "info", "job_id": job.id,
                                  "text": f"{job.name}: {name}'s test frame matches – it joins in"})
        else:
            self.emit("message", {"level": "warn", "job_id": job.id,
                                  "text": f"{job.name}: {name} left out – {reason}. Compare the images in "
                                          f"data/consistency/; untick the consistency check in the job "
                                          "settings to use it anyway."})
        self.emit("machines", {})

    def drop_probe(self, job_id: str, mid: str):
        with self.lock:
            self.probes.pop((job_id, mid), None)

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
            for k in [k for k, p in self.probes.items() if k[1] == mid and (job_id is None or k[0] == job_id)
                      and p["state"] == "rendering a test frame"]:
                del self.probes[k]
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
            self.probes = {k: p for k, p in self.probes.items() if p["state"] != "rendering a test frame"}

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
                    mid: {"name": m.name, "enabled": m.enabled, "total": dict(m.total), "os": m.os, "gpu": m.gpu,
                          "installs": m.installs, "key_hash": m.key_hash}
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
                for k, pr in list(self.probes.items()):
                    mm = self.machines.get(k[1])
                    if pr["state"].startswith("rendering") and (mm is None or not mm.online):
                        del self.probes[k]
            for l in expired:
                m = self.machines.get(l.machine_id)
                self.emit("message", {"level": "warn", "job_id": l.job_id,
                                      "text": f"{m.name if m else l.machine_id} stopped answering – frame {l.frame} "
                                              "goes back into the pool"})
            if expired:
                self.emit("machines", {})
