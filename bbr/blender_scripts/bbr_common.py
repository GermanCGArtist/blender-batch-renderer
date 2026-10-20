# Shared helpers for the scripts that run *inside* Blender.
# Keep this compatible with old Blender Python (3.7+): no walrus, no match.
import json
import os
import re
import sys

import bpy

VIDEO_FORMATS = {"FFMPEG", "AVI_JPEG", "AVI_RAW", "H264", "XVID", "THEORA", "QUICKTIME", "FRAMESERVER"}
_PEAK_RE = re.compile(r"(?:Peak|Mem)[:\s]+([\d.]+)\s*([KMG])", re.I)


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


def pick_scene(ov):
    """The scene to render: the one named in the overrides, else the file's active scene."""
    name = (ov or {}).get("scene")
    if name:
        sc = bpy.data.scenes.get(name) if hasattr(bpy.data.scenes, "get") else \
            next((s for s in bpy.data.scenes if s.name == name), None)
        if sc is None:
            warn("Scene '%s' not found in the file - rendering the active scene" % name)
        else:
            return sc
    return bpy.context.scene


def render_kwargs(scene, ov):
    """Arguments for bpy.ops.render.render: the scene (when not the active one) and a single view layer."""
    kw = {}
    ov = ov or {}
    if ov.get("scene") and scene is not bpy.context.scene:
        kw["scene"] = scene.name
    if ov.get("view_layer"):
        kw["layer"] = ov["view_layer"]
    return kw


def apply_overrides(scene, ov):
    """Only the parameters exposed in the batch renderer; everything else stays as saved."""
    ov = ov or {}
    applied = []
    if ov.get("scene"):
        applied.append("scene=%s" % scene.name)
    if ov.get("frame_step") is not None:
        scene.frame_step = max(1, int(ov["frame_step"]))
        applied.append("frame_step=%s" % ov["frame_step"])
    if ov.get("camera"):
        cam = next((o for o in getattr(bpy.data, "objects", []) if o.name == ov["camera"]), None)
        if cam is None or getattr(cam, "type", "CAMERA") != "CAMERA":
            warn("Camera '%s' not found - using the scene's camera" % ov["camera"])
        else:
            scene.camera = cam
            applied.append("camera=%s" % cam.name)
    if ov.get("view_layer"):
        names = [vl.name for vl in scene.view_layers]
        if ov["view_layer"] not in names:
            warn("View layer '%s' not found in scene '%s'" % (ov["view_layer"], scene.name))
        else:
            applied.append("view_layer=%s" % ov["view_layer"])
    if ov.get("res_x") and ov.get("res_y"):
        scene.render.resolution_x = int(ov["res_x"])
        scene.render.resolution_y = int(ov["res_y"])
        applied.append("resolution=%sx%s" % (ov["res_x"], ov["res_y"]))
    if ov.get("file_format"):
        try:
            scene.render.image_settings.file_format = ov["file_format"]
            applied.append("file_format=%s" % ov["file_format"])
        except Exception as e:  # noqa: BLE001 - unknown format in this Blender version
            warn("Output format %s not available: %s" % (ov["file_format"], e))
    if ov.get("color_depth"):
        try:
            scene.render.image_settings.color_depth = str(ov["color_depth"])
            applied.append("color_depth=%s" % ov["color_depth"])
        except Exception as e:  # noqa: BLE001 - e.g. 16 bit for JPEG
            warn("Bit depth %s not available for this format: %s" % (ov["color_depth"], e))
    if scene.render.engine == "CYCLES":
        if ov.get("noise_threshold") is not None:
            t = float(ov["noise_threshold"])
            scene.cycles.use_adaptive_sampling = t > 0
            if t > 0:
                scene.cycles.adaptive_threshold = t
            applied.append("noise_threshold=%s" % t)
        if ov.get("denoise") is not None:
            scene.cycles.use_denoising = bool(ov["denoise"])
            applied.append("denoise=%s" % bool(ov["denoise"]))
    elif ov.get("noise_threshold") is not None or ov.get("denoise") is not None:
        warn("Noise threshold and denoiser only apply to Cycles (this scene uses %s)" % scene.render.engine)
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
    dev = (ov.get("device") or "").upper()
    if dev in ("CPU", "GPU"):
        if scene.render.engine != "CYCLES":
            warn("Device choice only applies to Cycles (this scene uses %s)" % scene.render.engine)
        else:
            if dev == "GPU":
                note = enable_gpu()
                if note:
                    warn(note)
            scene.cycles.device = dev
            applied.append("device=%s" % dev)
    return applied


GPU_BACKENDS = ("OPTIX", "CUDA", "METAL", "HIP", "ONEAPI")


def enable_gpu():
    """Make sure Cycles has a GPU backend with at least one device switched on (this computer's
    preferences are used as they are when they already have one). Returns a note or None."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except Exception:
        return "Cycles preferences not available - using the file's device"
    try:
        if hasattr(prefs, "refresh_devices"):
            prefs.refresh_devices()
        else:
            prefs.get_devices()
    except Exception:
        pass
    kind = getattr(prefs, "compute_device_type", "NONE")

    def gpus():
        return [d for d in prefs.devices if d.type != "CPU"]

    if kind in ("", "NONE") or not gpus():
        for b in GPU_BACKENDS:
            try:
                prefs.compute_device_type = b
                if hasattr(prefs, "refresh_devices"):
                    prefs.refresh_devices()
                else:
                    prefs.get_devices()
            except Exception:
                continue
            if gpus():
                kind = b
                break
        else:
            return "No GPU found for Cycles on this computer - rendering on the CPU"
    if not any(d.use for d in gpus()):
        for d in gpus():
            d.use = True
        return "No GPU was switched on in Blender's preferences - using all %s devices" % kind
    return None


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


def safe_dir_name(name):
    return re.sub(r"[^\w.\-]+", "_", name) or "file_output"


def redirect_file_outputs(scene, frames_dir):
    """Point compositor File Output nodes into frames/<node name>/."""
    done = []
    for node in file_output_nodes(scene):
        safe = safe_dir_name(node.name)
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


# ---- add-ons ------------------------------------------------------------------
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def enabled_addons():
    """{module: {"module", "name", "version", "bundled"}} for every enabled add-on / extension."""
    import addon_utils
    enabled = set(bpy.context.preferences.addons.keys())
    try:
        system_scripts = os.path.normpath(bpy.utils.system_resource("SCRIPTS"))
    except Exception:
        system_scripts = ""
    out = {}
    for mod in addon_utils.modules(refresh=False):
        name = mod.__name__
        if name not in enabled:
            continue
        try:
            info = addon_utils.module_bl_info(mod)
        except Exception:
            info = getattr(mod, "bl_info", {}) or {}
        path = os.path.normpath(getattr(mod, "__file__", "") or "")
        ver = info.get("version") or ()
        out[name] = {
            "module": name,
            "name": info.get("name") or name.split(".")[-1],
            "version": ".".join(str(v) for v in ver) if isinstance(ver, (tuple, list)) else str(ver),
            "bundled": bool(system_scripts and path.startswith(system_scripts)),
        }
    for name in enabled:          # enabled but not found by addon_utils (rare)
        out.setdefault(name, {"module": name, "name": name.split(".")[-1], "version": "", "bundled": False})
    return out


HANDLER_LISTS = ("render_init", "render_pre", "render_post", "render_complete", "frame_change_pre",
                 "frame_change_post", "depsgraph_update_pre", "depsgraph_update_post", "load_post")


def used_addons(addons):
    """Which enabled add-ons does this file appear to rely on when rendering?
    Signs: the add-on hooks into rendering / frame changes, or the file contains its node groups,
    custom nodes or custom properties. Returns {module: reason}."""
    used = {}
    mods = sorted(addons.keys(), key=len, reverse=True)

    def owner(module_name):
        for m in mods:
            if module_name == m or module_name.startswith(m + "."):
                return m
        return None

    for hname in HANDLER_LISTS:
        for fn in getattr(bpy.app.handlers, hname, []):
            m = owner(getattr(fn, "__module__", "") or "")
            if m and not m.startswith("bbr"):
                used.setdefault(m, "hooks into rendering (%s)" % hname)

    tokens = {}
    for m, a in addons.items():
        for t in {_norm(m.split(".")[-1]), _norm(a.get("name"))}:
            if len(t) >= 4:
                tokens[t] = m
    names = []
    for ng in bpy.data.node_groups:
        names.append(("node group", ng.name))
        for n in ng.nodes:
            names.append(("node", n.bl_idname))
    for coll in (bpy.data.materials, bpy.data.worlds, bpy.data.scenes):
        for idb in coll:
            tree = getattr(idb, "node_tree", None) or getattr(idb, "compositing_node_group", None)
            if tree:
                for n in tree.nodes:
                    names.append(("node", n.bl_idname))
    for coll in (bpy.data.scenes, bpy.data.objects, bpy.data.cameras, bpy.data.materials, bpy.data.worlds,
                 bpy.data.lights, bpy.data.meshes):
        for idb in coll:
            try:
                for k in idb.keys():
                    names.append(("property", k))
            except Exception:
                pass
    for kind, n in names:
        nn = _norm(n)
        for t, m in tokens.items():
            if t in nn:
                used.setdefault(m, "the file contains its %s '%s'" % (kind, n))
    return used


# ---- input check (worker) ------------------------------------------------------
def _candidates(rel, first_base):
    """A relative path ("//…") can count from the file that links it or from the open file – Blender
    is not consistent about which one it reports for libraries linked through other libraries."""
    main_base = os.path.dirname(bpy.data.filepath)
    out = []
    for base in (first_base, main_base):
        if base is not None:
            p = os.path.normpath(os.path.join(base, rel))
            if p not in out:
                out.append(p)
    return out


def _pick(cands):
    for p in cands:
        if os.path.exists(p) or os.path.isdir(os.path.dirname(p)) and "<UDIM>" in p:
            return p
    return cands[0]


def lib_path(lib):
    """Absolute path of a linked library (the first candidate that exists)."""
    p = lib.filepath
    if p.startswith("//"):
        parent = getattr(lib, "parent", None)
        base = os.path.dirname(lib_path(parent)) if parent is not None else None
        return _pick(_candidates(p[2:], base))
    return os.path.normpath(p)


def file_path(path, library=None):
    """Absolute path of a file used by a data-block (image, cache …), which may come from a library."""
    if path.startswith("//"):
        base = os.path.dirname(lib_path(library)) if library is not None else None
        return _pick(_candidates(path[2:], base))
    return os.path.normpath(path)


def missing_inputs(ignore=()):
    """External files the open scene refers to that are not there (sequence folders count, single
    frames of sequences are checked per frame by the app)."""
    ignore = set(ignore)
    out = []

    def check(path, lib, kind, resolved=None):
        if not path or path.startswith("<"):
            return
        p = resolved or file_path(path, lib)
        if os.path.basename(p) in ignore:
            return
        if kind == "dir":
            if not os.path.isdir(os.path.dirname(p)):
                out.append(p)
        elif kind == "udim":
            d, base = os.path.split(p)
            pat = re.compile("^" + re.escape(base).replace(re.escape("<UDIM>"), r"\d{4}") + "$")
            if not (os.path.isdir(d) and any(pat.match(f) for f in os.listdir(d))):
                out.append(p)
        elif not os.path.exists(p):
            out.append(p)

    for lib in bpy.data.libraries:
        check(lib.filepath, None, "file", resolved=lib_path(lib))
    for img in bpy.data.images:
        if img.packed_file or not img.filepath or img.source not in ("FILE", "TILED", "SEQUENCE", "MOVIE"):
            continue
        kind = "udim" if (img.source == "TILED" or "<UDIM>" in img.filepath) else \
            ("dir" if img.source == "SEQUENCE" else "file")
        check(img.filepath, img.library, kind)
    for coll in (bpy.data.sounds, bpy.data.fonts, bpy.data.movieclips, bpy.data.cache_files, bpy.data.volumes):
        for d in coll:
            if getattr(d, "packed_file", None):
                continue
            seq = getattr(d, "is_sequence", False) or getattr(d, "source", "") == "SEQUENCE"
            check(getattr(d, "filepath", ""), d.library, "dir" if seq else "file")
    return sorted(set(out))
