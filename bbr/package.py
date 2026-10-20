"""Prepares a job for other computers ("job copy").

Nothing is zipped or packed anymore. Blender writes copies of the .blend files whose paths point
into a fixed layout (see blender_scripts/bbr_package.py), and the app lists every file a worker
needs with its size and checksum:

  scene      main.blend and the linked libraries (the copies)      sent before rendering
  asset      whole files every frame may need (textures, caches)   sent before rendering, cached
                                                                    on the worker by checksum
  frame      one file per frame (image / VDB sequences, caches)     Smart transfer: fetched frame by
                                                                    frame; Full copy: sent up front

job.package holds a short summary (it is saved with the job after every frame); the full list
lives in <job>/package/v_<version>/manifest.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional

from .hashing import shared as hash_cache
from .parse import parse_marker
from .process import BlenderProcess
from .storage import atomic_write_json, read_json

# Raise when the job copy changes in a way workers depend on: older copies are then made again.
PACKAGE_FORMAT = 3       # 2: indirect libraries next to their parent in libs/; 3: library paths resolved robustly

_manifests: "OrderedDict[str, dict]" = OrderedDict()
_mlock = threading.Lock()


# ---- frame mapping (mirrors Blender's image user / volume sequence rules) --------------------
def file_frame(user: dict, f: int) -> Optional[int]:
    dur = int(user.get("duration") or 0)
    c = f - int(user.get("start", 1)) + 1
    mode = user.get("mode")
    if dur > 0:
        if user.get("cyclic") or mode == "REPEAT":
            c %= dur
            if c <= 0:
                c += dur
        elif mode == "CLIP" and (c < 1 or c > dur):
            return None
        elif mode == "EXTEND":
            c = min(max(c, 1), dur)
        else:
            c = min(max(c, 0), dur)
    return c + int(user.get("offset") or 0)


def frames_needed(seq: dict, f: int) -> Optional[set]:
    """File frame numbers a sequence needs for scene frame f; None = cannot tell (send all)."""
    if seq.get("whole"):
        return None
    w = int(seq.get("window") or 0)
    users = seq.get("users") or []
    if any(u.get("mode") == "PING_PONG" for u in users):
        return None
    out = set()
    for g in range(f - w, f + w + 1):
        if not users:
            out.add(g)                 # caches: file frame = scene frame
        for u in users:
            n = file_frame(u, g)
            if n is not None:
                out.add(n)
    return out


# ---- building ----------------------------------------------------------------------------------
def build_package(job, inst, on_line, frames: Optional[List[int]] = None) -> dict:
    from .runner import PACKAGE_SCRIPT, env_for
    pdir = job.package_dir
    build = pdir / "build"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    s = job.settings
    args = {"out_dir": str(build),
            "overrides": {"frame_start": s.frame_start, "frame_end": s.frame_end,
                          "samples": s.samples, "resolution_pct": s.resolution_pct}}
    job.ensure_folders()
    args_path = job.tmp_dir / "package_args.json"
    atomic_write_json(args_path, args)
    result = {}

    def handle(line):
        on_line(line)
        mk = parse_marker(line)
        if mk and mk[0] in ("PACKAGE", "PACKAGE_ERROR"):
            result[mk[0]] = mk[1]

    stamp_before = job.current_blend_stamp()
    cmd = [inst.executable, "-b", job.blend_path, "--python-exit-code", "2",
           "--python", PACKAGE_SCRIPT, "--", str(args_path)]
    rc = BlenderProcess(cmd, env_for(job), str(job.logs_dir / "package.log"), handle,
                        cwd=os.path.dirname(job.blend_path) or None).run()
    if "PACKAGE" not in result or rc != 0 or not (build / "main.blend").exists():
        err = (result.get("PACKAGE_ERROR") or {}).get("error", "") or f"Blender exited with code {rc}"
        raise RuntimeError(err.strip().splitlines()[-1][:300] + "  (see logs/package.log)")
    r = result["PACKAGE"]
    hc = hash_cache()

    files: Dict[str, dict] = {}

    def add(path, src, kind, hash_now):
        st = os.stat(src)
        files[path] = {"src": str(src), "size": st.st_size, "mtime_ns": st.st_mtime_ns, "kind": kind,
                       "hash": hc.get(str(src)) if hash_now else hc.lookup(str(src))}

    # the .blend copies
    for root, _dirs, fns in os.walk(build):
        for fn in fns:
            full = os.path.join(root, fn)
            add(os.path.relpath(full, build).replace(os.sep, "/"), full, "scene", True)
    # whole files
    for rel, src in (r.get("assets") or {}).items():
        try:
            add(rel, src, "asset", True)
        except OSError:
            r.setdefault("missing", []).append(src)
    # sequences
    si = r.get("scene_info") or {}
    if frames is None and si.get("frame_start") is not None:
        frames = list(range(int(si["frame_start"]), int(si["frame_end"]) + 1, max(1, int(si.get("frame_step") or 1))))
    frames = frames or []
    per_frame: Dict[str, List[str]] = {str(f): [] for f in frames}
    whole_seqs = 0
    for seq in r.get("sequences") or []:
        by_num: Dict[int, List[str]] = {}
        paths = []
        for e in seq["files"]:
            rel = seq["dir"] + "/" + e["name"]
            src = os.path.join(seq["src_dir"], *e["name"].split("/"))
            try:
                add(rel, src, "frame", False)
            except OSError:
                continue
            paths.append(rel)
            if e.get("frame") is not None:
                by_num.setdefault(int(e["frame"]), []).append(rel)
            else:
                files[rel]["kind"] = "asset"          # e.g. config files of a fluid cache
        mappable = not seq.get("whole") and by_num
        if not mappable:
            whole_seqs += 1
            for p in paths:
                files[p]["kind"] = "asset"
            continue
        used = set()
        for f in frames:
            nums = frames_needed(seq, f)
            if nums is None:
                for p in paths:
                    files[p]["kind"] = "asset"
                break
            for n in nums:
                for p in by_num.get(n, []):
                    per_frame[str(f)].append(p)
                    used.add(p)
        # frame files no frame of this job uses are not sent at all
        for p in paths:
            if files[p]["kind"] == "frame" and p not in used:
                del files[p]
    # versions: a checksum over what defines the job copy
    h = hashlib.sha256()
    for p in sorted(files):
        e = files[p]
        h.update(f"{p}\0{e['size']}\0{e['hash'] or e['mtime_ns']}\n".encode())
    version = h.hexdigest()[:16]
    vdir = pdir / f"v_{version}"
    if vdir.exists():
        shutil.rmtree(build, ignore_errors=True)
    else:
        os.replace(build, vdir)
    for p, e in files.items():                 # scene copies now live in the version folder
        if e["kind"] == "scene":
            e["src"] = str(vdir / p)
    for old in pdir.glob("v_*"):
        if old != vdir:
            shutil.rmtree(old, ignore_errors=True)
    for old in pdir.glob("*.zip"):              # job copies of v0.4
        try:
            old.unlink()
        except OSError:
            pass
    hc.save()

    upfront = sum(e["size"] for e in files.values() if e["kind"] != "frame")
    stream = [sum(files[p]["size"] for p in set(v) if p in files) for v in per_frame.values()]
    per_frame = {k: sorted(set(v)) for k, v in per_frame.items() if v}
    manifest = {
        "version": version, "files": files, "frames": per_frame, "missing": sorted(set(r.get("missing") or [])),
    }
    atomic_write_json(vdir / "manifest.json", manifest)
    with _mlock:
        _manifests[version] = manifest
    summary = {
        "hash": version, "built_at": time.time(), "blend_stamp": stamp_before, "format": PACKAGE_FORMAT,
        "blender": f"{inst.version[0]}.{inst.version[1]}", "blender_label": inst.label,
        "scene_info": si, "addons": r.get("addons") or [], "addons_required": r.get("addons_required") or [],
        "libraries": r.get("libraries") or [], "warnings": r.get("warnings") or [],
        "unbaked": r.get("unbaked") or [], "missing": len(manifest["missing"]),
        "files": len(files), "size": upfront, "size_upfront": upfront,
        "size_total": sum(e["size"] for e in files.values()),
        "stream_bytes_per_frame": (sum(stream) / len(stream)) if stream and any(stream) else 0,
        "sequences_streamed": sum(1 for _ in (r.get("sequences") or [])) - whole_seqs,
    }
    return summary


# ---- reading ----------------------------------------------------------------------------------
def load_manifest(job) -> Optional[dict]:
    v = (job.package or {}).get("hash")
    if not v:
        return None
    with _mlock:
        m = _manifests.get(v)
        if m is not None:
            _manifests.move_to_end(v)
            return m
    m = read_json(job.package_dir / f"v_{v}" / "manifest.json")
    if not m or m.get("version") != v:
        return None
    with _mlock:
        _manifests[v] = m
        while len(_manifests) > 8:
            _manifests.popitem(last=False)
    return m


def worker_manifest(job) -> Optional[dict]:
    """What a worker needs to know: paths, sizes, checksums, kinds and the per-frame lists."""
    m = load_manifest(job)
    if m is None:
        return None
    return {"version": m["version"],
            "files": {p: {"size": e["size"], "hash": e["hash"], "kind": e["kind"]} for p, e in m["files"].items()},
            "frames": m["frames"],
            "missing": [os.path.basename(x) for x in m.get("missing") or []]}


def package_current(job) -> bool:
    """Is the job copy still up to date (the .blend and its linked libraries unchanged)?"""
    pkg = job.package or {}
    if not pkg.get("hash") or pkg.get("blend_stamp") != job.current_blend_stamp():
        return False
    if pkg.get("format") != PACKAGE_FORMAT:
        return False                    # made by an older version of the app
    for lib in pkg.get("libraries") or []:
        try:
            st = os.stat(lib["path"])
        except OSError:
            continue
        if [round(st.st_mtime, 3), st.st_size] != lib.get("stamp"):
            return False
    return True
