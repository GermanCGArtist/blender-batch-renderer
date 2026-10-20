# Runs inside Blender:  blender -b file.blend --python bbr_render.py -- args.json
#
# Renders every frame of the active scene that is not listed in args["skip"],
# one at a time. Each frame is saved to a temp name and renamed when complete,
# so a killed render never leaves a half-written frame behind.
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy  # noqa: E402
import bbr_common as C  # noqa: E402


def frame_name(prefix, frame, ext):
    return "%s%s%04d%s" % (prefix, "-" if frame < 0 else "", abs(frame), ext)


def main():
    args = C.script_args()
    scene = C.pick_scene(args.get("overrides"))
    rkw = C.render_kwargs(scene, args.get("overrides"))
    frames_dir = args["frames_dir"]
    prefix = args.get("prefix", "")
    os.makedirs(frames_dir, exist_ok=True)

    applied = C.apply_overrides(scene, args.get("overrides"))
    note = C.ensure_image_output(scene)
    redirected = C.redirect_file_outputs(scene, frames_dir)

    info = C.scene_info(scene)
    info["overrides_applied"] = applied
    info["format_note"] = note
    info["file_outputs_redirected"] = redirected
    C.out("JOB_INFO", info)
    if note:
        C.warn(note)
    if redirected:
        C.warn("Compositor File Output nodes redirected into frames/<node name>/: " + ", ".join(redirected))

    ci = args.get("check_inputs")
    if ci is not None:
        missing = C.missing_inputs(ci.get("ignore") or [])
        if missing:
            C.out("INPUT_MISSING", {"files": missing[:50]})
            sys.exit(5)

    if scene.camera is None and not any(m.camera for m in scene.timeline_markers):
        C.out("FATAL", {"error": "Scene '%s' has no active camera" % scene.name})
        sys.exit(4)

    last = {"stats": "", "sent": 0.0, "peak": None}

    def on_stats(*a):
        # Blender 5+ no longer prints progress lines in background mode, so forward
        # the stats string (samples, remaining time, memory) to the batch renderer.
        if not a:
            return
        text = str(a[0])
        last["stats"] = text
        pk = C.peak_mb(text)
        if pk is not None:
            last["peak"] = pk if last["peak"] is None else max(last["peak"], pk)
        now = time.time()
        if now - last["sent"] >= 0.25:
            last["sent"] = now
            C.out("STATS", {"s": text})

    bpy.app.handlers.render_stats.append(on_stats)

    ext = scene.render.file_extension
    skip = set(int(f) for f in args.get("skip", []))
    frames = list(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))
    todo = [f for f in frames if f not in skip]
    C.out("PLAN", {"total": len(frames), "todo": len(todo)})
    max_frames = int(args.get("max_frames") or 0)   # restart Blender every N frames
    dynamic = bool(args.get("dynamic"))
    extra_dirs = [os.path.join(frames_dir, C.safe_dir_name(n)) for n in redirected]

    state = {"rendered": 0, "chunk": False}

    def chunk_full():
        if max_frames and state["rendered"] >= max_frames:
            state["chunk"] = True
            return True
        return False

    def next_frames():
        """Static list, or (dynamic) ask the batch renderer for each next frame over stdin."""
        if not dynamic:
            for f in todo:
                if chunk_full():
                    return
                yield f
            return
        while not chunk_full():
            C.out("NEED_FRAME", {})
            line = sys.stdin.readline()
            if not line:
                return
            parts = line.split()
            if not parts or parts[0] != "FRAME":
                return                      # "DONE" or anything else: stop
            yield int(parts[1])

    for f in next_frames():
        C.out("FRAME_START", {"frame": f})
        last["stats"] = ""
        last["peak"] = None
        t0 = time.time()
        try:
            scene.frame_set(f)
            bpy.ops.render.render(write_still=False, **rkw)
            t1 = time.time()
            img = bpy.data.images.get("Render Result")
            if img is None:
                raise RuntimeError("Render produced no result")
            final = os.path.join(frames_dir, frame_name(prefix, f, ext))
            tmp = os.path.join(frames_dir, ".tmp_" + frame_name(prefix, f, ext))
            img.save_render(tmp, scene=scene)
            if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                raise RuntimeError("Saving the frame failed: %s" % tmp)
            os.replace(tmp, final)
        except Exception:
            C.out("FRAME_ERROR", {"frame": f, "error": traceback.format_exc()})
            sys.exit(3)
        t2 = time.time()
        extra = []
        for d in extra_dirs:      # files written by compositor File Output nodes for this frame
            for root, _dirs, files in os.walk(d):
                for fn in files:
                    p = os.path.join(root, fn)
                    try:
                        if os.path.getmtime(p) >= t0 - 1:
                            extra.append(os.path.relpath(p, frames_dir))
                    except OSError:
                        pass
        state["rendered"] += 1
        C.out("FRAME_DONE", {
            "frame": f,
            "render_s": round(t1 - t0, 3),
            "save_s": round(t2 - t1, 3),
            "file": final,
            "size": os.path.getsize(final),
            "peak_mb": last["peak"],
            "samples": info.get("samples"),
            "stats": last["stats"],
            "extra": extra,
        })

    if state["chunk"]:
        C.out("CHUNK_DONE", {"rendered": state["rendered"]})
        return
    C.out("ALL_DONE", {"rendered": state["rendered"]})


try:
    main()
except SystemExit:
    raise
except Exception:
    C.out("FRAME_ERROR", {"frame": None, "error": traceback.format_exc()})
    sys.exit(2)
