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
    scene = bpy.context.scene
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

    if scene.camera is None and not any(m.camera for m in scene.timeline_markers):
        C.out("FATAL", {"error": "Scene '%s' has no active camera" % scene.name})
        sys.exit(4)

    last = {"stats": ""}

    def on_stats(*a):
        if a:
            last["stats"] = str(a[0])

    bpy.app.handlers.render_stats.append(on_stats)

    ext = scene.render.file_extension
    skip = set(int(f) for f in args.get("skip", []))
    frames = list(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))
    todo = [f for f in frames if f not in skip]
    C.out("PLAN", {"total": len(frames), "todo": len(todo)})

    for f in todo:
        C.out("FRAME_START", {"frame": f})
        last["stats"] = ""
        t0 = time.time()
        try:
            scene.frame_set(f)
            bpy.ops.render.render(write_still=False)
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
        C.out("FRAME_DONE", {
            "frame": f,
            "render_s": round(t1 - t0, 3),
            "save_s": round(t2 - t1, 3),
            "file": final,
            "size": os.path.getsize(final),
            "peak_mb": C.peak_mb(last["stats"]),
            "samples": info.get("samples"),
            "stats": last["stats"],
        })

    C.out("ALL_DONE", {"rendered": len(todo)})


try:
    main()
except SystemExit:
    raise
except Exception:
    C.out("FRAME_ERROR", {"frame": None, "error": traceback.format_exc()})
    sys.exit(2)
