"""Stop the computer from sleeping while a batch runs."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


class KeepAwake:
    def __init__(self):
        self._proc = None
        self._win = False
        self.active = False

    def start(self) -> bool:
        if self.active:
            return True
        try:
            if sys.platform == "darwin" and shutil.which("caffeinate"):
                # -i: no idle sleep, -s: no system sleep on AC power,
                # -w: ends by itself if this app dies.
                self._proc = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
            elif sys.platform.startswith("win"):
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self._win = True
            elif shutil.which("systemd-inhibit"):
                self._proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle:sleep", "--who=Blender Batch Renderer",
                     "--why=Rendering", "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                return False
        except Exception:
            return False
        self.active = True
        return True

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        if self._win:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass
            self._win = False
        self.active = False
