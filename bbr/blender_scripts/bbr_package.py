# Runs inside Blender:  blender -b file.blend --python bbr_package.py -- args.json
#
# Prepares a job for other computers. Nothing is copied or packed here; the script only
# writes copies of the .blend files whose paths point into a fixed layout, and lists every
# file the scene needs, so the app can send each file on its own:
#
#   main.blend                      the job's file
#   libs/<id>.blend                 every linked library (also libraries linked by libraries)
#   a/<id>/<name>                   "assets": whole files every frame may need (textures, HDRIs,
#                                   UDIM tiles, movies, fonts, sounds, Alembic/USD, single VDBs)
#   s/<id>/<name>                   "sequences": one file per frame (image sequences, VDB sequences,
#                                   disk caches). Sent frame by frame in Smart mode.
#   blendcache_main/ ...            point caches / bakes in Blender's default location
#
# The original files are never modified.
import hashlib
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy  # noqa: E402
import bbr_common as C  # noqa: E402

ARGS = C.script_args()
OUT = ARGS["out_dir"]
warnings = []
missing = []          # files the scene refers to that do not exist here either
assets = {}           # layout path -> source path
sequences = {}        # id -> {"dir", "src_dir", "files": [...], "kind", "window", "users": [...], "whole"}
unbaked = []
DIGITS = re.compile(r"(\d+)(?!.*\d)")


def key_for(path):
    p = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:10]


def abspath(path, lib=None):
    try:
        return os.path.normpath(bpy.path.abspath(path, library=lib))
    except TypeError:
        return os.path.normpath(bpy.path.abspath(path))


def lib_abspath(lib):
    return abspath(lib.filepath, lib.parent) if getattr(lib, "parent", None) else abspath(lib.filepath)


def frame_of(name):
    stem = name.rsplit(".", 1)[0] if "." in name else name
    m = DIGITS.search(stem)
    return int(m.group(1)) if m else None


def add_asset(src):
    """A whole file. Returns its layout path (relative to the package root)."""
    rel = "a/%s/%s" % (key_for(src), os.path.basename(src))
    assets[rel] = src
    return rel


def add_udim(src_pattern):
    d, base = os.path.split(src_pattern)
    if "<UDIM>" not in base:            # e.g. wood.1001.png: the tile number becomes the token
        base = re.sub(r"(?<=\D)1\d{3}(?=\.[^.]+$)", "<UDIM>", base)
        src_pattern = os.path.join(d, base)
    k = key_for(src_pattern)
    rx = re.compile("^" + re.escape(base).replace(re.escape("<UDIM>"), r"\d{4}") + "$")
    found = 0
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if rx.match(fn):
            assets["a/%s/%s" % (k, fn)] = os.path.join(d, fn)
            found += 1
    if not found:
        missing.append(src_pattern)
    return "a/%s/%s" % (k, base)


def add_sequence(src_file_or_dir, kind, window, users=None, whole=False, pattern=None, layout_dir=None):
    """A folder of per-frame files. Returns the layout folder."""
    src_dir = src_file_or_dir if os.path.isdir(src_file_or_dir) else os.path.dirname(src_file_or_dir)
    k = key_for(src_dir + "|" + (pattern or ""))
    seq = sequences.get(k)
    if seq is None:
        files = []
        for root, _dirs, fns in os.walk(src_dir):
            for fn in sorted(fns):
                if fn.startswith("."):
                    continue
                if pattern and not re.match(pattern, fn):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
                files.append({"name": rel, "frame": frame_of(fn)})
        seq = {"id": k, "dir": layout_dir or "s/%s" % k, "src_dir": src_dir, "files": files, "kind": kind,
               "window": window, "users": [], "whole": whole}
        sequences[k] = seq
    if users is None:
        seq["whole"] = True          # used somewhere we cannot map frames for: send it completely
    else:
        seq["users"] += users
    seq["whole"] = seq["whole"] or whole
    return seq["dir"]


# ---- who uses an image sequence, and with which frame mapping -------------------------
def image_users(img):
    """[{'start','offset','duration','cyclic'}] or None if some user cannot be mapped."""
    users = []
    unknown = False

    def from_iu(iu):
        users.append({"start": int(iu.frame_start), "offset": int(iu.frame_offset),
                      "duration": int(iu.frame_duration), "cyclic": bool(iu.use_cyclic)})

    trees = []
    for coll in (bpy.data.materials, bpy.data.worlds, bpy.data.lights, bpy.data.node_groups, bpy.data.textures):
        for idb in coll:
            t = getattr(idb, "node_tree", None) if coll is not bpy.data.node_groups else idb
            if t is not None:
                trees.append(t)
    for sc in bpy.data.scenes:
        t = C.compositor_tree(sc)
        if t is not None:
            trees.append(t)
    for t in trees:
        for n in t.nodes:
            if getattr(n, "image", None) != img:
                continue
            if getattr(n, "image_user", None) is not None:
                from_iu(n.image_user)
            elif hasattr(n, "frame_duration") and hasattr(n, "frame_offset"):     # compositor Image node
                users.append({"start": int(n.frame_start), "offset": int(n.frame_offset),
                              "duration": int(n.frame_duration), "cyclic": bool(n.use_cyclic)})
            else:
                unknown = True               # e.g. geometry-nodes image texture (frame from a socket)
    for tex in bpy.data.textures:
        if getattr(tex, "image", None) == img and getattr(tex, "image_user", None) is not None:
            from_iu(tex.image_user)
    for ob in bpy.data.objects:
        if getattr(ob, "data", None) == img and getattr(ob, "image_user", None) is not None:
            from_iu(ob.image_user)
    for cam in bpy.data.cameras:
        for bg in getattr(cam, "background_images", []):
            if getattr(bg, "image", None) == img:
                from_iu(bg.image_user)
    if unknown or not users:
        return None
    return users


# ---- the work ------------------------------------------------------------------------------
def make_self_contained(prefix, where, blend_layout):
    """Point every external file of the open .blend into the layout; returns linked libraries."""
    up = prefix          # "//" for main.blend, "//../" for libs/<id>.blend

    # images
    for img in bpy.data.images:
        if img.library or img.packed_file or not img.filepath:
            continue
        if img.source not in ("FILE", "TILED", "SEQUENCE", "MOVIE"):
            continue
        src = abspath(img.filepath)
        try:
            if img.source == "TILED" or "<UDIM>" in img.filepath:
                img.filepath = up + add_udim(src)
            elif not os.path.exists(src):
                missing.append(src)
                warnings.append("%s: missing image '%s': %s" % (where, img.name, src))
            elif img.source == "SEQUENCE":
                d = add_sequence(src, "image", 0, image_users(img))
                img.filepath = up + d + "/" + os.path.basename(src)
            else:
                img.filepath = up + add_asset(src)
        except Exception as e:
            warnings.append("%s: could not prepare image '%s' (%s)" % (where, img.name, e))

    # sounds, fonts, movie clips, Alembic/USD, volumes
    for coll in (bpy.data.sounds, bpy.data.fonts, bpy.data.movieclips, bpy.data.cache_files, bpy.data.volumes):
        for d in coll:
            fp = getattr(d, "filepath", "")
            if d.library or getattr(d, "packed_file", None) or not fp or fp.startswith("<"):
                continue
            src = abspath(fp)
            try:
                if coll is bpy.data.volumes and getattr(d, "is_sequence", False):
                    if not os.path.isdir(os.path.dirname(src)):
                        missing.append(src)
                        continue
                    users = [{"start": int(d.frame_start), "offset": int(d.frame_offset),
                              "duration": int(d.frame_duration), "cyclic": d.sequence_mode == "REPEAT",
                              "mode": d.sequence_mode}]
                    dd = add_sequence(src, "cache", 1, users)
                    d.filepath = up + dd + "/" + os.path.basename(src)
                elif (coll is bpy.data.cache_files and getattr(d, "is_sequence", False)) or \
                        (coll is bpy.data.movieclips and getattr(d, "source", "") == "SEQUENCE"):
                    dd = add_sequence(src, "cache", 0, None)
                    d.filepath = up + dd + "/" + os.path.basename(src)
                elif not os.path.exists(src):
                    missing.append(src)
                    warnings.append("%s: missing file '%s': %s" % (where, d.name, src))
                else:
                    d.filepath = up + add_asset(src)
            except Exception as e:
                warnings.append("%s: could not prepare '%s' (%s)" % (where, d.name, e))

    # video editor strips (image strips use their own file lists)
    for sc in bpy.data.scenes:
        se = sc.sequence_editor
        if not se:
            continue
        strips = getattr(se, "strips_all", None) or getattr(se, "sequences_all", [])
        for st in strips:
            try:
                if st.type == "IMAGE":
                    d = abspath(st.directory)
                    if os.path.isdir(d):
                        names = [e.filename for e in st.elements]
                        k = key_for(d)
                        for n in names:
                            if os.path.exists(os.path.join(d, n)):
                                assets["a/%s/%s" % (k, n)] = os.path.join(d, n)
                        st.directory = up + "a/%s/" % k
                elif st.type in ("MOVIE", "SOUND"):
                    fp = st.filepath if st.type == "MOVIE" else getattr(getattr(st, "sound", None), "filepath", "")
                    if st.type == "MOVIE" and fp:
                        src = abspath(fp)
                        if os.path.exists(src):
                            st.filepath = up + add_asset(src)
            except Exception as e:
                warnings.append("%s: could not prepare strip '%s' (%s)" % (where, st.name, e))

    # disk caches and bakes
    for ob in bpy.data.objects:
        if ob.library:
            continue
        for md in ob.modifiers:
            caches = []
            if md.type in ("CLOTH", "SOFT_BODY"):
                caches.append(md.point_cache)
            elif md.type == "PARTICLE_SYSTEM":
                caches.append(md.particle_system.point_cache)
            elif md.type == "DYNAMIC_PAINT" and getattr(md, "canvas_settings", None):
                caches += [s.point_cache for s in md.canvas_settings.canvas_surfaces]
            for pc in caches:
                if pc is None:
                    continue
                if getattr(pc, "use_external", False) and pc.filepath:
                    src = abspath(pc.filepath)
                    if os.path.isdir(src):
                        pc.filepath = up + add_sequence(src, "cache", 1, [], pattern=r".*\.bphys$")
                    else:
                        missing.append(src)
            if md.type == "FLUID" and getattr(md, "fluid_type", "") == "DOMAIN":
                ds = md.domain_settings
                src = abspath(ds.cache_directory)
                if os.path.isdir(src):
                    off = int(getattr(ds, "cache_frame_offset", 0) or 0)
                    users = [] if off == 0 else None
                    ds.cache_directory = up + add_sequence(src, "cache", 1, users)
                else:
                    warnings.append("%s: fluid cache folder not found: %s" % (where, src))
            if md.type == "NODES" and getattr(md, "bake_directory", ""):
                src = abspath(md.bake_directory)
                if os.path.isdir(src):
                    md.bake_directory = up + add_sequence(src, "cache", 0, None)
    # Blender's default cache folder next to the file: blendcache_<file name>
    src_blend = bpy.data.filepath
    dflt = os.path.join(os.path.dirname(src_blend), "blendcache_" + os.path.splitext(os.path.basename(src_blend))[0])
    if os.path.isdir(dflt):
        target = os.path.dirname(blend_layout)
        target = (target + "/" if target else "") + "blendcache_" + os.path.splitext(os.path.basename(blend_layout))[0]
        add_sequence(dflt, "cache", 1, [], layout_dir=target)

    # libraries -> libs/
    found = []
    for lib in bpy.data.libraries:
        src = lib_abspath(lib)
        k = key_for(src)
        found.append((src, k))
        lib.filepath = (up + "libs/" if prefix == "//" else "//") + k + ".blend"
    return found


def find_unbaked(scene):
    def check(owner, what, pc):
        if pc is not None and not getattr(pc, "is_baked", True):
            unbaked.append("%s: %s" % (owner, what))
    for ob in scene.objects:
        for md in ob.modifiers:
            if not getattr(md, "show_render", True):
                continue
            t = md.type
            if t in ("CLOTH", "SOFT_BODY"):
                check(ob.name, t.lower().replace("_", " "), getattr(md, "point_cache", None))
            elif t == "PARTICLE_SYSTEM":
                ps = md.particle_system
                if ps.settings.type == "HAIR" and not getattr(ps, "use_hair_dynamics", False):
                    continue
                if getattr(ps.settings, "physics_type", "") == "NO":
                    continue
                check(ob.name, "particles '%s'" % ps.name, getattr(ps, "point_cache", None))
            elif t == "DYNAMIC_PAINT" and getattr(md, "canvas_settings", None):
                for s in md.canvas_settings.canvas_surfaces:
                    check(ob.name, "dynamic paint '%s'" % s.name, getattr(s, "point_cache", None))
            elif t == "FLUID" and getattr(md, "fluid_type", "") == "DOMAIN":
                if getattr(md.domain_settings, "cache_type", "") == "REPLAY":
                    unbaked.append("%s: fluid (Replay cache)" % ob.name)
    rbw = scene.rigidbody_world
    if rbw and getattr(rbw, "enabled", False):
        check("Scene", "rigid body", getattr(rbw, "point_cache", None))


def save_copy(path):
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, relative_remap=False, compress=True)


def main():
    os.makedirs(os.path.join(OUT, "libs"), exist_ok=True)
    scene = bpy.context.scene
    addons = C.enabled_addons()
    used = C.used_addons(addons)
    find_unbaked(scene)

    libs = make_self_contained("//", "main file", "main.blend")
    save_copy(os.path.join(OUT, "main.blend"))

    C.apply_overrides(scene, ARGS.get("overrides"))
    note = C.ensure_image_output(scene)
    info = C.scene_info(scene)
    info["format_note"] = note

    lib_list = []
    seen = set()
    todo = list(libs)
    while todo:
        src, k = todo.pop(0)
        if k in seen:
            continue
        seen.add(k)
        if not os.path.exists(src):
            missing.append(src)
            warnings.append("missing linked library: %s" % src)
            continue
        try:
            st = os.stat(src)
            lib_list.append({"path": src, "key": k, "stamp": [round(st.st_mtime, 3), st.st_size]})
        except OSError:
            pass
        try:
            bpy.ops.wm.open_mainfile(filepath=src, load_ui=False)
        except Exception as e:
            warnings.append("could not open linked library %s (%s)" % (src, e))
            continue
        todo += make_self_contained("//../", os.path.basename(src), "libs/%s.blend" % k)
        save_copy(os.path.join(OUT, "libs", k + ".blend"))

    used_mods = sorted(m for m in used if not addons.get(m, {}).get("bundled"))
    C.out("PACKAGE", {
        "scene_info": info,
        "addons": [dict(addons[m], used=m in used, reason=used.get(m, "")) for m in sorted(addons)],
        "addons_required": used_mods,
        "libraries": lib_list,
        "assets": assets,
        "sequences": list(sequences.values()),
        "missing": sorted(set(missing)),
        "unbaked": unbaked,
        "warnings": warnings,
        "blender_version": bpy.app.version_string,
    })


try:
    main()
except Exception:
    C.out("PACKAGE_ERROR", {"error": traceback.format_exc()})
    sys.exit(2)
