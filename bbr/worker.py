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
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, quote

from . import __version__, PROTOCOL
from .installs import BlenderInstall
from .parse import parse_marker, parse_stats, fmt_bytes
from .process import BlenderProcess, hidden
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
        st, d = _request(server, "POST", "/api/v1/worker/pair", {"code": code, "id": machine_id, "name": name},
                         {"X-BBR-Protocol": str(PROTOCOL)})
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
        out = subprocess.run([inst.executable, "-b", "-noaudio",
                              "--python-exit-code", "2", "--python", ADDONS_SCRIPT],
                             capture_output=True, text=True, timeout=timeout, errors="replace",
                             **hidden()).stdout
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
        self._cache_override = None    # tests
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
        self._full_jobs = set()         # (job id, version) that switched to Full copy
        self._rechecked = False
        self._progress = None
        from .keepawake import KeepAwake
        self._keep = KeepAwake()
        self._lock = threading.RLock()

    # ---- public ------------------------------------------------------------------
    @property
    def cache_dir(self) -> Path:
        d = self._cache_override or (Path(self.settings.worker_cache_dir).expanduser()
                                     if getattr(self.settings, "worker_cache_dir", "") else
                                     app_data_dir() / "worker_cache")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @cache_dir.setter
    def cache_dir(self, value):
        self._cache_override = Path(value)

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

    def _load_addon_cache(self):
        if self._addon_cache:
            return
        d = read_json(app_data_dir() / "addons_cache.json", {}) or {}
        self._addon_cache = {exe: (v["mtime"], v["result"]) for exe, v in d.items()
                             if isinstance(v, dict) and "mtime" in v and "result" in v}

    def _save_addon_cache(self):
        try:
            atomic_write_json(app_data_dir() / "addons_cache.json",
                              {exe: {"mtime": mt, "result": res} for exe, (mt, res) in self._addon_cache.items()})
        except OSError:
            pass

    def refresh_installs(self, force: bool = False) -> list:
        """Blender installs with their enabled add-ons. Without force, the list from the last check is
        used (asking Blender takes a few seconds per install); see _recheck_addons."""
        self._load_addon_cache()
        out = []
        changed = False
        for d in self.settings.installs:
            inst = BlenderInstall.from_dict(d)
            try:
                mt = os.path.getmtime(inst.executable)
            except OSError:
                continue
            c = self._addon_cache.get(inst.executable)
            if force or not c or c[0] != mt:
                self._console(f"Checking the add-ons of {inst.label} …")
                new = (mt, list_addons(inst))
                changed = changed or (c is None or c[1].get("addons") != new[1].get("addons"))
                c = new
                self._addon_cache[inst.executable] = c
            res = c[1]
            out.append({"version": inst.version_short, "label": inst.label, "executable": inst.executable,
                        "addons": res.get("addons") or {}, "addons_error": res.get("error")})
        self._save_addon_cache()
        if out != self.installs_info:
            self.installs_info = out
            self.registered = False      # tell the coordinator again
        self.emit("worker", {})
        return out

    def _recheck_addons(self):
        """Once per session, after connecting: ask Blender again in the background (an add-on may have
        been installed or enabled since), and tell the coordinator if anything changed."""
        if self._rechecked:
            return
        self._rechecked = True

        def run():
            try:
                self.refresh_installs(force=True)
            except Exception:
                pass
        threading.Thread(target=run, name="bbr-addons", daemon=True).start()

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
        return {"X-BBR-Worker-Key": self.settings.worker_key, "X-BBR-Machine": self.settings.worker_id,
                "X-BBR-Protocol": str(PROTOCOL)}

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
            self.refresh_installs()          # quick: uses the list from the last check if there is one
        info = machine_info()
        info.update({"name": self.name,
                     "installs": [{k: i[k] for k in ("version", "label", "addons")} for i in self.installs_info]})
        d = self._post("/api/v1/worker/hello", info)
        if d.get("protocol") != PROTOCOL:
            raise CoordinatorError(426, f"version mismatch: the coordinator runs version {d.get('app_version')}, "
                                        f"this computer {__version__} – update both to the same version")
        self.coordinator_name = d.get("coordinator") or ""
        self.registered = True
        self._console(f"Connected to {self.coordinator_name} ({self.settings.worker_server})")
        self._recheck_addons()

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
                    self._sleep(2)
                    continue
                self._work(lease)
            except CoordinatorError as e:
                if e.status == 401:
                    self._set_state("not paired", e.message)
                    self._sleep(5)
                elif e.status == 426:
                    self.registered = False
                    self._set_state("error", e.message)
                    self._sleep(30)
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

    def _note(self, job_id: str, level: str, text: str):
        self._msg(level, text)
        try:
            self._post("/api/v1/worker/note", {"job_id": job_id, "level": level, "text": text}, timeout=5)
        except Exception:
            pass

    def _work(self, lease: dict):
        job_id, version = lease["job_id"], lease["package"]
        self.current = dict(lease)
        self._abort_current = ""
        if self._abort_gen is None:
            self._abort_gen = lease.get("abort_gen")
        held = {lease["frame"]}           # frames we hold a lease on that are not finished yet
        mode = "full" if lease.get("transfer") == "full" or (job_id, version) in self._full_jobs else "smart"
        self.current["transfer"] = mode
        if getattr(self.settings, "keep_awake", True):
            self._keep.start()
        try:
            inst = self._install_for(lease.get("blender") or "")
            if inst is None:
                self._post("/api/v1/worker/job-error", {"job_id": job_id,
                                                         "reason": f"Blender {lease.get('blender')} not installed"})
                return
            first = [lease["frame"]]
            while True:
                try:
                    ws, man = self._prepare(lease, mode)
                except CoordinatorError as e:
                    if e.status in (404, 410):
                        self._console(f"The job copy changed on the coordinator ({e.message}) – starting over")
                        return
                    raise
                except _PackageBroken as e:
                    self._post("/api/v1/worker/job-error", {"job_id": job_id, "reason": str(e)})
                    return
                if self._abort_current:
                    return
                result = self._render(lease, inst, ws, man, mode, held, first)
                if result and result[0] == "fallback" and mode == "smart":
                    mode = "full"
                    self.current["transfer"] = mode
                    self._full_jobs.add((job_id, version))
                    self._note(job_id, "warn", f"{result[1]} – switching to Full copy for this job")
                    first = sorted(f for f in held if f not in self._pending_frames(job_id))
                    if not first:
                        return
                    continue
                if result and result[0] == "missing":
                    self._post("/api/v1/worker/job-error", {"job_id": job_id, "reason": result[1][:300]})
                return
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

    def _render(self, lease, inst, ws: Path, man: dict, mode: str, held: set, first: list):
        """Run Blender until the job is done here. Returns None, ('fallback', why) or ('missing', why)."""
        job_id = lease["job_id"]
        work = self.cache_dir / "work" / job_id
        shutil.rmtree(work, ignore_errors=True)
        (work / "frames").mkdir(parents=True, exist_ok=True)
        (work / "tmp").mkdir(parents=True, exist_ok=True)
        first = list(first)
        load_fails = crashes = 0
        timeout_s = float(lease.get("frame_timeout_min") or 0) * 60
        frames_map = man.get("frames") or {}
        streaming = mode == "smart" and bool(frames_map)
        n = 0

        def frame_files(f):
            return [(p, man["files"][p]) for p in frames_map.get(str(f), []) if p in man["files"]]

        def fetch_frame(f):
            t0 = time.time()
            for p, e in frame_files(f):
                self._ensure_file(lease, ws, p, e, store=e["kind"] != "frame", quiet=True)
            return time.time() - t0

        def drop_frame_files(f, keep_for=()):
            keep = set()
            for g in keep_for:
                keep |= {p for p, _ in frame_files(g)}
            for p, e in frame_files(f):
                if e["kind"] == "frame" and p not in keep:
                    try:
                        (ws / p).unlink()
                    except OSError:
                        pass

        while not self._stop.is_set():
            n += 1
            args = {"frames_dir": str(work / "frames"), "prefix": lease.get("prefix", ""), "skip": [],
                    "dynamic": True, "max_frames": int(lease.get("restart_every") or 0),
                    "overrides": lease.get("overrides") or {},
                    "check_inputs": {"ignore": man.get("missing") or [],
                                     "streamed": streaming}}
            args_path = work / f"args_{n:03d}.json"
            atomic_write_json(args_path, args)
            cmd = [inst.executable, "-b", str(ws / "main.blend"), "--python-exit-code", "2",
                   "--python", RENDER_SCRIPT, "--", str(args_path)]
            env = dict(os.environ)
            env.update({"TMPDIR": str(work / "tmp"), "TEMP": str(work / "tmp"), "TMP": str(work / "tmp"),
                        "PYTHONUNBUFFERED": "1"})
            st = {"all_done": False, "chunk": False, "error": "", "fatal": "", "frame": None, "rendered": 0,
                  "missing": None, "input_error": "", "fetch_s": {}}
            ahead = {"frame": None, "thread": None}
            self._set_state("loading", f"{lease.get('job')}: opening the file")
            self.live = {"frame": None, "since": time.time()}
            proc = BlenderProcess(cmd, env, str(work / f"blender_{n:03d}.log"), lambda l: None, cwd=str(ws))
            proc.interactive = True

            def stopping():
                return bool(self._stop_after_frame or self._abort_current or not self.accepting)

            def prefetch():
                def run():
                    try:
                        g = self._post("/api/v1/worker/next", {"job_id": job_id}, timeout=10).get("frame")
                    except Exception:
                        return
                    if g is None:
                        return
                    held.add(int(g))
                    ahead["frame"] = int(g)
                    try:
                        st["fetch_s"][int(g)] = fetch_frame(int(g))
                    except Exception as ex:
                        ahead["error"] = ex
                t = threading.Thread(target=run, name="bbr-prefetch", daemon=True)
                ahead["thread"] = t
                t.start()

            def on_line(line, proc=proc, st=st):
                self._console(line)
                mk = parse_marker(line)
                if not mk:
                    if line.startswith("Error") or "Traceback" in line:
                        st["error"] = (st["error"] + "\n" + line).strip()[-2000:]
                    if streaming and st["frame"] is not None and INPUT_ERROR.search(line) and \
                            (str(ws) in line or any(os.path.basename(p) in line for p, _ in frame_files(st["frame"]))):
                        st["input_error"] = line.strip()[:300]
                    return
                kind, data = mk
                if kind == "NEED_FRAME":
                    f = None
                    if first:
                        f = first.pop(0)
                    else:
                        t = ahead.get("thread")
                        if t is not None:
                            t.join()
                            ahead["thread"] = None
                        if ahead.get("frame") is not None:
                            f, ahead["frame"] = ahead["frame"], None
                            if ahead.pop("error", None) is not None:
                                st["fetch_s"].pop(f, None)       # fetch again below
                        elif not stopping():
                            try:
                                f = self._post("/api/v1/worker/next", {"job_id": job_id}, timeout=10).get("frame")
                            except Exception:
                                f = None
                    if f is not None and stopping() and not first:
                        try:
                            self._post("/api/v1/worker/release", {"job_id": job_id, "frames": [f]}, timeout=5)
                        except Exception:
                            pass
                        held.discard(f)
                        f = None
                    if f is not None:
                        f = int(f)
                        held.add(f)
                        if streaming and f not in st["fetch_s"]:
                            self._set_state("downloading", f"{lease.get('job')}: files for frame {f}")
                            try:
                                st["fetch_s"][f] = fetch_frame(f)
                            except (CoordinatorError, _PackageBroken, OSError) as ex:
                                self._console(f"Could not get the files for frame {f}: {ex}")
                                st["input_error"] = f"files for frame {f} could not be fetched ({ex})"
                                f = None
                    proc.send(f"FRAME {f}" if f is not None else "DONE")
                elif kind == "INPUT_MISSING":
                    st["missing"] = data.get("files") or []
                elif kind == "JOB_INFO":
                    self.live["device"] = data.get("device")
                elif kind == "FRAME_START":
                    st["frame"] = int(data["frame"])
                    st["input_error"] = ""
                    self.live = {"frame": st["frame"], "since": time.time(), "device": self.live.get("device")}
                    proc.reset_peak()
                    self._set_state("rendering", f"{lease.get('job')}: frame {st['frame']}"
                                    + ("  (test frame for the consistency check)" if lease.get("probe") else ""))
                    if streaming and not first and not stopping() and not lease.get("probe"):
                        prefetch()
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
                    if st["input_error"]:
                        # a file was missing or unreadable while rendering this frame: do not deliver it
                        st["fallback"] = f"frame {f}: {st['input_error']}"
                        proc.kill("fallback")
                        return
                    st["rendered"] += 1
                    meta = {k: data.get(k) for k in ("render_s", "save_s", "peak_mb", "samples", "stats")}
                    meta["process_peak_mb"] = proc.rss_peak_mb
                    meta["fetch_s"] = round(st["fetch_s"].pop(f, 0.0), 2)
                    self._uploads.put({"job_id": job_id, "job": lease.get("job"), "frame": f,
                                       "file": data["file"], "extra": data.get("extra") or [],
                                       "frames_dir": str(work / "frames"), "meta": meta, "held": held})
                    if streaming:
                        drop_frame_files(f, keep_for=[g for g in (ahead.get("frame"),) if g is not None] + first)
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
                return None
            try:
                rc = proc.run()
            except OSError as e:
                rc, st["error"] = -999, f"could not start Blender: {e}"
            finally:
                self._proc = None
                t = ahead.get("thread")
                if t is not None:
                    t.join(30)
                if ahead.get("frame") is not None and ahead["frame"] not in first:
                    first.insert(0, ahead["frame"])        # leased and fetched: render it next time
            if self._abort_current or proc.kill_reason == "quit":
                self._console(f"Stopped: {self._abort_current or 'quit'}")
                return None
            if st["missing"] is not None:
                why = "files missing here: " + ", ".join(os.path.basename(x) for x in st["missing"][:5])
                self._keep_crash_log(work, n)
                return ("fallback", why) if mode == "smart" else ("missing", why)
            if st.get("fallback"):
                self._keep_crash_log(work, n)
                return ("fallback", st["fallback"]) if mode == "smart" else ("missing", st["fallback"])
            if st["input_error"] and mode == "smart" and st["frame"] is None and not st["rendered"]:
                return ("fallback", st["input_error"])
            if st["fatal"]:
                self._post("/api/v1/worker/job-error", {"job_id": job_id, "reason": st["fatal"][:300]})
                return None
            if rc == 0 and st["all_done"]:
                return None
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
                if lease.get("probe"):
                    return None
                if crashes >= CRASHES_BEFORE_GIVING_UP and st["rendered"] == 0:
                    self._post("/api/v1/worker/job-error", {"job_id": job_id,
                                                             "reason": f"crashed {crashes}x in a row ({why})"})
                    return None
            elif st["rendered"] == 0:
                load_fails += 1
                if load_fails >= LOAD_FAILS_BEFORE_GIVING_UP:
                    self._post("/api/v1/worker/job-error", {"job_id": job_id,
                                                             "reason": f"Blender could not open the job here ({why})"})
                    return None
            if st["rendered"]:
                crashes = 0
            # hand back the frames Blender got but never started (except one we prefetched)
            unstarted = [f for f in held if f not in self._pending_frames(job_id) and f not in first]
            for f in unstarted:
                held.discard(f)
            if unstarted:
                try:
                    self._post("/api/v1/worker/release", {"job_id": job_id, "frames": unstarted}, timeout=5)
                except Exception:
                    pass
            if not self.accepting or self._stop_after_frame or lease.get("probe"):
                return None
            time.sleep(1)
        return None

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
        digest = _sha256(file)
        c = http.client.HTTPConnection(u.hostname, u.port or 8642, timeout=60)
        try:
            h = dict(self._headers())
            h.update(extra_headers)
            h["Content-Length"] = str(size)
            h["Content-Type"] = "application/octet-stream"
            h["X-BBR-SHA256"] = digest
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
        damaged = 0
        while not self._stop.is_set():
            try:
                for rel in item["extra"]:
                    p = os.path.join(item["frames_dir"], rel)
                    if not os.path.exists(p):
                        continue
                    st = self._put(f"/api/v1/worker/extra/{job_id}/{f}?path={quote(rel.replace(os.sep, '/'))}", p, {})
                    if st == 422:
                        raise _Damaged()
                    if st not in (200, 409):
                        raise CoordinatorError(st, f"upload failed (HTTP {st})")
                meta = dict(item["meta"])
                meta["upload_s"] = round(time.time() - t0, 2)
                st = self._put(f"/api/v1/worker/frame/{job_id}/{f}?name={quote(os.path.basename(item['file']))}",
                               item["file"], {"X-BBR-Meta": json.dumps(meta)})
                if st == 202:
                    self._console(f"Test frame {f} of {item['job']} delivered – the coordinator compares it")
                elif st == 200:
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
                elif st == 422:
                    raise _Damaged()
                elif st == 401:
                    self.registered = False
                    raise CoordinatorError(st, "not paired")
                elif st in (413, 426, 507):
                    self._msg("error", f"Frame {f}: the coordinator refused it (HTTP {st})")
                else:
                    raise CoordinatorError(st, f"upload failed (HTTP {st})")
                break
            except _Damaged:
                damaged += 1
                self._console(f"Frame {f}: arrived damaged at the coordinator, sending it again ({damaged})")
                if damaged >= 5:
                    self._msg("error", f"Frame {f}: could not be delivered undamaged – giving it back")
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

    # ---- job copies: files by checksum ----------------------------------------------
    def _store_path(self, digest: str) -> Path:
        return self.cache_dir / "store" / digest[:2] / digest

    def _prepare(self, lease: dict, mode: str):
        """Make sure every file the job needs before rendering is here and correct."""
        job_id, version = lease["job_id"], lease["package"]
        jobs = self.cache_dir / "jobs" / job_id
        ws = jobs / version
        ws.mkdir(parents=True, exist_ok=True)
        for old in jobs.iterdir():                     # older versions of this job
            if old.name != version:
                shutil.rmtree(old, ignore_errors=True)
        man = read_json(ws / ".manifest.json")
        if not man or man.get("version") != version:
            self._set_state("downloading", f"{lease.get('job')}: list of files")
            man = self._get_json(f"/api/v1/worker/manifest/{job_id}/{version}")
            atomic_write_json(ws / ".manifest.json", man)
        (ws / ".manifest.json").touch()
        for d in {os.path.dirname(p) for p in man["files"]}:      # sequence folders exist from the start
            if d:
                (ws / d).mkdir(parents=True, exist_ok=True)
        need = [(p, e) for p, e in man["files"].items() if e["kind"] != "frame" or mode == "full"]
        todo = [(p, e) for p, e in need if not self._present(ws, p, e)]
        total = sum(e["size"] for _p, e in todo)
        if todo:
            stored = sum(e["size"] for _p, e in todo if e.get("hash") and self._store_path(e["hash"]).exists())
            self._evict(keep=ws, need=total - stored)
            free = shutil.disk_usage(self.cache_dir).free
            if free < (total - stored) + (1 << 30):
                raise _PackageBroken(f"not enough disk space here ({fmt_bytes(total - stored)} needed, "
                                     f"{fmt_bytes(free)} free)")
        self._progress = {"done": 0, "total": total, "t0": time.time(), "job": lease.get("job")}
        for p, e in todo:
            if self._abort_current:
                return ws, man
            self._ensure_file(lease, ws, p, e, store=e["kind"] != "frame" or mode == "full")
        self._progress = None
        return ws, man

    def _present(self, ws: Path, p: str, e: dict) -> bool:
        try:
            return (ws / p).stat().st_size == e["size"]
        except OSError:
            return False

    def _link(self, src: Path, dst: Path):
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.unlink()
        except OSError:
            pass
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)

    def _ensure_file(self, lease: dict, ws: Path, p: str, e: dict, store: bool, quiet: bool = False):
        dst = ws / p
        if self._present(ws, p, e):
            return
        h = e.get("hash")
        if store and h:
            sp = self._store_path(h)
            try:
                if sp.stat().st_size == e["size"]:
                    os.utime(sp)
                    self._link(sp, dst)
                    return
            except OSError:
                pass
        tmp, digest = self._download(lease, p, e, quiet)
        if store:
            sp = self._store_path(digest)
            sp.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, sp)
            self._link(sp, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dst)

    def _get_json(self, path: str) -> dict:
        u = urlparse(self.settings.worker_server)
        c = http.client.HTTPConnection(u.hostname, u.port or 8642, timeout=60)
        try:
            c.request("GET", path, headers=self._headers())
            r = c.getresponse()
            raw = r.read()
            try:
                d = json.loads(raw.decode() or "{}")
            except ValueError:
                d = {}
            if r.status != 200:
                raise CoordinatorError(r.status, d.get("error") or f"HTTP {r.status}")
            return d
        finally:
            c.close()

    def _download(self, lease: dict, p: str, e: dict, quiet: bool = False):
        """Fetch one file into the temp folder and check its checksum. Returns (path, sha256)."""
        u = urlparse(self.settings.worker_server)
        tmpdir = self.cache_dir / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        part = tmpdir / (hashlib.sha1(f"{lease['job_id']}/{p}".encode()).hexdigest()[:16] + ".part")
        delay, damaged = 2.0, 0
        try:
            import zstandard  # noqa: F401
            accept = "zstd"
        except ImportError:
            accept = ""
        while not self._stop.is_set():
            if self._abort_current:
                raise CoordinatorError(499, "stopped")
            have = part.stat().st_size if part.exists() else 0
            c = http.client.HTTPConnection(u.hostname, u.port or 8642, timeout=60)
            try:
                h = dict(self._headers())
                h["X-BBR-Accept"] = accept
                if have:
                    h["Range"] = f"bytes={have}-"
                c.request("GET", f"/api/v1/worker/file/{lease['job_id']}/{lease['package']}?path={quote(p)}",
                          headers=h)
                r = c.getresponse()
                if r.status in (404, 410):
                    msg = r.read()[:300].decode(errors="replace")
                    raise CoordinatorError(410, msg)
                if r.status not in (200, 206):
                    r.read()
                    raise CoordinatorError(r.status, f"download failed (HTTP {r.status})")
                want = (r.getheader("X-BBR-SHA256") or "").lower()
                enc = r.getheader("X-BBR-Encoding") or "identity"
                t0, last = time.time(), 0.0
                if enc == "zstd":
                    import zstandard
                    data = zstandard.ZstdDecompressor().decompress(r.read(), max_output_size=e["size"] + 1)
                    with open(part, "wb") as fh:
                        fh.write(data)
                else:
                    with open(part, "ab" if r.status == 206 else "wb") as fh:
                        got = have if r.status == 206 else 0
                        while True:
                            if self._abort_current:
                                raise CoordinatorError(499, "stopped")
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            fh.write(chunk)
                            got += len(chunk)
                            if self._progress is not None:
                                self._progress["done"] += len(chunk)
                            now = time.time()
                            if now - last > 0.5 and not quiet:
                                last = now
                                self._show_progress(p)
                digest = _sha256(str(part))
                if digest == want and (not e.get("hash") or e["hash"] == digest):
                    return part, digest
                damaged += 1
                part.unlink()
                self._console(f"{os.path.basename(p)} arrived damaged – downloading it again ({damaged})")
                if damaged >= 3:
                    raise _PackageBroken(f"{os.path.basename(p)} arrived damaged three times")
            except (CoordinatorError, _PackageBroken):
                raise
            except OSError as ex:
                self._set_state("downloading", f"connection lost ({ex.__class__.__name__}), resuming …")
                self._stop.wait(delay)
                delay = min(delay * 2, 30)
            finally:
                c.close()
        raise CoordinatorError(499, "stopped")

    def _show_progress(self, p: str):
        pr = self._progress
        if not pr:
            return
        rate = pr["done"] / max(0.001, time.time() - pr["t0"])
        self.live = {"download": pr["done"], "download_of": pr["total"], "rate": rate}
        self._set_state("downloading", f"{pr['job']}: {fmt_bytes(pr['done'])} of {fmt_bytes(pr['total'])} "
                                       f"({fmt_bytes(rate)}/s)")
        self.emit("worker_live", {})

    def _evict(self, keep: Path, need: float = 0):
        """Delete what was used longest ago until the cache fits its size limit."""
        limit = float(self.settings.worker_cache_gb or 50) * 1024 ** 3
        items = []                       # (last used, size, path, kind)
        total = 0
        jobs = self.cache_dir / "jobs"
        for job_dir in (jobs.iterdir() if jobs.exists() else []):
            for vdir in job_dir.iterdir() if job_dir.is_dir() else []:
                size = sum(f.stat().st_size for f in vdir.rglob("*") if f.is_file() and f.stat().st_nlink == 1)
                total += size
                if vdir != keep:
                    mf = vdir / ".manifest.json"
                    items.append(((mf.stat().st_mtime if mf.exists() else 0), size, vdir, "job"))
        store = self.cache_dir / "store"
        for f in (store.rglob("*") if store.exists() else []):
            if f.is_file():
                st = f.stat()
                total += st.st_size
                items.append((st.st_mtime, st.st_size, f, "file"))
        for _t, size, path, kind in sorted(items, key=lambda x: (x[3] != "job", x[0])):
            if total + need <= limit:
                break
            if kind == "job":
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except OSError:
                    continue
            total -= size
            self._console(f"Cache full – removed {path.name} ({fmt_bytes(size)})")

    def cache_usage(self) -> int:
        try:
            seen = set()
            total = 0
            for p in self.cache_dir.rglob("*"):
                if p.is_file():
                    st = p.stat()
                    if (st.st_dev, st.st_ino) in seen:
                        continue
                    seen.add((st.st_dev, st.st_ino))
                    total += st.st_size
            return total
        except OSError:
            return 0

    def clear_cache(self):
        if self.busy:
            return False
        for name in ("store", "jobs", "tmp", "work"):
            shutil.rmtree(self.cache_dir / name, ignore_errors=True)
        for p in self.cache_dir.glob("*.zip.part"):      # leftovers of version 0.4
            try:
                p.unlink()
            except OSError:
                pass
        for d in self.cache_dir.iterdir():
            if d.is_dir() and (d / ".complete").exists():
                shutil.rmtree(d, ignore_errors=True)
        return True


INPUT_ERROR = __import__("re").compile(
    r"(cannot (read|open|load)|can't (read|open|load)|unable to (read|open|load)|could not (read|open|load)|"
    r"not found|no such file|failed to (read|open|load))", __import__("re").IGNORECASE)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _PackageBroken(Exception):
    pass


class _Damaged(Exception):
    pass
