"""Shared helpers for the multi-computer tests: a coordinator and in-process workers."""
import json  # noqa: F401
import os
import shutil
import socket  # noqa: F401
import stat
import sys
import tempfile
import time
import urllib.request  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="bbr_xfer_")
os.environ["BBR_DATA_DIR"] = os.path.join(TMP, "appdata")
os.environ["BBR_DISCOVERY_PORT"] = str(20000 + os.getpid() % 20000)

from bbr import scheduler as sched_mod  # noqa: E402
from bbr.api import ApiServer, DirectExecutor  # noqa: E402
from bbr.control import Controller  # noqa: E402
from bbr.installs import probe  # noqa: E402
from bbr.jobqueue import JobQueue  # noqa: E402
from bbr.models import AppSettings, JobSettings  # noqa: E402
from bbr.runner import Runner  # noqa: E402
from bbr.worker import Worker, pair, CoordinatorError, list_addons  # noqa: E402
from bbr.workerapi import discover  # noqa: E402
from bbr.package import build_package  # noqa: E402

FAKE = os.path.join(HERE, "fake_blender.py")
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def fake_exe(name, **env):
    """A Blender stand-in with its own environment (add-ons, speed, crashes) per machine."""
    p = os.path.join(TMP, "bin", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    lines = ["#!/bin/sh"] + [f"export {k}='{v}'" for k, v in env.items()]
    lines.append(f'exec "{sys.executable}" "{FAKE}" "$@"')
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return probe(p)


def wait_for(cond, timeout=60, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def make_blend(name, sub="src"):
    p = os.path.join(TMP, sub, name + ".blend")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"BLENDER-v402" + b"\0" * 100)
    return p


class Coordinator:
    def __init__(self, inst, render_locally=False):
        for k in list(os.environ):
            if k.startswith("FAKE_"):
                del os.environ[k]
        shutil.rmtree(os.environ["BBR_DATA_DIR"], ignore_errors=True)
        self.st = AppSettings.load()
        self.st.installs = [inst.to_dict()]
        self.st.keep_awake = False
        self.st.share_with_workers = True
        self.st.render_locally = render_locally
        self.st.api_port = 0
        self.q = JobQueue()
        self.q.load()
        self.events = []
        self.r = Runner(self.q, self.st, lambda k, p: self.events.append((k, p)))
        self.api = ApiServer(Controller(self.q, self.r, self.st), DirectExecutor(), self.st)
        assert self.api.start(), self.api.error
        self.port = self.api.httpd.server_address[1]
        self.st.api_port = self.port
        self.url = f"http://127.0.0.1:{self.port}"

    def add(self, blend, **kw):
        d = dict(render_root=os.path.join(TMP, "render"), retry_delay_s=0, make_preview=False,
                 consistency_check=False)
        d.update(kw)
        return self.q.add(blend, JobSettings(**d))

    def messages(self):
        return [p["text"] for k, p in self.events if k == "message"]

    def close(self):
        self.r.stop_now()
        self.r.wait(10)
        self.api.stop()
        self.r.scheduler.close()


def make_worker(c, name, inst, paired=True):
    ws = AppSettings()
    ws.save = lambda: None
    ws.installs = [inst.to_dict()]
    ws.worker_id = "w" + name.lower().replace(" ", "")[:10] + "x"
    ws.worker_name = name
    ws.mode = "worker"
    w = Worker(ws, lambda k, p: None)
    w.cache_dir = __import__("pathlib").Path(TMP) / f"cache_{ws.worker_id}"
    w.cache_dir.mkdir(parents=True, exist_ok=True)
    if paired:
        ws.worker_server = c.url
        ws.worker_key = pair(c.url, c.st.pairing_code, ws.worker_id, name)["key"]
    return w


