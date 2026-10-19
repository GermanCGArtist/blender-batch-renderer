"""Command line use (no window):

    python -m bbr.cli add FILE.blend [--root DIR] [--start N --end N ...]
    python -m bbr.cli list
    python -m bbr.cli run            # render the queue; Ctrl+C = stop now
    python -m bbr.cli scan           # find Blender installs and save them
"""
from __future__ import annotations

import argparse
import signal
import sys

from .installs import scan, probe
from .jobqueue import JobQueue
from .models import AppSettings, JobSettings
from .parse import fmt_duration


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bbr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("files", nargs="+")
    a.add_argument("--root", default="")
    a.add_argument("--blender", default="auto")
    for k in ("start", "end", "samples", "pct", "attempts", "delay"):
        a.add_argument(f"--{k}", type=int)
    a.add_argument("--timeout", type=float)
    a.add_argument("--no-preview", action="store_true")
    a.add_argument("--preflight", action="store_true")
    a.add_argument("--folder", default="", help="queue folder name (created if missing)")
    sub.add_parser("list")
    sub.add_parser("run")
    sc = sub.add_parser("scan")
    sc.add_argument("extra", nargs="*", help="extra install paths")
    args = ap.parse_args(argv)

    settings = AppSettings.load()
    if args.cmd in ("add", "run"):
        from .storage import InstanceLock
        lock = InstanceLock()
        if not lock.acquire():
            print("Blender Batch Renderer is already running (window or command line). Close it first.")
            return 1
    q = JobQueue()
    for w in q.load():
        print("warning:", w)

    if args.cmd == "scan":
        found = scan() + [i for i in (probe(p) for p in args.extra) if i]
        settings.installs = [i.to_dict() for i in found]
        settings.save()
        for i in found:
            print(f"{i.label:30s} {i.executable}")
        return 0
    if args.cmd == "add":
        for f in args.files:
            s = JobSettings.from_dict(settings.defaults)
            s.render_root = args.root
            s.blender = args.blender
            s.frame_start, s.frame_end, s.samples, s.resolution_pct = args.start, args.end, args.samples, args.pct
            if args.attempts:
                s.max_attempts = args.attempts
            if args.delay is not None:
                s.retry_delay_s = args.delay
            if args.timeout is not None:
                s.frame_timeout_min = args.timeout
            s.make_preview = not args.no_preview
            s.preflight = args.preflight
            gid = ""
            if args.folder:
                g = next((g for g in q.groups if g["name"] == args.folder), None) or q.add_group(args.folder)
                gid = g["id"]
            j = q.add(f, s, group=gid)
            print(f"added {j.name} -> {j.folder}")
        return 0
    if args.cmd == "list":
        for j in q.ordered_jobs():
            g = q.group(j.group)
            name = f"{g['name']}/{j.name}" if g else j.name
            skip = "" if q.is_active(j) else " (skipped)"
            print(f"{name:40s} {j.status + skip:30s} {j.count('done')}/{j.total_frames or '?'}  {j.folder}")
        return 0
    if args.cmd == "run":
        from .runner import Runner

        def emit(kind, p):
            if kind == "console":
                print("   |", p["text"])
            elif kind == "message":
                print(f"[{p['level']}] {p['text']}")
            elif kind == "frame_done":
                j = q.find(p["job_id"])
                print(f"== {j.name} frame {p['frame']} done  ({j.count('done')}/{j.total_frames}) "
                      f"ETA {fmt_duration(j.eta_seconds())}")

        r = Runner(q, settings, emit)
        api = None
        if settings.api_enabled:
            from .api import ApiServer, DirectExecutor
            from .control import Controller
            api = ApiServer(Controller(q, r, settings), DirectExecutor(), settings)
            if api.start():
                print(f"status page: {api.url}")
            else:
                print(f"API not started: {api.error}")
        signal.signal(signal.SIGINT, lambda *a: r.stop_now())
        r.start()
        while r.running:
            r.wait(0.5)
        if api:
            api.stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())
