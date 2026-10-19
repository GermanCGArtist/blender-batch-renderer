"""Coordinator side of rendering on several computers: the HTTP endpoints workers talk to.

All endpoints live under /api/v1/worker/ and need two headers:
  X-BBR-Worker-Key: <key>      the key a worker receives once when it pairs (pairing code)
  X-BBR-Machine:    <id>       the worker's machine id

  POST /api/v1/worker/pair            {"code", "id", "name"}  -> {"key", "coordinator"}  (no key needed)
  POST /api/v1/worker/hello           machine info + Blender installs with their add-ons
  POST /api/v1/worker/heartbeat       {"state", "live", "note"} -> abort / stop signals, current leases
  POST /api/v1/worker/lease           -> {"lease": {...}} or {"lease": null, "reason"}
  POST /api/v1/worker/next            {"job_id"} -> {"frame": n | null}  (Blender stays open)
  GET  /api/v1/worker/package/<job>/<hash>   the job's self-contained copy (zip, Range supported)
  PUT  /api/v1/worker/extra/<job>/<frame>?path=<node>/<file>   an extra output file (File Output node)
  PUT  /api/v1/worker/frame/<job>/<frame>?name=<file>   the finished frame; header X-BBR-Meta: JSON stats
  POST /api/v1/worker/fail            {"job_id", "frame", "error"} -> {"status"}
  POST /api/v1/worker/job-error       {"job_id", "reason"}  this machine cannot render the job
  POST /api/v1/worker/release         {"job_id"?, "frames"?}  give leases back (stop now, quitting)
  POST /api/v1/worker/bye             the worker is shutting down / leaving worker mode
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .models import F_DONE, Job
from . import reports

MAX_UPLOAD = 8 << 30            # 8 GB per file (huge multilayer EXRs)
DISCOVERY_PORT = 8643
SAFE_PART = re.compile(r"^[^/\\:*?\"<>|\x00-\x1f]+$")


class WorkerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _safe_rel(rel: str) -> str:
    if not rel or rel[0] in "/\\" or re.match(r"^[A-Za-z]:", rel):
        raise WorkerError(400, f"bad file name: {rel!r}")
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") or not SAFE_PART.match(p) for p in parts):
        raise WorkerError(400, f"bad file name: {rel!r}")
    return "/".join(parts)


class WorkerEndpoints:
    def __init__(self, runner, settings):
        self.runner = runner
        self.sched = runner.scheduler
        self.queue = runner.queue
        self.settings = settings
        self._pair_fail = []          # times of wrong pairing codes
        self._lock = threading.Lock()

    # ---- auth -----------------------------------------------------------------
    def key(self) -> str:
        if not self.settings.worker_secret:
            self.settings.worker_secret = secrets.token_urlsafe(24)
            try:
                self.settings.save()
            except OSError:
                pass
        return self.settings.worker_secret

    def authorized(self, headers) -> bool:
        k = headers.get("X-BBR-Worker-Key") or ""
        return bool(k) and secrets.compare_digest(k, self.key())

    def pair(self, body: dict) -> dict:
        if not self.settings.share_with_workers:
            raise WorkerError(403, "this computer does not share jobs with other machines "
                                   "(Settings → Machines → Share jobs with workers)")
        now = time.time()
        with self._lock:
            self._pair_fail = [t for t in self._pair_fail if now - t < 600]
            if len(self._pair_fail) >= 8:
                raise WorkerError(429, "too many wrong codes – wait 10 minutes")
            code = re.sub(r"\D", "", str(body.get("code", "")))
            if not code or not secrets.compare_digest(code, str(self.settings.pairing_code)):
                self._pair_fail.append(now)
                raise WorkerError(401, "wrong pairing code")
        self.runner._msg("info", f"{body.get('name') or 'A computer'} is now paired as a worker")
        return {"key": self.key(), "coordinator": self.sched.local.name, "app_version": __version__}

    # ---- dispatch -------------------------------------------------------------
    def handle(self, h, method: str, path: str, q: dict):
        """Called by the API handler for /api/v1/worker/... Writes the response itself."""
        sub = path[len("/api/v1/worker/"):].strip("/")
        if sub == "pair" and method == "POST":
            return h._json(200, self.pair(h._body()))
        if not self.authorized(h.headers):
            raise WorkerError(401, "not paired with this coordinator (wrong or missing worker key)")
        mid = (h.headers.get("X-BBR-Machine") or "").strip()
        if not re.match(r"^[\w-]{4,40}$", mid):
            raise WorkerError(400, "missing X-BBR-Machine header")
        if sub == "hello" and method == "POST":
            info = h._body()
            info["id"] = mid
            m = self.sched.register(info)
            self.sched._persist()
            return h._json(200, {"ok": True, "id": m.id, "name": m.name, "enabled": m.enabled,
                                 "coordinator": self.sched.local.name, "app_version": __version__})
        if sub == "heartbeat" and method == "POST":
            b = h._body()
            return h._json(200, self.sched.heartbeat(mid, str(b.get("state", "")), b.get("live"),
                                                     str(b.get("note", ""))[:300]))
        if sub == "lease" and method == "POST":
            return h._json(200, self.sched.lease_remote(mid))
        if sub == "next" and method == "POST":
            b = h._body()
            return h._json(200, {"frame": self.sched.next_frame_same_job(mid, str(b.get("job_id", "")))})
        if sub == "fail" and method == "POST":
            return h._json(200, self.fail(mid, h._body()))
        if sub == "job-error" and method == "POST":
            b = h._body()
            job = self._job(str(b.get("job_id", "")))
            reason = str(b.get("reason", "cannot render this job"))[:300]
            self.sched.exclude(mid, job, reason)
            m = self.sched.machines.get(mid)
            self.runner._msg("warn", f"{job.name}: {m.name if m else mid} cannot render it – {reason}", job)
            return h._json(200, {"ok": True})
        if sub == "release" and method == "POST":
            b = h._body()
            if b.get("frames") is not None and b.get("job_id"):
                for f in b["frames"]:
                    self.sched.release(str(b["job_id"]), int(f), mid)
            else:
                self.sched.release_machine(mid, b.get("job_id") or None)
            self.sched.emit("machines", {})
            return h._json(200, {"ok": True})
        if sub == "bye" and method == "POST":
            self.sched.release_machine(mid)
            with self.sched.lock:
                m = self.sched.machines.get(mid)
                if m:
                    m.last_seen = 0
                    m.state = "offline"
            self.sched.emit("machines", {})
            return h._json(200, {"ok": True})
        m = re.match(r"^package/([\w-]+)/(\w+)$", sub)
        if m and method == "GET":
            return self.send_package(h, m.group(1), m.group(2))
        m = re.match(r"^(frame|extra)/([\w-]+)/(-?\d+)$", sub)
        if m and method == "PUT":
            return self.receive(h, mid, m.group(1), m.group(2), int(m.group(3)), q)
        raise WorkerError(404, f"unknown worker endpoint {method} {sub}")

    def _job(self, job_id: str) -> Job:
        j = self.queue.find(job_id)
        if j is None:
            raise WorkerError(404, "this job is no longer in the coordinator's queue")
        return j

    # ---- packages ---------------------------------------------------------------
    def send_package(self, h, job_id: str, digest: str):
        from .package import package_file
        job = self._job(job_id)
        p = package_file(job)
        if p is None or (job.package or {}).get("hash") != digest:
            raise WorkerError(410, "this package is outdated – ask for a new lease")
        size = p.stat().st_size
        start = 0
        rng = h.headers.get("Range") or ""
        mr = re.match(r"bytes=(\d+)-$", rng.strip())
        if mr:
            start = min(int(mr.group(1)), size)
        h.send_response(206 if start else 200)
        h.send_header("Content-Type", "application/zip")
        h.send_header("Content-Length", str(size - start))
        h.send_header("Accept-Ranges", "bytes")
        if start:
            h.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        h.end_headers()
        with self.sched.lock:
            mach = self.sched.machines.get(h.headers.get("X-BBR-Machine") or "")
            if mach:
                mach.state = "downloading"
        with open(p, "rb") as f:
            f.seek(start)
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                try:
                    h.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

    # ---- results ----------------------------------------------------------------
    def _read_body_to(self, h, dest: Path) -> int:
        n = int(h.headers.get("Content-Length") or -1)
        if n < 0:
            raise WorkerError(411, "Content-Length required")
        if n > MAX_UPLOAD:
            raise WorkerError(413, "file too large")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".tmp_up_{secrets.token_hex(4)}_{dest.name}"
        left = n
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = h.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise WorkerError(400, "upload interrupted")
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return n

    def receive(self, h, mid: str, kind: str, job_id: str, frame: int, q: dict):
        job = self._job(job_id)
        rec = job.frames.get(str(frame)) or {}
        if rec.get("status") == F_DONE:
            # another machine was faster (e.g. after this one dropped off Wi-Fi): drop the upload
            h.rfile.read(int(h.headers.get("Content-Length") or 0))
            return h._json(409, {"error": "frame already done", "duplicate": True})
        from .scheduler import LOCAL_ID  # noqa: F401
        if kind == "extra":
            rel = _safe_rel((q.get("path") or [""])[0])
            self._read_body_to(h, job.frames_dir / rel)
            return h._json(200, {"ok": True})
        name = _safe_rel((q.get("name") or [""])[0])
        if "/" in name:
            raise WorkerError(400, "frame name must be a file name")
        try:
            meta = json.loads(h.headers.get("X-BBR-Meta") or "{}")
        except ValueError:
            meta = {}
        dest = job.frames_dir / name
        size = self._read_body_to(h, dest)
        m = self.sched.machines.get(mid)
        mname = m.name if m else mid
        with self.sched.lock:
            rec = job.frames.setdefault(str(frame), {"attempts": 0})
            if rec.get("status") == F_DONE:          # raced with another upload
                return h._json(409, {"error": "frame already done", "duplicate": True})
            rec.update({
                "status": F_DONE,
                "attempts": int(rec.get("attempts", 0)) + 1,
                "render_s": meta.get("render_s"), "save_s": meta.get("save_s"),
                "file": str(dest), "rel": os.path.relpath(dest, job.folder), "size": size,
                "finished_at": time.time(),
                "blender_peak_mb": meta.get("peak_mb"), "process_peak_mb": meta.get("process_peak_mb"),
                "samples": meta.get("samples"), "stats": str(meta.get("stats", ""))[:300],
                "machine": mname, "upload_s": meta.get("upload_s"),
            })
            rec.pop("last_error", None)
            self.queue.save_job(job)
        reports.append_frame(job, frame, rec, 0)
        self.sched.frame_done(mid, job, frame, rec.get("render_s"), size)
        self.sched.emit("frame_done", {"job_id": job.id, "frame": frame, "machine": mname})
        if not self.runner._disk_ok(job):
            pass        # the runner stops the batch; workers see it in their next heartbeat
        return h._json(200, {"ok": True})

    def fail(self, mid: str, b: dict) -> dict:
        job = self._job(str(b.get("job_id", "")))
        frame = int(b.get("frame"))
        err = str(b.get("error", "failed"))[:500]
        if (job.frames.get(str(frame)) or {}).get("status") == F_DONE:
            return {"status": F_DONE}
        status = self.sched.frame_failed(mid, job, frame, err)
        rec = job.frames[str(frame)]
        m = self.sched.machines.get(mid)
        who = m.name if m else mid
        if status == "failed":
            self.runner._msg("error", f"{job.name}: frame {frame} failed {rec['attempts']}x, skipping it "
                                      f"(last on {who}: {err})", job)
        else:
            self.runner._msg("warn", f"{job.name}: frame {frame} failed on {who} (attempt {rec['attempts']}/"
                                     f"{job.settings.max_attempts}): {err}", job)
        reports.append_frame(job, frame, rec, 0)
        return {"status": status}


# ---- finding the coordinator on the home network ----------------------------------
class DiscoveryResponder:
    """Answers 'is there a coordinator?' broadcasts from workers (UDP, port 8643)."""

    def __init__(self, settings, name: str):
        self.settings = settings
        self.name = name
        self.sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        self.stop()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", int(os.environ.get("BBR_DISCOVERY_PORT", DISCOVERY_PORT))))
            s.settimeout(1.0)
        except OSError:
            return False
        self.sock = s
        self.thread = threading.Thread(target=self._loop, args=(s,), name="bbr-discovery", daemon=True)
        self.thread.start()
        return True

    def _loop(self, s):
        while self.sock is s:
            try:
                data, addr = s.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() != b"BBR_DISCOVER":
                continue
            reply = json.dumps({"bbr": 1, "name": self.name, "port": int(self.settings.api_port),
                                "app_version": __version__}).encode()
            try:
                s.sendto(reply, addr)
            except OSError:
                pass

    def stop(self):
        s, self.sock = self.sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass


def discover(timeout: float = 1.5, port: Optional[int] = None) -> list:
    """Broadcast on the home network; returns [{"name", "url", "app_version"}]."""
    port = port or int(os.environ.get("BBR_DISCOVERY_PORT", DISCOVERY_PORT))
    found = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.3)
        for target in ("255.255.255.255", "127.0.0.1"):
            try:
                s.sendto(b"BBR_DISCOVER", (target, port))
            except OSError:
                pass
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                d = json.loads(data.decode())
            except ValueError:
                continue
            if d.get("bbr") != 1:
                continue
            ip = addr[0]
            url = f"http://{ip}:{int(d.get('port', 8642))}"
            found[url] = {"name": d.get("name") or ip, "url": url, "app_version": d.get("app_version"),
                          "local": ip.startswith("127.")}
        s.close()
    except OSError:
        pass
    # the same coordinator answers on loopback and on the LAN address: prefer the LAN one
    names_lan = {d["name"] for d in found.values() if not d["local"]}
    return [d for d in found.values() if not (d["local"] and d["name"] in names_lan)]
