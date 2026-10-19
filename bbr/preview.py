"""Build preview/<job>.mp4 from the finished frames using Blender's video editor."""
from __future__ import annotations

import glob
import os
import shutil
import time
from pathlib import Path

from .process import BlenderProcess
from .storage import atomic_write_json


def build_preview(job, inst, on_line) -> str:
    from .runner import PREVIEW_SCRIPT, env_for
    done = [r for r in job.done_records() if r.get("file") and os.path.exists(r["file"])]
    if not done:
        raise RuntimeError("no finished frames yet")
    si = job.scene_info or {}
    # contiguous runs of frames -> one image strip each; gaps stay black
    step = max(1, int(si.get("frame_step", 1) or 1))
    runs, cur = [], None
    for r in done:
        if cur and r["frame"] == cur["last"] + step:
            cur["files"].append(os.path.basename(r["file"]))
            cur["last"] = r["frame"]
        else:
            cur = {"start": r["frame"], "last": r["frame"], "dir": os.path.dirname(r["file"]),
                   "files": [os.path.basename(r["file"])]}
            runs.append(cur)
    pct = (si.get("res_pct") or 100) / 100.0
    w = int((si.get("res_x") or 1920) * pct) // 2 * 2
    h = int((si.get("res_y") or 1080) * pct) // 2 * 2
    ext = (si.get("file_extension") or "").lower()
    out_tmp = job.preview_dir / "_encoding"
    shutil.rmtree(out_tmp, ignore_errors=True)
    out_tmp.mkdir(parents=True, exist_ok=True)
    args = {
        "runs": runs, "step": step, "frame_start": done[0]["frame"], "frame_end": done[-1]["frame"],
        "fps": si.get("fps") or 24, "fps_base": si.get("fps_base") or 1.0,
        "res_x": max(2, w), "res_y": max(2, h), "out_dir": str(out_tmp),
        "linear_input": ext in (".exr", ".hdr"),
        "view_transform": si.get("view_transform"), "look": si.get("look"),
        "exposure": si.get("exposure"), "gamma": si.get("gamma"),
    }
    args_path = job.tmp_dir / "preview_args.json"
    atomic_write_json(args_path, args)
    cmd = [inst.executable, "-b", "--factory-startup", "--python-exit-code", "2",
           "--python", PREVIEW_SCRIPT, "--", str(args_path)]
    log = job.logs_dir / "preview.log"
    rc = BlenderProcess(cmd, env_for(job), str(log), on_line).run()
    vids = sorted(glob.glob(str(out_tmp / "*.mp4")), key=os.path.getmtime)
    if rc != 0 or not vids:
        raise RuntimeError(f"Blender exited with {rc}, see {log}")
    final = job.preview_dir / f"{job.name}.mp4"
    try:
        os.replace(vids[-1], final)
    except OSError:
        final = job.preview_dir / f"{job.name}_{time.strftime('%H%M%S')}.mp4"
        os.replace(vids[-1], final)
    shutil.rmtree(out_tmp, ignore_errors=True)
    return str(final)
