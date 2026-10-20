"""A small web API and status page, built on Python's standard library.

Security model
- Off the home network by default: the server only listens on 127.0.0.1.
- "Allow devices on my home network" makes it listen on all interfaces; every
  request from another device must then carry the API key (header
  X-API-Key, or ?key=… for opening the status page).
- Requests from this computer need no key, but:
  * the Host header must be localhost/127.0.0.1 (blocks DNS-rebinding tricks),
  * requests that change something need the header X-BBR-Request: 1, which a
    random web page in your browser cannot send to another site without
    permission (no CORS headers are ever sent).

API (all JSON; v1)
  GET    /api/v1/status                    app + batch state, live frame info
  GET    /api/v1/queue                     folders and jobs in render order
  GET    /api/v1/jobs/<id>                 job details
  GET    /api/v1/jobs/<id>/thumbnail       last finished frame (PNG/JPG/TIFF/WebP)
  GET    /api/v1/console?since=N[&job=id]  recent Blender console lines
  POST   /api/v1/jobs                      add a job  {"blend": "/path/file.blend", ...}
  POST   /api/v1/batch/start
  POST   /api/v1/batch/stop                {"mode": "after_frame" | "now"}
  POST   /api/v1/batch/cancel-job
  POST   /api/v1/jobs/<id>/retry | /reset | /preview
  POST   /api/v1/jobs/<id>/enabled         {"enabled": true|false}
  DELETE /api/v1/jobs/<id>                 remove from the queue (files stay)
  POST   /api/v1/queue/open                {"path": "/path/x.bbrqueue"}  (window only)
  GET    /api/v1/machines                  computers taking part (per machine + combined statistics)
  POST   /api/v1/machines/<id>/enabled     {"enabled": true|false}
  POST   /api/v1/machines/<id>/command     {"command": "pause" | "stop" | "resume"}
  DELETE /api/v1/machines/<id>             forget a worker
  /api/v1/worker/...                       used by worker machines, see workerapi.py
  GET    /                                 status page for a browser
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs

from .control import ApiError, Controller
from .workerapi import DiscoveryResponder, WorkerEndpoints, WorkerError

ASSETS = Path(__file__).resolve().parent / "assets"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
MAX_BODY = 1 << 20


class DirectExecutor:
    """Runs controller calls right away (command line / tests), one at a time."""

    def __init__(self):
        self._lock = threading.RLock()

    def run(self, fn: Callable):
        with self._lock:
            return fn()


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1") or ip.startswith("127.") or ip.startswith("::ffff:127.")


class ApiServer:
    def __init__(self, controller: Controller, executor, settings):
        self.controller = controller
        self.executor = executor
        self.settings = settings
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        self.workers = WorkerEndpoints(controller.runner, settings)
        self.discovery = DiscoveryResponder(settings, controller.runner.scheduler.local.name)

    @property
    def running(self) -> bool:
        return self.httpd is not None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.settings.api_port}/"

    def lan_urls(self):
        import socket
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        try:   # the address used for the default route
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 9))
            ips.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
        ips = sorted(ip for ip in ips if not ip.startswith("127."))
        return [f"http://{ip}:{self.settings.api_port}/?key={self.settings.api_key}" for ip in ips]

    def start(self) -> bool:
        self.stop()
        self.error = None
        if not self.settings.api_enabled:
            return False
        lan = self.settings.api_allow_lan or (self.settings.share_with_workers and self.settings.mode == "coordinator")
        host = os.environ.get("BBR_API_HOST") or ("0.0.0.0" if lan else "127.0.0.1")
        handler = _make_handler(self)
        try:
            self.httpd = ThreadingHTTPServer((host, int(self.settings.api_port)), handler)
        except OSError as e:
            self.httpd = None
            self.error = f"port {self.settings.api_port} is not available ({e.strerror or e})"
            return False
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="bbr-api", daemon=True)
        self.thread.start()
        if self.settings.share_with_workers and self.settings.mode == "coordinator":
            self.discovery.start()
        return True

    def stop(self) -> None:
        self.discovery.stop()
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


def _make_handler(server: ApiServer):
    ctl = server.controller
    ex = server.executor
    st = server.settings

    routes = [
        ("GET", r"/api/v1/status", lambda m, q, b: ctl.status()),
        ("GET", r"/api/v1/queue", lambda m, q, b: ctl.queue_list()),
        ("GET", r"/api/v1/jobs/([\w-]+)", lambda m, q, b: ctl.job_detail(m.group(1))),
        ("GET", r"/api/v1/console", lambda m, q, b: ctl.console(int((q.get("since") or ["0"])[0] or 0),
                                                                (q.get("job") or [None])[0])),
        ("POST", r"/api/v1/jobs", lambda m, q, b: ctl.add_job(b)),
        ("POST", r"/api/v1/batch/start", lambda m, q, b: ctl.start()),
        ("POST", r"/api/v1/batch/stop", lambda m, q, b: ctl.stop(b.get("mode", "after_frame"))),
        ("POST", r"/api/v1/batch/cancel-job", lambda m, q, b: ctl.cancel_current()),
        ("POST", r"/api/v1/jobs/([\w-]+)/retry", lambda m, q, b: ctl.retry(m.group(1))),
        ("POST", r"/api/v1/jobs/([\w-]+)/reset", lambda m, q, b: ctl.reset(m.group(1))),
        ("POST", r"/api/v1/jobs/([\w-]+)/preview", lambda m, q, b: ctl.preview(m.group(1))),
        ("POST", r"/api/v1/jobs/([\w-]+)/enabled", lambda m, q, b: ctl.set_enabled(m.group(1), bool(b.get("enabled")))),
        ("DELETE", r"/api/v1/jobs/([\w-]+)", lambda m, q, b: ctl.remove(m.group(1))),
        ("GET", r"/api/v1/machines", lambda m, q, b: ctl.machines()),
        ("POST", r"/api/v1/machines/([\w-]+)/enabled", lambda m, q, b: ctl.set_machine_enabled(m.group(1), bool(b.get("enabled")))),
        ("DELETE", r"/api/v1/machines/([\w-]+)", lambda m, q, b: ctl.forget_machine(m.group(1))),
        ("POST", r"/api/v1/machines/([\w-]+)/command", lambda m, q, b: ctl.machine_command(m.group(1), str(b.get("command", "")))),
        ("POST", r"/api/v1/queue/open", lambda m, q, b: ctl.open_queue(str(b.get("path", "")))),
    ]
    compiled = [(meth, re.compile("^" + pat + "/?$"), fn) for meth, pat, fn in routes]

    class Handler(BaseHTTPRequestHandler):
        server_version = "BlenderBatchRenderer"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):     # keep the console quiet
            pass

        # ---- helpers ------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, data):
            self._send(code, json.dumps(data, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _key_ok(self, q) -> bool:
            key = self.headers.get("X-API-Key") or (q.get("key") or [""])[0]
            return bool(st.api_key) and key == st.api_key

        def _check(self, q, changing: bool) -> Optional[tuple]:
            ip = self.client_address[0]
            local = _is_loopback(ip)
            key_ok = self._key_ok(q)
            if not local:
                if not st.api_allow_lan:
                    return 403, "access from other devices is switched off"
                if not key_ok:
                    return 401, "API key missing or wrong"
            else:
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower()
                if host not in LOCAL_HOSTS and not key_ok:
                    return 403, "unexpected Host header"
            if changing and not (key_ok or self.headers.get("X-BBR-Request") == "1"):
                return 403, "missing X-BBR-Request header"
            return None

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "request too large")
            if not n:
                return {}
            try:
                d = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                raise ApiError(400, "body is not valid JSON")
            if not isinstance(d, dict):
                raise ApiError(400, "body must be a JSON object")
            return d

        # ---- dispatch -----------------------------------------------------
        def _handle(self, method: str):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path
            if path.startswith("/api/v1/worker/"):
                return self._worker(method, path, q)
            bad = self._check(q, changing=method in ("POST", "DELETE"))
            if bad:
                return self._json(bad[0], {"error": bad[1]})
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    return self._send(200, (ASSETS / "status.html").read_bytes(), "text/html; charset=utf-8")
                if method == "GET" and path == "/icon.png":
                    return self._send(200, (ASSETS / "icon.png").read_bytes(), "image/png")
                m = re.match(r"^/api/v1/jobs/([\w-]+)/thumbnail/?$", path)
                if method == "GET" and m:
                    p = ex.run(lambda: ctl.last_frame_path(m.group(1)))
                    if not p:
                        return self._json(404, {"error": "no finished frame yet"})
                    ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
                    if not ctype.startswith("image/") or p.lower().endswith((".exr", ".hdr")):
                        return self._json(415, {"error": "no browser preview for this image format"})
                    return self._send(200, Path(p).read_bytes(), ctype)
                body = self._body() if method in ("POST", "DELETE") else {}
                for meth, rx, fn in compiled:
                    if meth != method:
                        continue
                    m = rx.match(path)
                    if m:
                        result = ex.run(lambda: fn(m, q, body))
                        return self._json(200, result)
                return self._json(404, {"error": f"unknown endpoint {method} {path}"})
            except ApiError as e:
                return self._json(e.status, {"error": e.message})
            except TimeoutError:
                return self._json(503, {"error": "the app is busy, try again"})
            except Exception as e:      # never take the app down
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass        # the other side went away (sleeping laptop, Wi-Fi drop)

        def _worker(self, method, path, q):
            self.close_connection = True       # one request per connection keeps this simple and robust
            try:
                if not (st.share_with_workers and st.mode == "coordinator"):
                    raise WorkerError(403, "this computer does not share jobs with other machines right now")
                return server.workers.handle(self, method, path, q)
            except (WorkerError, ApiError) as e:
                self.close_connection = True
                return self._json(e.status, {"error": e.message})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as e:
                self.close_connection = True
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_PUT(self):
            u = urlparse(self.path)
            if u.path.startswith("/api/v1/worker/"):
                return self._worker("PUT", u.path, parse_qs(u.query))
            self._json(405, {"error": "PUT is only used by workers"})

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


def forward_to_running_app(settings, path: str, timeout: float = 3.0) -> bool:
    """Ask an already running app to open a queue file (used when a second copy starts)."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{settings.api_port}/api/v1/queue/open",
            data=json.dumps({"path": os.path.abspath(path)}).encode(),
            headers={"Content-Type": "application/json", "X-BBR-Request": "1"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
