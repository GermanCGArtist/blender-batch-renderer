# Runs inside Blender: checks a file before the batch starts and reports problems.
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy  # noqa: E402
import bbr_common as C  # noqa: E402


def abspath(path, lib=None):
    try:
        return os.path.normpath(bpy.path.abspath(path, library=lib))
    except TypeError:
        return os.path.normpath(bpy.path.abspath(path))


def main():
    scene = bpy.context.scene
    problems, notes = [], []

    if scene.camera is None and not any(m.camera for m in scene.timeline_markers):
        problems.append("Scene '%s' has no active camera - it cannot render" % scene.name)

    if C.output_is_video(scene):
        notes.append("Output is set to video; it will be rendered as a PNG sequence instead")
    if C.file_output_nodes(scene):
        notes.append("Compositor File Output nodes will be redirected into frames/<node name>/")

    # missing images
    for img in bpy.data.images:
        if img.source not in ("FILE", "SEQUENCE", "MOVIE") or img.packed_file or not img.filepath:
            continue
        p = abspath(img.filepath, img.library)
        target = os.path.dirname(p) if img.source == "SEQUENCE" else p
        if not os.path.exists(target):
            problems.append("Missing image '%s': %s" % (img.name, p))

    # missing linked libraries
    for lib in bpy.data.libraries:
        p = abspath(lib.filepath)
        if not os.path.exists(p):
            problems.append("Missing linked library: %s" % p)

    # other external files
    for coll_name in ("sounds", "movieclips", "fonts", "volumes", "cache_files"):
        for d in getattr(bpy.data, coll_name, []):
            fp = getattr(d, "filepath", "")
            if not fp or getattr(d, "packed_file", None) or fp.startswith("<"):
                continue
            p = abspath(fp, getattr(d, "library", None))
            if not os.path.exists(p):
                problems.append("Missing %s file '%s': %s" % (coll_name[:-1], d.name, p))

    # simulations that are not baked (resuming mid-sequence could change them)
    def check_cache(owner, what, pc):
        if pc is not None and not getattr(pc, "is_baked", True):
            problems.append("%s: %s cache is not baked - resumed renders may differ" % (owner, what))

    for ob in scene.objects:
        for md in ob.modifiers:
            t = md.type
            if t in ("CLOTH", "SOFT_BODY"):
                check_cache(ob.name, t.lower(), getattr(md, "point_cache", None))
            elif t == "PARTICLE_SYSTEM":
                ps = md.particle_system
                if ps.settings.type == "HAIR" and not getattr(ps, "use_hair_dynamics", False):
                    continue
                check_cache(ob.name, "particle '%s'" % ps.name, getattr(ps, "point_cache", None))
            elif t == "DYNAMIC_PAINT" and getattr(md, "canvas_settings", None):
                for s in md.canvas_settings.canvas_surfaces:
                    check_cache(ob.name, "dynamic paint '%s'" % s.name, getattr(s, "point_cache", None))
            elif t == "FLUID" and getattr(md, "fluid_type", "") == "DOMAIN":
                ds = md.domain_settings
                if getattr(ds, "cache_type", "") == "REPLAY":
                    problems.append("%s: fluid domain uses a Replay cache - bake it (All/Modular) before batch rendering" % ob.name)
    rbw = scene.rigidbody_world
    if rbw and rbw.enabled:
        check_cache("Scene", "rigid body", getattr(rbw, "point_cache", None))

    if scene.render.engine == "CYCLES":
        dev = C.cycles_device(scene) or ""
        if "no device enabled" in dev:
            problems.append("Cycles is set to GPU but no GPU is enabled in Preferences > System")

    info = C.scene_info(scene)
    C.out("PREFLIGHT", {"scene_info": info, "problems": problems, "notes": notes})


try:
    main()
except Exception:
    C.out("PREFLIGHT", {"problems": ["Preflight crashed: " + traceback.format_exc()]})
