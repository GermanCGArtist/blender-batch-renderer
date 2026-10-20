# Runs inside Blender:  blender -b file.blend --python bbr_scenes.py
#
# Lists the scenes of the file (with their view layers, cameras and frame range) for the
# node editor, which shows one output socket per scene on the job node.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy  # noqa: E402
import bbr_common as C  # noqa: E402


def main():
    active = bpy.context.scene
    scenes = []
    for sc in bpy.data.scenes:
        cams = [o.name for o in getattr(sc, "objects", []) if getattr(o, "type", "") == "CAMERA"]
        scenes.append({
            "name": sc.name,
            "active": sc is active,
            "engine": sc.render.engine,
            "frame_start": sc.frame_start,
            "frame_end": sc.frame_end,
            "frame_step": sc.frame_step,
            "res_x": sc.render.resolution_x,
            "res_y": sc.render.resolution_y,
            "res_pct": sc.render.resolution_percentage,
            "camera": sc.camera.name if sc.camera else None,
            "cameras": cams,
            "view_layers": [vl.name for vl in sc.view_layers],
        })
    C.out("SCENES", {"scenes": scenes, "blender_version": bpy.app.version_string})


main()
