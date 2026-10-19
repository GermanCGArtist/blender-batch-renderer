# Runs inside Blender (factory startup): loads an image sequence into the
# video editor and renders it as an H.264 MP4.
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy  # noqa: E402
import bbr_common as C  # noqa: E402


def main():
    a = C.script_args()
    scene = bpy.context.scene
    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = a["res_x"], a["res_y"], 100
    r.fps = int(a["fps"])
    r.fps_base = float(a["fps_base"])
    step = max(1, int(a.get("step", 1)))
    fs = int(a["frame_start"])
    pos = lambda frame: (frame - fs) // step + 1  # noqa: E731
    scene.frame_start = 1
    scene.frame_end = pos(int(a["frame_end"]))

    se = scene.sequence_editor_create()
    strips = getattr(se, "strips", None)
    if strips is None:
        strips = se.sequences
    for i, run in enumerate(a["runs"]):
        first = os.path.join(run["dir"], run["files"][0])
        st = strips.new_image(name="frames_%d" % i, filepath=first, channel=1, frame_start=pos(run["start"]))
        for fn in run["files"][1:]:
            st.elements.append(fn)
        try:
            st.frame_final_duration = len(run["files"])
        except Exception:
            pass

    vs = scene.view_settings
    try:
        if a.get("linear_input"):
            if a.get("view_transform"):
                vs.view_transform = a["view_transform"]
            if a.get("look"):
                vs.look = a["look"]
            if a.get("exposure") is not None:
                vs.exposure = a["exposure"]
            if a.get("gamma") is not None:
                vs.gamma = a["gamma"]
        else:
            vs.view_transform = "Standard"   # PNG/JPG are already display-ready
            vs.look = "None"
    except Exception as e:
        C.warn("Could not set colour management for preview: %s" % e)

    ims = r.image_settings
    if hasattr(ims, "media_type"):
        ims.media_type = "VIDEO"
    ims.file_format = "FFMPEG"
    ff = r.ffmpeg
    ff.format = "MPEG4"
    ff.codec = "H264"
    for attr, val in (("constant_rate_factor", "HIGH"), ("ffmpeg_preset", "GOOD"), ("audio_codec", "NONE")):
        try:
            setattr(ff, attr, val)
        except Exception:
            pass
    r.use_sequencer = True
    r.filepath = os.path.join(a["out_dir"], "preview_")
    bpy.ops.render.render(animation=True)
    C.out("PREVIEW_DONE", {})


try:
    main()
except Exception:
    C.out("PREVIEW_ERROR", {"error": traceback.format_exc()})
    sys.exit(2)
