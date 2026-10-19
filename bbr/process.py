"""Running a Blender process, streaming its console line by line, sampling its
memory and killing it cleanly (including on timeout)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional

from .parse import split_lines

try:
    import psutil  # type: ignore
except ImportError:  # memory sampling is optional
    psutil = None


class BlenderProcess:
    def __init__(self, cmd: List[str], env: dict, log_path: str,
                 on_line: Callable[[str], None], cwd: Optional[str] = None):
        self.cmd = cmd
        self.env = env
        self.log_path = log_path
        self.on_line = on_line
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self.kill_reason: Optional[str] = None
        self.rss_mb: Optional[float] = None
        self.rss_peak_mb: Optional[float] = None   # peak since last reset_peak()
        self.rss_peak_total_mb: Optional[float] = None
        self._stop_watch = threading.Event()
        self.watch_hook: Optional[Callable[[], None]] = None   # called every second
        self.interactive = False      # True: we talk to the script through stdin (next-frame protocol)
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------
    def run(self) -> int:
        """Start, stream until exit, return exit code."""
        kwargs = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True   # own process group -> clean kill
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"# command: {' '.join(self.cmd)}\n# started: {time.ctime()}\n")
            log.flush()
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.PIPE if self.interactive else subprocess.DEVNULL,
                                         env=self.env, cwd=self.cwd, bufsize=0, **kwargs)
            watcher = threading.Thread(target=self._watch, daemon=True)
            watcher.start()
            buf = ""
            fd = self.proc.stdout.fileno()
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                lines, buf = split_lines(buf)
                for line in lines:
                    log.write(line + "\n")
                    try:
                        self.on_line(line)
                    except Exception as e:  # never let a handler bug kill the stream
                        log.write(f"# BBR handler error: {e!r}\n")
                log.flush()
            if buf:
                log.write(buf + "\n")
                try:
                    self.on_line(buf)
                except Exception:
                    pass
            rc = self.proc.wait()
            self._stop_watch.set()
            log.write(f"# exit code: {rc}  kill reason: {self.kill_reason}\n# ended: {time.ctime()}\n")
        return rc

    def send(self, line: str) -> bool:
        """Write one line to the script's stdin. False if Blender is gone."""
        p = self.proc
        if not p or not p.stdin or p.poll() is not None:
            return False
        try:
            with self._send_lock:
                p.stdin.write((line.rstrip("\n") + "\n").encode("utf-8"))
                p.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def kill(self, reason: str, grace: float = 5.0) -> None:
        """Terminate the process (and its children). Safe to call from any thread."""
        if self.kill_reason is None:
            self.kill_reason = reason
        p = self.proc
        if not p or p.poll() is not None:
            return

        def _do():
            try:
                if sys.platform.startswith("win"):
                    p.terminate()
                else:
                    os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    if sys.platform.startswith("win"):
                        p.kill()
                    else:
                        os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        threading.Thread(target=_do, daemon=True).start()

    def reset_peak(self) -> None:
        self.rss_peak_mb = self.rss_mb

    # ------------------------------------------------------------------
    def _watch(self) -> None:
        ps = None
        while not self._stop_watch.wait(1.0):
            if psutil is not None and self.proc is not None:
                try:
                    if ps is None:
                        ps = psutil.Process(self.proc.pid)
                    rss = ps.memory_info().rss
                    for c in ps.children(recursive=True):
                        try:
                            rss += c.memory_info().rss
                        except psutil.Error:
                            pass
                    mb = rss / (1024 * 1024)
                    self.rss_mb = mb
                    self.rss_peak_mb = max(self.rss_peak_mb or 0, mb)
                    self.rss_peak_total_mb = max(self.rss_peak_total_mb or 0, mb)
                except Exception:
                    pass
            if self.watch_hook:
                try:
                    self.watch_hook()
                except Exception:
                    pass
