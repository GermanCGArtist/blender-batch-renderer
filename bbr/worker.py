"""Worker mode: this computer renders frames for a coordinator on the home network.

  1. Pair once with the coordinator's 6-digit code (Settings → Machines on the coordinator).
  2. The worker tells the coordinator which Blender versions and add-ons it has.
  3. It asks for work (a lease on one frame), downloads the job's self-contained copy once
     (cached, resumable), opens it in Blender and keeps Blender open while it asks for more
     frames of the same job.
  4. Each finished frame is uploaded right away (in the background, so Blender carries on);
     the local copy is deleted after the coordinator confirmed it.
  5. Heartbeats every few seconds keep the leases alive and carry "stop now" / "stop after
     frame" / "job cancelled" from the coordinator.

Nothing is ever written next to the coordinator's files: the coordinator stores the frames.
"""
from __future__ import annotations

import collections
import hashlib
import http.client
import json
import os
import platform
import queue as _queue
import shutil
import socket
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, quote

from . import __version__
from .installs import BlenderInstall
from .parse import parse_marker, parse_stats, fmt_bytes
from .process import BlenderProcess
from .storage import app_data_dir, atomic_write_json, read_json

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None

SCRIPTS = Path(__file__).resolve().parent / "blender_scripts"
RENDER_SCRIPT = str(SCRIPTS / "bbr_render.py")
ADDONS_SCRIPT = str(SCRIPTS / "bbr_addons.py")
HEARTBEAT_S = 5.0
LOAD_FAILS_BEFORE_GIVING_UP = 2
CRASHES_BEFORE_GIVING_UP = 3


class CoordinatorError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_server(addr: str, default_port: int = 8642) -> str:
    addr = (addr or "").strip().rstrip("/")
    if not addr:
        return ""
    if "://" not in addr:
        addr = "http://" + addr
    u = urlparse(addr)
    return f"http://{u.hostname}:{u.port or default_port}"


def _request(server: str, method: str, path: str, body=None, headers=None, timeout: float = 20.0):
    """Returns (status, parsed JSON or {}). Raises OSError when the coordinator is unreachable."""
    u = urlparse(server)
    c = http.client.HTTPConnection(u.hostname, u.port or 8642, timeout=timeout)
    try:
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        try:
            d = json.loads(raw.decode() or "{}")
        except ValueError:
            d = {"error": raw[:200].decode(errors="replace")}
        return r.status, d
    finally:
        c.close()


def pair(server: str, code: str, machine_id: str, name: str) -> dict:
    """Ask the coordinator for its worker key. Returns {"key", "coordinator"}; raises CoordinatorError."""
    server = normalize_server(server)
    try:
        st, d = _request(server, "POST", "/api/v1/worker/pair", {"code": code, "id": machine_id, "name": name})
    except OSError as e:
        raise CoordinatorError(0, f"cannot reach {server} ({e})")
    if st != 200:
        raise CoordinatorError(st, d.get("error") or f"HTTP {st}")
    return d


def machine_info() -> dict:
    ram = None
    if psutil is not None:
        try:
            ram = round(psutil.virtual_memory().total / 1024 ** 3, 1)
        except Exception:
            pass
    cpu = platform.processor() or platform.machine()
    if platform.system() == "Darwin":
        try:
            import subprocess
            cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                 text=True, timeout=5).stdout.strip() or cpu
        except Exception:
            pass
    return {"os": f"{platform.system()} {platform.release()}", "cpu": cpu, "ram_gb": ram,
            "app_version": __version__}


def list_addons(inst: BlenderInstall, timeout: float = 180) -> dict:
    """Enabled add-ons of one Blender install (with this computer's preferences)."""
    import subprocess
    try:
        out = subprocess.run([inst.executable, "-b", "--python-exit-code", "2", "--python", ADDONS_SCRIPT],
                             capture_output=True, text=True, timeout=timeout, errors="replace").stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e), "addons": {}}
    for line in out.splitlines():
        mk = parse_marker(line)
        if mk and mk[0] == "ADDONS":
            return mk[1]
    return {"error": "Blender did not report its add-ons", "addons": {}}


class Worker:
    def __init__(self, settings, emit: Callable[[str, dict], None] = lambda k, p: None):
        self.settings = settings
        self._emit_ext = emit
        self.cache_dir = app_data_dir() / "worker_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.running = False
        self.accepting = bool(getattr(settings, "worker_accepting", True))
        self.state = "stopped"          # stopped / not paired / offline / paused / idle / downloading /
        self.note = ""                  # loading / rendering / uploading / error
        self.coordinator_name = ""
        self.registered = False
        self.live: dict = {}
        self.current: Optional[dict] = None       # the lease being worked on
        self.installs_info: list = []
        self.console_tail = collections.deque(maxlen=2000)
        self._seq = 0
        self.session = {"frames": 0, "failed": 0, "render_s": 0.0, "bytes": 0, "started": None}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._proc: Optional[BlenderProcess] = None
        self._abort_gen = None
        self._abort_current = ""        # why the current Blender run is being stopped
        self._stop_after_frame = False
        self._uploads: "_queue.Queue" = _queue.Queue()
        self._uploading = 0
        self._active_upload = None
        self._threads = []
        self._addon_cache = {}          # executable -> (mtime, result)
        from .keepawake import KeepAwake
        self._keep = KeepAwake()
        self._lock = threading.RLock()

    # ---- public ------------------------------------------------------------------
    @property
    def paired(self) -> bool:
        return bool(self.settings.worker_server and self.settings.worker_key)

    @property
    def busy(self) -> bool:
        return self.current is not None or self._uploading > 0 or not self._uploads.empty()

    @property
    def name(self) -> str:
        return self.settings.worker_name or socket.gethostname().split(".")[0] or "worker"

    def start(self):
        if self.running:
            return
        self.running = True
        self._stop.clear()
        self.session = {"frames": 0, "failed": 0, "render_s": 0.0, "bytes": 0, "started": time.time()}
        for fn, nm in ((self._main_loop, "bbr-worker"), (self._heartbeat_loop, "bbr-worker-hb"),
                       (self._upload_loop, "bbr-worker-up")):
            t = threading.Thread(target=fn, name=nm, daemon=True)
            t.start()
            self._threads.append(t)
        self._set_state("starting")

    def stop(self, now: bool = True, timeout: float = 15.0):
        """Leave worker mode. now=True kills Blender (the frame goes back to the coordinator)."""
        self.accepting = False
        if now:
            self._kill("quit")
        else:
            self._stop_after_frame = True
        end = time.time() + timeout
        while self.current is not None and time.time() < end:
            time.sleep(0.1)
        # give finished frames a moment to reach the coordinator
        while (self._uploading or not self._uploads.empty()) and time.time() < end:
            time.sleep(0.1)
        try:
            self._post("/api/v1/worker/bye", {}, timeout=3)
        except Exception:
            pass
        self._stop.set()
        self._wake.set()
        self.running = False
        self._threads = []
        self._set_state("stopped")

    def set_accepting(self, on: bool):
        """Off = finish the current frame (and its upload), then pause."""
        self.accepting = bool(on)
        self.settings.worker_accepting = self.accepting
        try:
            self.settings.save()
        except OSError:
            pass
        if not on:
            self._stop_after_frame = True
        else:
            self._stop_after_frame = False
        self._wake.set()
        self.emit("worker", {})

    def stop_now(self):
        """Drop the current frame right away and pause."""
        self.accepting = False
        self._kill("stopped on this computer")
        self.emit("worker", {})

    def forget_pairing(self):
        self.settings.worker_key = ""
        self.registered = False
        try:
            self.settings.save()
        except OSError:
            pass
        self._wake.set()

    def refresh_installs(self, force: bool = False) -> list:
        out = []
        for d in self.settings.installs:
            inst = BlenderInstall.from_dict(d)
            try:
                mt = os.path.getmtime(inst.executable)
            except OSError:
                continue
            c = self._addon_cache.get(inst.executable)
            if force or not c or c[0] != mt:
                self._console(f"Checking the add-ons of {inst.label} …")
                c = (mt, list_addons(inst))
                self._addon_cache[inst.executable] = c
            res = c[1]
            out.append({"version": inst.version_short, "label": inst.label, "executable": inst.executable,
                        "addons": res.get("addons") or {}, "addons_error": res.get("error")})
        self.installs_info = out
        self.registered = False          # tell the coordinator again
        self.emit("worker", {})
        return out

    def status(self) -> dict:
        return {"running": self.running, "state": self.state, "note": self.note, "accepting": self.accepting,
                "paired": self.paired, "server": self.settings.worker_server,
                "coordinator": self.coordinator_name, "current": self.current, "live": dict(self.live),
                "session": dict(self.session), "total": dict(self.settings.worker_stats or {}),
                "uploads_waiting": self._uploads.qsize() + self._uploading,
                "installs": self.installs_info}

    # ---- helpers -----------------------------------------------------------------
    def emit(self, kind: str, payload: dict):
        try:
            self._emit_ext(kind, payload)
        except Exception:
            pass          # a display problem must never stop the worker

    def _set_state(self, state: str, note: str = ""):
        if state != self.state or note != self.note:
            self.state, self.note = state, note
            self.emit("worker", {})

    def _console(self, text: str):
        self._seq += 1
        self.console_tail.append((self._seq, text))
        self.emit("console", {"job_id": None, "text": text})

    def _msg(self, level: str, text: str):
        self._console(f"[{level}] {text}")
        self.emit("message", {"level": level, "text": text, "job_id": None})

    def _headers(self) -> dict:
        return {"X-BBR-Worker-Key": self.settings.worker_key, "X-BBR-Machine": self.settings.worker_id}

    def _post(self, path: str, body: dict, timeout: float = 20.0) -> dict:
        st, d = _request(self.settings.worker_server, "POST", path, body, self._headers(), timeout)
        if st == 401:
            self.registered = False
            raise CoordinatorError(st, d.get("error") or "not paired")
        if st >= 400:
            raise CoordinatorError(st, d.get("error") or f"HTTP {st}")
        return d

    def _sleep(self, s: float):
        self._wake.clear()
        self._wake.wait(s)

    def _kill(self, reason: str):
        self._abort_current = reason
        p = self._proc
        if p:
            p.kill(reason)

    def _hello(self):
        if not self.installs_info:
            self.refresh_installs()
        info = machine_info()
        info.update({"name": self.name,
                     "installs": [{k: i[k] for k in ("version", "label", "addons")} for i in self.installs_info]})
        d = self._post("/api/v1/worker/hello", info)
        self.coordinator_name = d.get("coordinator") or ""
        self.registered = True
        self._console(f"Connected to {self.coordinator_name} ({self.settings.worker_server})")

    # ---- main loop ---------------------------------------------------------------
    def _main_loop(self):
        while not self._stop.is_set():
            try:
                if not self.paired:
                    self._set_state("not paired", "enter the coordinator's address and pairing code")
                    self._sleep(2)
                    continue
                if not self.registered:
                    self._hello()
                if not self.accepting:
                    self._set_state("paused", "not accepting jobs")
                    self._sleep(2)
                    continue
                self._stop_after_frame = False
                r = self._post("/api/v1/worker/lease", {})
                if r.get("register"):
                    self.registered = False
                    continue
                lease = r.get("lease")
                if not lease:
                    self._set_state("idle", r.get("reason") or "waiting for work")
                    self._sleep(3)
                    continue
                self._work(lease)
            except CoordinatorError as e:
                if e.status == 401:
                    self._set_state("not paired", e.message)
                    self._sleep(5)
                else:
                    self._set_state("error", e.message)
                    self._sleep(5)
            except OSError as e:
                self.registered = False
                self._set_state("offline", f"cannot reach the coordinator ({e.__class__.__name__})")
                self._sleep(5)
            except Exception as e:      # never let the worker thread die
                import traceback
                self._console(traceback.format_exc())
                self._set_state("error", f"internal error: {e!r}")
                self._sleep(5)

    def _heartbeat_loop(self):
        while not self._stop.wait(HEARTBEAT_S):
            if not (self.paired and self.registered):
                continue
            try:
                d = self._post("/api/v1/worker/heartbeat",
                               {"state": self.state, "note": self.note, "live": self._live_for_coordinator()},
                               timeout=8)
            except Exception:
                continue
            if d.get("register"):
                self.registered = False
                continue
            cur = self.current
            gen = d.get("abort_gen")
            if self._abort_gen is None:
                self._abort_gen = gen
            elif gen is not None and gen != self._abort_gen:
                self._abort_gen = gen
                if cur:
                    self._console("The coordinator stopped the batch – dropping the current frame")
                    self._kill("stopped by the coordinator")
            if cur and cur.get("job_id") in (d.get("abort_jobs") or []):
                self._console("The coordinator cancelled this job")
                self._kill("job cancelled by the coordinator")
            if d.get("stop_after_frame"):
                self._stop_after_frame = True
            elif self.accepting:
                self._stop_after_frame = False
            # leases the coordinator no longer knows about: the frame was given to someone else
            if cur and self.live.get("frame") is not None:
                mine = {(l["job_id"], l["frame"]) for l in d.get("leases") or []}
                if (cur["job_id"], self.live["frame"]) not in mine and self.live.get("since", 0) < time.time() - 60:
                    self._console(f"Frame {self.live['frame']} was handed to another machine meanwhile "
                                  "(this one was unreachable); finishing it anyway")

    def _live_for_coordinator(self) -> dict:
        L = dict(self.live)
        cur = self.current or {}
        L.update({"job_id": cur.get("job_id"), "job": cur.get("job")})
        L.pop("since", None)
        return L

    # ---- one lease -----------------------------------------------------------------
    def _install_for(self, version: str) -> Optional[BlenderInstall]:
        for d in self.settings.installs:
            i = BlenderInstall.from_dict(d)
            if i.version_short == version:
                return i
        return None

    def _work(self, lease: dict):
        job_id = lease["job_id"]
        self.current = dict(lease)
        self._abort_current = ""
        if self._abort_gen is None:
            self._abort_gen = lease.get("abort_gen")
        held = {lease["frame"]}           # frames we hold a lease on that are not finished yet
        if getattr(self.settings, "keep_awake", True):
            self._keep.start()
        try:
            inst = self._install_for(lease.get("blender") or "")
            if inst is None:
                self._post("/api/v1/worker/job-error", {"job_id": job_id,
                                                         "reason": f"Blender {lease.get('blender')} not installed"})
                return
            try:
                pdir = self._ensure_package(lease)
            except CoordinatorError as e:
                if e.status == 410:
                    return            # outdated package: the lease is dropped, ask again
                raise
            except _PackageBroken as e:
                self._post("/api/v1/worker/job-error", {"job_id": job_id, "reason": str(e)})
                return
            if self._abort_current:
                return
            self._render(lease, inst, pdir, held)
        finally:
            leftover = sorted(f for f in held if f not in self._pending_frames(job_id))
            if leftover:
                try:
                    self._post("/api/v1/worker/release", {"job_id": job_id, "frames": leftover}, timeout=5)
                except Exception:
                    pass        # leases expire on their own
            self._keep.stop()
            self.current = None
            self.live = {}
            self.emit("worker", {})

    def _render(self, lease: dict, inst: BlenderInstall, pdir: Path, held: set):
        job_id = lease["job_id"]
        work = self.cache_dir / "work" / job_id
        shutil.rmtree(work, ignore_errors=True)
        (work / "frames").mkdir(parents=True, exist_ok=True)
        (work / "tmp").mkdir(parents=True, exist_ok=True)
        first = [lease["frame"]]
        load_fails = crashes = 0
        timeout_s = float(lease.get("frame_timeout_min") or 0) * 60
        n = 0
        while not self._stop.is_set():
            n += 1
            args = {"frames_dir": str(work / "frames"), "prefix": lease.get("prefix", ""), "skip": [],
                    "dynamic": True, "max_frames": int(lease.get("restart_every") or 0),
                    "overrides": lease.get("overrides") or {}}
            args_path = work / f"args_{n:03d}.json"
            atomic_write_json(args_path, args)
            cmd = [inst.executable, "-b", str(pdir / "main.blend"), "--python-exit-code", "2",
                   "--python", RENDER_SCRIPT, "--", str(args_path)]
            env = dict(os.environ)
            env.update({"TMPDIR": str(work / "tmp"), "TEMP": str(work / "tmp"), "TMP": str(work / "tmp"),
                        "PYTHONUNBUFFERED": "1"})
            st = {"all_done": False, "chunk": False, "error": "", "fatal": "", "frame": None, "rendered": 0}
            self._set_state("loading", f"{lease.get('job')}: opening the file")
            self.live = {"frame": None, "since": time.time()}
            proc = BlenderProcess(cmd, env, str(work / f"blender_{n:03d}.log"), lambda l: None, cwd=str(pdir))
            proc.interactive = True

            def on_line(line, proc=proc, st=st):
                self._console(line)
                mk = parse_marker(line)
                if not mk:
                    if line.startswith("Error") or "Traceback" in line:
                        st["error"] = (st["error"] + "\n" + line).strip()[-2000:]
                    return
                kind, data = mk
                if kind == "NEED_FRAME":
                    f = None
                    if first:
                        f = first.pop()
                    elif not (self._stop_after_frame or self._abort_current or not self.accepting):
                        try:
                            f = self._post("/api/v1/worker/next", {"job_id": job_id}, timeout=10).get("frame")
                        except Exception:
                            f = None
                    if f is not None:
                        held.add(int(f))
                    proc.send(f"FRAME {f}" if f is not None else "DONE")
                elif kind == "JOB_INFO":
                    self.live["device"] = data.get("device")
                elif kind == "FRAME_START":
                    st["frame"] = int(data["frame"])
                    self.live = {"frame": st["frame"], "since": time.time(), "device": self.live.get("device")}
                    proc.reset_peak()
                    self._set_state("rendering", f"{lease.get('job')}: frame {st['frame']}")
                elif kind == "STATS":
                    s = parse_stats(str(data.get("s", "")))
                    for k in ("sample", "samples", "remaining_s", "elapsed_s"):
                        if k in s:
                            self.live[k] = s[k]
                    self.live["mem_mb"] = proc.rss_mb
                    self.emit("worker_live", {})
                elif kind == "FRAME_DONE":
                    f = int(data["frame"])
                    st["frame"] = None
                    st["rendered"] += 1
                    meta = {k: data.get(k) for k in ("render_s", "save_s", "peak_mb", "samples", "stats")}
                    meta["process_peak_mb"] = proc.rss_peak_mb
                    self._uploads.put({"job_id": job_id, "job": lease.get("job"), "frame": f,
                                       "file": data["file"], "extra": data.get("extra") or [],
                                       "frames_dir": str(work / "frames"), "meta": meta, "held": held})
                    self.live = {"frame": None, "since": time.time(), "device": self.live.get("device")}
                    self._set_state("loading", f"{lease.get('job')}: next frame")
                elif kind == "FRAME_ERROR":
                    st["error"] = str(data.get("error", ""))[-2000:]
                elif kind == "FATAL":
                    st["fatal"] = str(data.get("error", "fatal error"))
                elif kind == "CHUNK_DONE":
                    st["chunk"] = True
                elif kind == "ALL_DONE":
                    st["all_done"] = True

            def watch(proc=proc, st=st):
                self.live["mem_mb"] = proc.rss_mb
                if timeout_s and st["frame"] is not None and time.time() - self.live.get("since", time.time()) > timeout_s:
                    proc.kill("timeout")

            proc.on_line = on_line
            proc.watch_hook = watch
            self._proc = proc
            if self._abort_current:
                self._proc = None
                return
            try:
                rc = proc.run()
            except OSError as e:
                rc, st["error"] = -999, f"could not start Blender: {e}"
            finally:
                self._proc = None
            if self._abort_current or proc.kill_reason == "quit":
                self._console(f"Stopped: {self._abort_current or 'quit'}")
                return                                   # leases are released by _work
            if st["fatal"]:
                self._post("/api/v1/worker/job-error", {"job_id": job_id, "reason": st["fatal"][:300]})
                return
            if rc == 0 and st["all_done"]:
                return
            if rc == 0 and st["chunk"]:
                continue                                 # planned restart of Blender
            why = "frame timeout" if proc.kill_reason == "timeout" else f"Blender exited with code {rc}"
            if st["error"]:
                why += f": {st['error'].splitlines()[-1][:300]}"
            self._keep_crash_log(work, n)
            if st["frame"] is not None:
                f = st["frame"]
                held.discard(f)
                crashes += 1
                self._bump("failed")
                try:
                    self._post("/api/v1/worker/fail", {"job_id": job_id, "frame": f, "error": why})
                except Exception:
                    pass
                self._msg("warn", f"{lease.get('job')}: frame {f} failed here ({why})")
                if crashes >= CRASHES_BEFORE_GIVING_UP and st["rendered"] == 0:
                    self._post("/api/v1/worker/job-error", {"job_id": job_id,
                                                             "reason": f"crashed {crashes}x in a row ({why})"})
                    return
            elif st["rendered"] == 0:
                load_fails += 1
                if load_fails >= LOAD_FAILS_BEFORE_GIVING_UP:
                    self._post("/api/v1/worker/job-error", {"job_id": job_id,
                                                             "reason": f"Blender could not open the job here ({why})"})
                    return
            if st["rendered"]:
                crashes = 0
            # hand the frames that Blender got but never started back, then carry on
            unstarted = [f for f in held if f not in self._pending_frames(job_id)]
            for f in unstarted:
                held.discard(f)
            if unstarted:
                try:
                    self._post("/api/v1/worker/release", {"job_id": job_id, "frames": unstarted}, timeout=5)
                except Exception:
                    pass
            if not self.accepting or self._stop_after_frame:
                return
            time.sleep(1)

    def _pending_frames(self, job_id: str) -> set:
        with self._uploads.mutex:
            items = list(self._uploads.queue)
        a = self._active_upload
        if a:
            items.append(a)
        return {u["frame"] for u in items if u["job_id"] == job_id}

    def _keep_crash_log(self, work: Path, n: int):
        try:
            d = app_data_dir() / "worker_logs"
            d.mkdir(exist_ok=True)
            shutil.copy2(work / f"blender_{n:03d}.log", d / f"{time.strftime('%Y%m%d_%H%M%S')}_{work.name}.log")
        except OSError:
            pass

    def _bump(self, key: str, val=1, job: Optional[str] = None):
        with self._lock:
            self.session[key] = self.session.get(key, 0) + val
            tot = self.settings.worker_stats or {}
            tot[key] = tot.get(key, 0) + val
            if job:
                pj = tot.setdefault("per_job", {}).setdefault(job, {})
                pj[key] = pj.get(key, 0) + val
            self.settings.worker_stats = tot

    # ---- uploads -------------------------------------------------------------------
    def _upload_loop(self):
        while not self._stop.is_set():
            try:
                item = self._uploads.get(timeout=0.5)
            except _queue.Empty:
                continue
            self._uploading += 1
            self._active_upload = item
            try:
                self._upload(item)
            finally:
                self._active_upload = None
                self._uploading -= 1
                item["held"].discard(item["frame"])
                self.emit("worker", {})

    def _put(self, path: str, file: str, extra_headers: dict) -> int:
        u = urlparse(self.settings.worker_server)
        size = os.path.getsize(file)
        c = http.client.HTTPConnection(u.hostname, u.port or 8642, timeout=60)
        try:
            h = dict(self._headers())
            h.update(extra_headers)
            h["Content-Length"] = str(size)
            h["Content-Type"] = "application/octet-stream"
            c.putrequest("PUT", path)
            for k, v in h.items():
                c.putheader(k, v)
            c.endheaders()
            with open(file, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    c.send(chunk)
            r = c.getresponse()
            r.read()
            return r.status
        finally:
            c.close()

    def _upload(self, item: dict):
        job_id, f = item["job_id"], item["frame"]
        t0 = time.time()
        delay = 2.0
        files = [os.path.join(item["frames_dir"], rel) for rel in item["extra"]] + [item["file"]]
        while not self._stop.is_set():
            try:
                for rel in item["extra"]:
                    p = os.path.join(item["frames_dir"], rel)
                    if not os.path.exists(p):
                        continue
                    st = self._put(f"/api/v1/worker/extra/{job_id}/{f}?path={quote(rel.replace(os.sep, '/'))}", p, {})
                    if st not in (200, 409):
                        raise CoordinatorError(st, f"upload failed (HTTP {st})")
                meta = dict(item["meta"], upload_s=None)
                meta["upload_s"] = round(time.time() - t0, 2)
                st = self._put(f"/api/v1/worker/frame/{job_id}/{f}?name={quote(os.path.basename(item['file']))}",
                               item["file"], {"X-BBR-Meta": json.dumps(meta)})
                if st == 200:
                    size = os.path.getsize(item["file"])
                    self._bump("frames", 1, item["job"])
                    self._bump("render_s", float(item["meta"].get("render_s") or 0), item["job"])
                    self._bump("bytes", size, item["job"])
                    try:
                        self.settings.save()
                    except OSError:
                        pass
                    self._console(f"Frame {f} of {item['job']} delivered ({fmt_bytes(size)})")
                elif st == 409:
                    self._console(f"Frame {f}: another machine already delivered it")
                elif st in (404, 410):
                    self._console(f"Frame {f}: the job is gone from the coordinator – dropped")
                elif st == 401:
                    self.registered = False
                    raise CoordinatorError(st, "not paired")
                elif st == 413 or st == 507:
                    self._msg("error", f"Frame {f}: the coordinator refused it (HTTP {st})")
                else:
                    raise CoordinatorError(st, f"upload failed (HTTP {st})")
                break
            except (OSError, CoordinatorError) as e:
                self._console(f"Frame {f}: upload failed ({e}), retrying in {int(delay)}s")
                self._stop.wait(delay)
                delay = min(delay * 2, 60)
        for p in files:
            try:
                os.remove(p)
            except OSError:
                pass

    # ---- packages ------------------------------------------------------------------
    def _ensure_package(self, lease: dict) -> Path:
        digest = lease["package"]
        final = self.cache_dir / digest
        if (final / ".complete").exists():
            (final / ".complete").touch()
            return final
        size = int(lease.get("package_size") or 0)
        self._evict(keep=digest, need=size * 2.2)
        free = shutil.disk_usage(self.cache_dir).free
        if size and free < size * 2.2:
            raise _PackageBroken(f"not enough disk space here for the job ({fmt_bytes(size * 2.2)} needed)")
        part = self.cache_dir / f"{digest}.zip.part"
        tries = 0
        while True:
            tries += 1
            self._download(lease, part, size)
            if self._abort_current:
                raise CoordinatorError(499, "stopped")
            h = hashlib.sha256()
            with open(part, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest()[:len(digest)] == digest:
                break
            part.unlink()
            if tries >= 2:
                raise _PackageBroken("the downloaded job copy is damaged twice in a row")
        self._set_state("downloading", f"{lease.get('job')}: unpacking")
        tmp = self.cache_dir / f"{digest}.unpacking"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(part) as z:
            for n in z.namelist():
                if n.startswith("/") or ".." in n.split("/"):
                    raise _PackageBroken("the job copy contains unsafe file names")
            z.extractall(tmp)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
        part.unlink()
        (final / ".complete").write_text(json.dumps({"job": lease.get("job"), "at": time.time()}))
        return final

    def _download(self, lease: dict, part: Path, size: int):
        u = urlparse(self.settings.worker_server)
        delay = 2.0
        while not self._abort_current and not self._stop.is_set():
            have = part.stat().st_size if part.exists() else 0
            if size and have == size:
                return
            c = http.client.HTTPConnection(u.hostname, u.port or 8642, timeout=60)
            try:
                h = self._headers()
                if have:
                    h["Range"] = f"bytes={have}-"
                c.request("GET", f"/api/v1/worker/package/{lease['job_id']}/{lease['package']}", headers=h)
                r = c.getresponse()
                if r.status == 410 or r.status == 404:
                    r.read()
                    raise CoordinatorError(410, "package outdated")
                if r.status not in (200, 206):
                    r.read()
                    raise CoordinatorError(r.status, f"download failed (HTTP {r.status})")
                mode = "ab" if r.status == 206 else "wb"
                got = have if r.status == 206 else 0
                t0, last = time.time(), 0.0
                with open(part, mode) as fh:
                    while True:
                        if self._abort_current:
                            return
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last > 0.5:
                            last = now
                            rate = (got - have) / max(0.001, now - t0)
                            self.live = {"download": got, "download_of": size, "rate": rate}
                            self._set_state("downloading", f"{lease.get('job')}: {fmt_bytes(got)} of "
                                                           f"{fmt_bytes(size)} ({fmt_bytes(rate)}/s)")
                            self.emit("worker_live", {})
                if not size or got >= size:
                    return
            except CoordinatorError:
                raise
            except OSError as e:
                self._set_state("downloading", f"connection lost ({e.__class__.__name__}), resuming …")
                self._stop.wait(delay)
                delay = min(delay * 2, 30)
            finally:
                c.close()

    def _evict(self, keep: str, need: float = 0):
        """Delete the least recently used job copies until the cache fits its size limit."""
        limit = float(self.settings.worker_cache_gb or 50) * 1024 ** 3
        entries = []
        for d in self.cache_dir.iterdir():
            if d.is_dir() and (d / ".complete").exists() and d.name != keep:
                size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
                entries.append(((d / ".complete").stat().st_mtime, size, d))
        total = sum(e[1] for e in entries)
        for _t, size, d in sorted(entries):
            if total + need <= limit:
                break
            shutil.rmtree(d, ignore_errors=True)
            total -= size
            self._console(f"Removed an old job copy from the cache ({fmt_bytes(size)})")

    def cache_usage(self) -> int:
        try:
            return sum(p.stat().st_size for p in self.cache_dir.rglob("*") if p.is_file())
        except OSError:
            return 0

    def clear_cache(self):
        if self.busy:
            return False
        for d in self.cache_dir.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            else:
                try:
                    d.unlink()
                except OSError:
                    pass
        return True


class _PackageBroken(Exception):
    pass
