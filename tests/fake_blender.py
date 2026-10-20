#!/usr/bin/env python3
"""A stand-in for the Blender executable, used to test the batch engine and the
real Blender-side scripts without Blender. It builds a mock `bpy` module and
runs the script passed with --python.

Behaviour switches (environment variables):
  FAKE_FRAMES=1-10        frame range of the "scene"
  FAKE_FRAME_TIME=0.2     seconds per frame
  FAKE_CRASH_ONCE=5,8     segfault-like exit the first time these frames render
  FAKE_ALWAYS_CRASH=7     exit every time this frame renders
  FAKE_LOAD_FAIL=1        exit before rendering anything
  FAKE_NO_CAMERA=1        scene without camera
  FAKE_VIDEO=1            scene output set to FFMPEG
  FAKE_NEW_LOG=1          behave like Blender 5.x (no Fra: status lines)
  FAKE_DISK_FULL=4        saving this frame fails with "No space left on device"
  FAKE_ADDONS=lens_sim:LensSim:1.2.0;other:Other:1.0   enabled add-ons (module:name:version)
  FAKE_NODE_GROUPS=LensSim Lens   node groups in the file (add-on detection)
  FAKE_LIBTREE={"main": ["//libs/a.blend"], "/abs/a.blend": ["//b.blend"]}   linked libraries
  FAKE_IMAGES=/abs/tex.png;//rel.png   external images of the main file
  FAKE_VERSION=4.2.3      version string
  FAKE_IMAGES entries starting with "seq:" are image sequences (e.g. seq:/abs/passes/pass_0001.exr)
  FAKE_SEQ_USER=1,0,0,0   start,offset,duration,cyclic of the node using the image sequence
  FAKE_PNG_COLOR=0,255,0  colour of rendered frames (consistency check tests)
"""
import os
import struct
import sys
import time
import types
import zlib

argv = sys.argv[1:]
if "--version" in argv:
    print("Blender %s\n\tbuild date: 2024-10-14" % os.environ.get("FAKE_VERSION", "4.2.3 LTS"))
    sys.exit(0)

env = os.environ
if env.get("FAKE_LOAD_FAIL"):
    print("Error: Cannot read file (fake load failure)")
    sys.exit(11)


def tiny_png(path):
    col = bytes(int(x) for x in env.get("FAKE_PNG_COLOR", "0,255,0").split(","))
    raw = b"\x00" + col
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(data)


NS = types.SimpleNamespace
fs, fe = (int(x) for x in env.get("FAKE_FRAMES", "1-10").split("-"))
ims = NS(file_format="FFMPEG" if env.get("FAKE_VIDEO") else "PNG", color_depth="8", color_mode="RGBA")
class RenderNS(types.SimpleNamespace):
    @property
    def file_extension(self):
        return ".png" if self.image_settings.file_format == "PNG" else ".mp4"


render = RenderNS(engine="CYCLES", fps=24, fps_base=1.0, resolution_x=1920, resolution_y=1080,
                  resolution_percentage=100, image_settings=ims, film_transparent=False,
                  use_persistent_data=False, ffmpeg=NS(format="", codec=""), filepath="", use_sequencer=True)


class Scene(types.SimpleNamespace):
    def frame_set(self, f):
        self.frame_current = f

    def sequence_editor_create(self):
        class Elements(list):
            def append(self, x):
                list.append(self, x)
        class Strips:
            def __init__(s): s.items = []
            def new_image(s, name, filepath, channel, frame_start):
                st = NS(name=name, filepath=filepath, elements=Elements([os.path.basename(filepath)]),
                        frame_final_duration=1)
                s.items.append(st)
                return st
        self._se = NS(strips=Strips())
        return self._se


scene = Scene(sequence_editor=None, name="Scene", render=render, cycles=NS(samples=128, device="CPU"),
              eevee=NS(taa_render_samples=64), frame_start=fs, frame_end=fe, frame_step=1,
              frame_current=fs, camera=None if env.get("FAKE_NO_CAMERA") else NS(name="Camera"),
              timeline_markers=[], view_layers=[NS(name="ViewLayer", use=True)],
              view_settings=NS(view_transform="AgX", look="None", exposure=0.0, gamma=1.0),
              compositing_node_group=None, use_nodes=False, objects=[], rigidbody_world=None)

handlers = NS(render_stats=[])
state = {"result": False}
marker_dir = env.get("TMPDIR", "/tmp")


def op_render(write_still=False, animation=False):
    if animation:
        out = render.filepath + "%04d-%04d.mp4" % (scene.frame_start, scene.frame_end)
        with open(out, "wb") as f:
            f.write(b"fake mp4")
        return {"FINISHED"}
    f = scene.frame_current
    for img in bpy.data.images:
        if getattr(img, "source", "") == "SEQUENCE" and not img.packed_file:
            p = seq_file_for(img, f)
            if p and not os.path.exists(p):
                print('Error: Cannot read image file "%s": No such file or directory' % p, flush=True)
    total = scene.cycles.samples
    ft = float(env.get("FAKE_FRAME_TIME", "0.2"))
    steps = 4
    for i in range(1, steps + 1):
        time.sleep(ft / steps)
        if env.get("FAKE_NEW_LOG"):   # Blender 5.x: no status lines, stats only via handler
            s = f"Frame:{f} | Time:00:0{i}.00 | Remaining:00:0{steps - i}.50 | Mem: {200 + f}M | Sample {total * i // steps}/{total}"
            print(f"00:0{i}.000  render           | something", flush=True)
        else:
            s = (f"Fra:{f} Mem:120.00M (Peak 150.00M) | Time:00:0{i}.00 | Remaining:00:0{steps - i}.50 | "
                 f"Mem:80.00M, Peak:{100 + f}.00M | Scene, ViewLayer | Sample {total * i // steps}/{total}")
            print(s, flush=True)
        for h in handlers.render_stats:
            h(s)
        crash_once = [int(x) for x in env.get("FAKE_CRASH_ONCE", "").split(",") if x]
        always = [int(x) for x in env.get("FAKE_ALWAYS_CRASH", "").split(",") if x]
        if i == 2 and f in always:
            print("Writing: /tmp/fake.crash.txt", flush=True)
            open(os.path.join(marker_dir, "fake.crash.txt"), "w").write("fake crash")
            os._exit(139)
        flag = os.path.join(marker_dir, f"crashed_{f}")
        if i == 2 and f in crash_once and not os.path.exists(flag):
            open(flag, "w").close()
            os._exit(139)
    state["result"] = True
    return {"FINISHED"}


class Images(list):
    def get(self, k, d=None):
        if k == "Render Result" and state["result"]:
            def save(path, scene=None):
                if str(scene.frame_current) in env.get("FAKE_DISK_FULL", "").split(","):
                    raise OSError(28, "No space left on device")
                tiny_png(path)
            return NS(save_render=save)
        return d


import json  # noqa: E402

scene.keys = lambda: []
current = {"file": os.path.abspath(argv[argv.index("-b") + 1]) if "-b" in argv and
           not argv[argv.index("-b") + 1].startswith("-") else ""}


def abspath(p, library=None):
    if p.startswith("//"):
        base = os.path.dirname(abspath(library.filepath) if library is not None else current["file"])
        return os.path.normpath(os.path.join(base, p[2:]))
    return p


def seq_file_for(img, f):
    u = [int(x) for x in env.get("FAKE_SEQ_USER", "1,0,0,0").split(",")]
    n = f - u[0] + 1 + u[1]
    path = abspath(img.filepath)
    d, base = os.path.split(path)
    import re as _re
    m = list(_re.finditer(r"\d+", base))
    if not m:
        return None
    m = m[-1]
    return os.path.join(d, base[:m.start()] + str(n).zfill(len(m.group())) + base[m.end():])


def make_image(path, source=None):
    if path.startswith("seq:"):
        path, source = path[4:], "SEQUENCE"
    img = NS(name=os.path.basename(path), filepath=path, library=None, packed_file=None, source=source or "FILE")
    img.pack = lambda: setattr(img, "packed_file", True)
    return img


libtree = json.loads(env.get("FAKE_LIBTREE") or "{}")


def load_data(path, is_main):
    saved = None
    try:
        with open(path) as fh:
            saved = json.load(fh) if path else None
    except (OSError, ValueError, UnicodeDecodeError):
        saved = None
    if saved and saved.get("fake_blend"):          # a copy written by save_as_mainfile below
        libs = []
        main_dir = os.path.dirname(os.path.abspath(path))
        for p in saved["libraries"]:
            parent = None
            if "|" in p:
                i, p = p.split("|", 1)
                parent = libs[int(i)]
                if p.startswith("//"):
                    # like Blender 5: an indirect library's path is reported relative to the open file
                    pdir = os.path.dirname(os.path.join(main_dir, parent.filepath[2:]))
                    p = "//" + os.path.relpath(os.path.join(pdir, p[2:]), main_dir).replace(os.sep, "/")
            libs.append(NS(filepath=p, parent=parent))
        imgs = Images()
        for fp, packed, src in saved["images"]:
            im = make_image(fp, src)
            im.packed_file = packed or None
            imgs.append(im)
    else:
        key = "main" if is_main else os.path.normpath(path)
        libs = []
        for p in libtree.get(key, []):         # "0|//shared.blend" = linked indirectly through library 0
            parent = None
            if "|" in p:
                i, p = p.split("|", 1)
                parent = libs[int(i)]
            libs.append(NS(filepath=p, parent=parent))
        imgs = Images(make_image(p) for p in (env.get("FAKE_IMAGES", "").split(";") if is_main else []) if p)
    mats = []
    for im in imgs:
        if im.source == "SEQUENCE":
            u = [int(x) for x in env.get("FAKE_SEQ_USER", "1,0,0,0").split(",")]
            iu = NS(frame_start=u[0], frame_offset=u[1], frame_duration=u[2], use_cyclic=bool(u[3]))
            mats.append(NS(node_tree=NS(nodes=[NS(image=im, image_user=iu, bl_idname="ShaderNodeTexImage")])))
    groups = [NS(name=n, nodes=[]) for n in env.get("FAKE_NODE_GROUPS", "").split(",") if n and is_main]
    bpy.data = NS(images=imgs, libraries=libs, sounds=[], movieclips=[], fonts=[], volumes=[], cache_files=[],
                  node_groups=groups, materials=mats, worlds=[], scenes=[scene], objects=[], cameras=[], lights=[],
                  meshes=[], textures=[], filepath=current["file"])
    bpy.data.images.load = load_png


def save_as_mainfile(filepath, copy=False, relative_remap=True, compress=False):
    with open(filepath, "w") as f:
        json.dump({"fake_blend": True, "from": current["file"],
                   "libraries": [("%d|" % bpy.data.libraries.index(l.parent) if l.parent else "") + l.filepath
                                 for l in bpy.data.libraries],
                   "images": [[i.filepath, bool(i.packed_file), i.source] for i in bpy.data.images]}, f)
    return {"FINISHED"}


def open_mainfile(filepath, load_ui=False):
    if not os.path.exists(filepath):
        raise RuntimeError("Cannot read file '%s'" % filepath)
    current["file"] = os.path.abspath(filepath)
    load_data(filepath, False)
    return {"FINISHED"}


def load_png(path, check_existing=False):
    """Reads the 1x1 PNGs this stand-in writes (for the consistency check)."""
    with open(path, "rb") as fh:
        data = fh.read()
    i = data.index(b"IDAT")
    n = struct.unpack(">I", data[i - 4:i])[0]
    raw = zlib.decompress(data[i + 4:i + 4 + n])
    px = [raw[1] / 255.0, raw[2] / 255.0, raw[3] / 255.0, 1.0]

    class Pixels(list):
        def foreach_get(self, arr):
            for k, v in enumerate(self):
                arr[k] = v
    return NS(size=(1, 1), channels=4, pixels=Pixels(px))


addon_mods = []
enabled = {}
for spec in [x for x in env.get("FAKE_ADDONS", "").split(";") if x]:
    mod, name, ver = (spec.split(":") + ["", ""])[:3]
    m = types.ModuleType(mod)
    m.__file__ = "/home/user/addons/%s/__init__.py" % mod
    m.bl_info = {"name": name, "version": tuple(int(v) for v in ver.split(".") if v)}
    addon_mods.append(m)
    enabled[mod] = NS(preferences=None)
addon_utils = types.ModuleType("addon_utils")
addon_utils.modules = lambda refresh=False: list(addon_mods)
addon_utils.module_bl_info = lambda m: m.bl_info
sys.modules["addon_utils"] = addon_utils

bpy = types.ModuleType("bpy")
bpy.context = NS(scene=scene, preferences=NS(addons=enabled))
bpy.app = NS(version_string=env.get("FAKE_VERSION", "4.2.3 LTS"), handlers=handlers)
bpy.ops = NS(render=NS(render=op_render), wm=NS(save_as_mainfile=save_as_mainfile, open_mainfile=open_mainfile))
bpy.path = NS(abspath=abspath)
bpy.utils = NS(system_resource=lambda kind: "/fake/blender/scripts")
load_data(current["file"], True)
sys.modules["bpy"] = bpy

script = argv[argv.index("--python") + 1]
sys.argv = ["blender"] + argv
code = compile(open(script).read(), script, "exec")
g = {"__name__": "__main__", "__file__": script}
exec(code, g)
