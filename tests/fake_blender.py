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
"""
import os
import struct
import sys
import time
import types
import zlib

argv = sys.argv[1:]
if "--version" in argv:
    print("Blender 4.2.3 LTS\n\tbuild date: 2024-10-14")
    sys.exit(0)

env = os.environ
if env.get("FAKE_LOAD_FAIL"):
    print("Error: Cannot read file (fake load failure)")
    sys.exit(11)


def tiny_png(path):
    raw = b"\x00\xff\x00\x00"
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


scene = Scene(name="Scene", render=render, cycles=NS(samples=128, device="CPU"),
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
    total = scene.cycles.samples
    ft = float(env.get("FAKE_FRAME_TIME", "0.2"))
    steps = 4
    for i in range(1, steps + 1):
        time.sleep(ft / steps)
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


class Images(dict):
    def get(self, k, d=None):
        if k == "Render Result" and state["result"]:
            return NS(save_render=lambda path, scene=None: tiny_png(path))
        return d


bpy = types.ModuleType("bpy")
bpy.context = NS(scene=scene, preferences=NS(addons={}))
bpy.app = NS(version_string="4.2.3 LTS", handlers=handlers)
bpy.data = NS(images=Images(), libraries=[], sounds=[], movieclips=[], fonts=[], volumes=[], cache_files=[])
bpy.ops = NS(render=NS(render=op_render))
bpy.path = NS(abspath=lambda p, library=None: p)
sys.modules["bpy"] = bpy

script = argv[argv.index("--python") + 1]
sys.argv = ["blender"] + argv
code = compile(open(script).read(), script, "exec")
g = {"__name__": "__main__", "__file__": script}
exec(code, g)
