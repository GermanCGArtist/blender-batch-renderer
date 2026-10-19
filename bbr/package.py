"""Builds the self-contained copy of a job that worker machines download."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile

from .process import BlenderProcess
from .storage import atomic_write_json
from .parse import parse_marker


def build_package(job, inst, on_line) -> dict:
    from .runner import PACKAGE_SCRIPT, env_for
    pdir = job.package_dir
    build = pdir / "build"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    s = job.settings
    args = {"out_dir": str(build),
            "overrides": {"frame_start": s.frame_start, "frame_end": s.frame_end,
                          "samples": s.samples, "resolution_pct": s.resolution_pct}}
    args_path = job.tmp_dir / "package_args.json"
    job.ensure_folders()
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
    manifest = result["PACKAGE"]

    # zip it (no compression: .blend files are already compressed, images too)
    tmp_zip = pdir / ".tmp_package.zip"
    h = hashlib.sha256()
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for root, _dirs, files in os.walk(build):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, build).replace(os.sep, "/"))
    with open(tmp_zip, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()[:20]
    final = pdir / f"{digest}.zip"
    for old in pdir.glob("*.zip"):
        if old != final and not old.name.startswith("."):
            try:
                old.unlink()
            except OSError:
                pass
    os.replace(tmp_zip, final)
    shutil.rmtree(build, ignore_errors=True)
    manifest.update({
        "hash": digest, "file": final.name, "size": final.stat().st_size, "built_at": time.time(),
        "blend_stamp": stamp_before,
        "blender": f"{inst.version[0]}.{inst.version[1]}", "blender_label": inst.label,
    })
    atomic_write_json(pdir / "manifest.json", manifest)
    return manifest


def package_file(job):
    pkg = job.package or {}
    if not pkg.get("file"):
        return None
    p = job.package_dir / pkg["file"]
    return p if p.exists() else None
