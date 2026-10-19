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
    wk = sub.add_parser("worker", help="render frames for a coordinator on the home network")
    wk.add_argument("--server", default="", help="coordinator address, e.g. 192.168.1.20 (found automatically if left out)")
    wk.add_argument("--code", default="", help="pairing code shown on the coordinator (first time only)")
    wk.add_argument("--name", default="", help="name shown on the coordinator")
    sc = sub.add_parser("scan")
    sc.add_argument("extra", nargs="*", help="extra install paths")
    args = ap.parse_args(argv)

    settings = AppSettings.load()
    if args.cmd in ("add", "run", "worker"):
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
    if args.cmd == "worker":
        return _worker_cli(settings, args)
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


def _worker_cli(settings, args) -> int:
    import time
    from .worker import Worker, pair, normalize_server, CoordinatorError
    from .workerapi import discover
    if not settings.installs:
        print("No Blender installs configured – run:  bbr scan")
        return 1
    if args.name:
        settings.worker_name = args.name
    server = normalize_server(args.server) if args.server else settings.worker_server
    if not server:
        found = discover()
        if not found:
            print("No coordinator found on the network. Pass --server <address>.")
            return 1
        server = found[0]["url"]
        print(f"found coordinator {found[0]['name']} at {server}")
    if server != settings.worker_server or args.code:
        if not args.code:
            print(f"Not paired with {server} yet. Pass --code <pairing code> (shown on the coordinator).")
            return 1
        try:
            d = pair(server, args.code, settings.worker_id, settings.worker_name or "")
        except CoordinatorError as e:
            print(f"pairing failed: {e.message}")
            return 1
        settings.worker_server, settings.worker_key = server, d["key"]
        print(f"paired with {d.get('coordinator')}")
    settings.mode = "worker"
    settings.save()

    def emit(kind, p):
        if kind == "message":
            print(f"[{p['level']}] {p['text']}")

    w = Worker(settings, emit)
    w.accepting = True
    signal.signal(signal.SIGINT, lambda *a: w.stop_now())
    w.start()
    last = ""
    try:
        while w.running:
            line = f"{w.state}: {w.note}"
            if line != last:
                print(line)
                last = line
            if w.state == "paused" and not w.accepting:
                break
            time.sleep(0.5)
    finally:
        w.stop(now=True)
        s = w.session
        print(f"session: {s['frames']} frames, {s['failed']} failed, {fmt_duration(s['render_s'])} rendering")
    return 0


if __name__ == "__main__":
    sys.exit(main())
