"""Runs API requests on the GUI thread, so the queue is never changed from two threads."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThread, Qt, Signal


class _Caller(QObject):
    call = Signal(object)


class QtExecutor:
    def __init__(self):
        self._obj = _Caller()
        self._obj.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _run(job):
        try:
            job["result"] = job["fn"]()
        except BaseException as e:      # handed back to the API thread
            job["error"] = e
        finally:
            job["event"].set()

    def run(self, fn, timeout: float = 20.0):
        if QThread.currentThread() is self._obj.thread():
            return fn()
        job = {"fn": fn, "event": threading.Event()}
        self._obj.call.emit(job)
        if not job["event"].wait(timeout):
            raise TimeoutError("the window did not answer in time")
        if "error" in job:
            raise job["error"]
        return job.get("result")
