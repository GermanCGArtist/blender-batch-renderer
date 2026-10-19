# Runs inside Blender:  blender -b file.blend --python bbr_package.py -- args.json
#
# Builds a self-contained copy of a job for other machines:
#   out/main.blend          the file, textures packed, paths relative
#   out/libs/<id>_<name>    every linked library (also libraries linked by libraries),
#                           each with its own textures packed and links pointing to libs/
#   out/files/...           external files that cannot be packed (image sequences, movies,
#                           Alembic/USD caches, VDB volumes, movie clips)
# The original files are never modified (everything is saved as copies).
import hashlib
import os
import shutil
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy  # noqa: E402
import bbr_common as C  # noqa: E402

ARGS = C.script_args()
OUT = ARGS["out_dir"]
LIBS = os.path.join(OUT, "libs")
FILES = os.path.join(OUT, "files")
warnings = []
copied = {}       # abs source -> name inside files/


def key_for(path):
    p = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:8] + "_" + os.path.basename(path)


def abspath(path, lib=None):
    try:
        return os.path.normpath(bpy.path.abspath(path, library=lib))
    except TypeError:
        return os.path.normpath(bpy.path.abspath(path))


def lib_abspath(lib):
    # an indirect library's path is relative to the library file that links it
    return abspath(lib.filepath, lib.parent) if getattr(lib, "parent", None) else abspath(lib.filepath)


def copy_into_files(src, sequence=False):
    """Copy an external file (or its whole folder for sequences); returns the name inside files/."""
    if src in copied:
        return copied[src]
    if sequence:
        d = os.path.dirname(src)
        name = key_for(d)
        dst = os.path.join(FILES, name)
        if not os.path.isdir(dst):
            shutil.copytree(d, dst)
        rel = name + "/" + os.path.basename(src)
    else:
        rel = key_for(src)
        shutil.copy2(src, os.path.join(FILES, rel))
    copied[src] = rel
    return rel


def make_self_contained(files_prefix, lib_prefix, where):
    """Pack or copy every external file of the open .blend and point library links to libs/."""
    found_libs = []
    # images
    for img in bpy.data.images:
        if img.library or img.packed_file or not img.filepath:
            continue
        if img.source not in ("FILE", "TILED", "SEQUENCE", "MOVIE"):
            continue
        src = abspath(img.filepath)
        if img.source == "TILED":
            if not os.path.exists(os.path.dirname(src)):
                warnings.append("%s: missing UDIM textures '%s': %s" % (where, img.name, src))
                continue
        elif not os.path.exists(src):
            warnings.append("%s: missing image '%s': %s" % (where, img.name, src))
            continue
        try:
            if img.source in ("FILE", "TILED"):
                img.pack()
            else:
                img.filepath = files_prefix + copy_into_files(src, sequence=img.source == "SEQUENCE")
        except Exception as e:
            warnings.append("%s: could not include image '%s' (%s)" % (where, img.name, e))
    # sounds and fonts can be packed
    for coll in (bpy.data.sounds, bpy.data.fonts):
        for d in coll:
            if d.library or d.packed_file or not d.filepath or d.filepath.startswith("<"):
                continue
            if not os.path.exists(abspath(d.filepath)):
                warnings.append("%s: missing file '%s': %s" % (where, d.name, abspath(d.filepath)))
                continue
            try:
                d.pack()
            except Exception as e:
                warnings.append("%s: could not pack '%s' (%s)" % (where, d.name, e))
    # files that cannot be packed: copy them
    for coll, seq_attr in ((bpy.data.cache_files, None), (bpy.data.volumes, "is_sequence"),
                           (bpy.data.movieclips, None)):
        for d in coll:
            if d.library or not getattr(d, "filepath", ""):
                continue
            src = abspath(d.filepath)
            if not os.path.exists(src):
                warnings.append("%s: missing file '%s': %s" % (where, d.name, src))
                continue
            seq = bool(seq_attr and getattr(d, seq_attr, False)) or getattr(d, "source", "") == "SEQUENCE"
            try:
                d.filepath = files_prefix + copy_into_files(src, sequence=seq)
            except Exception as e:
                warnings.append("%s: could not copy '%s' (%s)" % (where, d.name, e))
    # simulation caches on disk are not included yet
    for ob in bpy.data.objects:
        if ob.library:
            continue
        for md in ob.modifiers:
            pc = getattr(md, "point_cache", None)
            if pc is not None and getattr(pc, "use_disk_cache", False):
                warnings.append("%s: '%s' uses a disk cache (%s) – not included, bake it into the file or "
                                "render this job on this computer" % (where, ob.name, md.name))
            if md.type == "FLUID" and getattr(md, "fluid_type", "") == "DOMAIN":
                warnings.append("%s: '%s' is a fluid domain – its cache folder is not included yet" % (where, ob.name))
    # library links -> libs/
    for lib in bpy.data.libraries:
        src = lib_abspath(lib)
        k = key_for(src)
        found_libs.append((src, k))
        lib.filepath = lib_prefix + k
    return found_libs


def save_copy(path):
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, relative_remap=False, compress=True)


def main():
    for d in (LIBS, FILES):
        os.makedirs(d, exist_ok=True)
    scene = bpy.context.scene

    addons = C.enabled_addons()
    used = C.used_addons(addons)

    libs = make_self_contained("//files/", "//libs/", "main file")
    save_copy(os.path.join(OUT, "main.blend"))

    # facts about the scene as it will render (overrides applied in memory only, after saving)
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
        # inside libs/: other libraries are next to it, files are one level up
        todo += make_self_contained("//../files/", "//", os.path.basename(src))
        save_copy(os.path.join(LIBS, k))

    used_mods = sorted(m for m in used if not addons.get(m, {}).get("bundled"))
    C.out("PACKAGE", {
        "scene_info": info,
        "addons": [dict(addons[m], used=m in used, reason=used.get(m, "")) for m in sorted(addons)],
        "addons_required": used_mods,
        "libraries": lib_list,
        "external_files": len(copied),
        "warnings": warnings,
        "blender_version": bpy.app.version_string,
    })


try:
    main()
except Exception:
    C.out("PACKAGE_ERROR", {"error": traceback.format_exc()})
    sys.exit(2)
