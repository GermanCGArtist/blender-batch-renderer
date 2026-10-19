# Runs inside Blender (background, with the user's preferences): lists enabled add-ons.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy  # noqa: E402,F401
import bbr_common as C  # noqa: E402

try:
    C.out("ADDONS", {"version": bpy.app.version_string, "addons": C.enabled_addons()})
except Exception as e:
    C.out("ADDONS", {"version": bpy.app.version_string, "addons": {}, "error": str(e)})
