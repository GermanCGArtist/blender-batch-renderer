# Shared helpers for the scripts that run *inside* Blender.
# Keep this compatible with old Blender Python (3.7+): no walrus, no match.
import json
import os
import re
import sys

import bpy

VIDEO_FORMATS = {"FFMPEG", "AVI_JPEG", "AVI_RAW", "H264", "XVID", "THEORA", "QUICKTIME", "FRAMESERVER"}
_PEAK_RE = re.compile(r"Peak[:\s]+([\d.]+)\s*([KMG])", re.I)


def out(kind, data=None):
    sys.stdout.write("BBR_" + kind + " " + json.dumps(data or {}, default=str) + "\n")
    sys.stdout.flush()


def warn(text):
    out("WARN", {"text": text})


def script_args():
    argv = sys.argv
    if "--" not in argv:
        return {}
    rest = argv[argv.index("--") + 1:]
    if not rest:
        return {}
    with open(rest[0], "r", encoding="utf-8") as f:
        return json.load(f)


def peak_mb(stats):
    best = None
    for v, u in _PEAK_RE.findall(stats or ""):
        x = float(v)
        u = u.upper()
        x = x / 1024 if u == "K" else x * 1024 if u == "G" else x
        best = x if best is None else max(best, x)
    return best


def is_eevee(engine):
    return engine.startswith("BLENDER_EEVEE")


def apply_overrides(scene, ov):
    """Only the parameters exposed in the batch renderer; everything else stays as saved."""
    ov = ov or {}
    applied = []
    if ov.get("frame_start") is not None:
        scene.frame_start = int(ov["frame_start"])
        applied.append("frame_start=%s" % ov["frame_start"])
    if ov.get("frame_end") is not None:
        scene.frame_end = int(ov["frame_end"])
        applied.append("frame_end=%s" % ov["frame_end"])
    if ov.get("resolution_pct") is not None:
        scene.render.resolution_percentage = int(ov["resolution_pct"])
        applied.append("resolution_pct=%s" % ov["resolution_pct"])
    if ov.get("samples") is not None:
        n = int(ov["samples"])
        eng = scene.render.engine
        if eng == "CYCLES":
            scene.cycles.samples = n
            applied.append("cycles samples=%d" % n)
        elif is_eevee(eng):
            scene.eevee.taa_render_samples = n
            applied.append("eevee samples=%d" % n)
        else:
            warn("Samples override ignored for engine %s" % eng)
    return applied


def output_is_video(scene):
    ims = scene.render.image_settings
    if getattr(ims, "media_type", None) == "VIDEO":
        return True
    return ims.file_format in VIDEO_FORMATS


def ensure_image_output(scene):
    """If the file renders to a video, switch to a PNG sequence (needed for resuming)."""
    if not output_is_video(scene):
        return None
    ims = scene.render.image_settings
    old_depth = getattr(ims, "color_depth", "8")
    if hasattr(ims, "media_type"):
        ims.media_type = "IMAGE"
    ims.file_format = "PNG"
    try:
        ims.color_depth = "16" if old_depth in ("10", "12", "16", "32") else "8"
    except Exception:
        pass
    try:
        ims.color_mode = "RGBA" if scene.render.film_transparent else "RGB"
    except Exception:
        pass
    return "Output was set to video; switched to a PNG image sequence (the preview MP4 is made afterwards)"


def compositor_tree(scene):
    tree = getattr(scene, "compositing_node_group", None)       # Blender 5.0+
    if tree is None and getattr(scene, "use_nodes", False):
        tree = getattr(scene, "node_tree", None)                 # up to 4.x
    return tree


def file_output_nodes(scene):
    tree = compositor_tree(scene)
    if tree is None:
        return []
    return [n for n in tree.nodes if n.bl_idname == "CompositorNodeOutputFile" and not n.mute]


def redirect_file_outputs(scene, frames_dir):
    """Point compositor File Output nodes into frames/<node name>/."""
    done = []
    for node in file_output_nodes(scene):
        safe = re.sub(r"[^\w.\-]+", "_", node.name) or "file_output"
        d = os.path.join(frames_dir, safe) + os.sep
        if hasattr(node, "directory"):
            node.directory = d
        elif hasattr(node, "base_path"):
            node.base_path = d
        else:
            continue
        done.append(node.name)
    return done


def cycles_device(scene):
    if scene.render.engine != "CYCLES":
        return None
    dev = getattr(scene.cycles, "device", "CPU")
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        if hasattr(prefs, "refresh_devices"):
            prefs.refresh_devices()
        else:
            prefs.get_devices()
        kind = getattr(prefs, "compute_device_type", "NONE")
        if dev == "GPU" and kind not in ("NONE", ""):
            names = [d.name for d in prefs.devices if d.use and d.type != "CPU"]
            return "GPU (%s: %s)" % (kind, ", ".join(names) or "no device enabled!")
        return "CPU" if dev == "CPU" else "GPU (%s)" % kind
    except Exception:
        return dev


def scene_info(scene):
    r = scene.render
    ims = r.image_settings
    eng = r.engine
    samples = None
    if eng == "CYCLES":
        samples = scene.cycles.samples
    elif is_eevee(eng):
        samples = scene.eevee.taa_render_samples
    vs = scene.view_settings
    return {
        "blender_version": bpy.app.version_string,
        "scene": scene.name,
        "engine": eng,
        "device": cycles_device(scene) or ("GPU" if eng != "CYCLES" else None),
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_step": scene.frame_step,
        "fps": r.fps,
        "fps_base": r.fps_base,
        "res_x": r.resolution_x,
        "res_y": r.resolution_y,
        "res_pct": r.resolution_percentage,
        "samples": samples,
        "file_format": ims.file_format,
        "color_depth": getattr(ims, "color_depth", None),
        "file_extension": r.file_extension,
        "film_transparent": getattr(r, "film_transparent", None),
        "camera": scene.camera.name if scene.camera else None,
        "view_layers": [vl.name for vl in scene.view_layers if vl.use],
        "view_transform": vs.view_transform,
        "look": vs.look,
        "exposure": vs.exposure,
        "gamma": vs.gamma,
        "file_output_nodes": [n.name for n in file_output_nodes(scene)],
        "persistent_data": getattr(r, "use_persistent_data", None),
    }
