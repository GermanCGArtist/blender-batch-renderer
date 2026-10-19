# Blender add-on: "Send to Batch Renderer"
#
# Adds the open .blend file to the Blender Batch Renderer queue with one click.
# Install: Blender → Edit → Preferences → Add-ons → (▾) Install from Disk… → choose this file.
# Then: Render menu → "Send to Batch Renderer…", or Properties → Output → Batch Renderer panel.
#
# The Batch Renderer app must be running with its status page / API switched on
# (Settings → Status page & API; it is on by default).

bl_info = {
    "name": "Send to Batch Renderer",
    "author": "Blender Batch Renderer",
    "version": (1, 0, 0),
    "blender": (2, 93, 0),
    "location": "Render menu ▸ Send to Batch Renderer…  /  Properties ▸ Output ▸ Batch Renderer",
    "description": "Add the current file to the Blender Batch Renderer queue",
    "category": "Render",
}

import json
import urllib.error
import urllib.request
import webbrowser

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty


def _prefs(context):
    try:
        return context.preferences.addons[__name__].preferences
    except (KeyError, AttributeError):
        return None


def _base(context):
    p = _prefs(context)
    port = p.port if p else 8642
    return f"http://127.0.0.1:{port}"


def _request(context, method, path, body=None, timeout=5):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_base(context) + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-BBR-Request": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _error_text(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.loads(e.read().decode("utf-8")).get("error", str(e))
        except Exception:
            return str(e)
    if isinstance(e, urllib.error.URLError):
        return "Batch Renderer is not running (or its status page / API is switched off)"
    return str(e)


class BBR_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    port: IntProperty(name="Port", default=8642, min=1024, max=65535,
                      description="Port of the Batch Renderer status page / API (see its Settings)")

    def draw(self, context):
        self.layout.prop(self, "port")


class BBR_OT_send(bpy.types.Operator):
    """Add this .blend file to the Blender Batch Renderer queue"""
    bl_idname = "render.bbr_send_to_queue"
    bl_label = "Send to Batch Renderer"
    bl_options = {"REGISTER"}

    where: EnumProperty(name="Render folder", default="NEXT", items=[
        ("NEXT", "Next to the .blend", "A folder called 'render' beside the .blend file"),
        ("CUSTOM", "Custom folder", "Choose a folder"),
    ])
    custom_folder: StringProperty(name="Folder", subtype="DIR_PATH")
    queue_folder: StringProperty(name="Queue folder", description="Optional folder in the queue (created if missing)")
    use_range: BoolProperty(name="Override frame range", default=False)
    frame_start: IntProperty(name="Start", default=1)
    frame_end: IntProperty(name="End", default=250)
    save_first: BoolProperty(name="Save the file first", default=True,
                             description="The Batch Renderer renders the file as saved on disk")
    start: BoolProperty(name="Start rendering", default=False,
                        description="Start the batch right away if it is not running")

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath)

    def invoke(self, context, event):
        scene = context.scene
        self.frame_start, self.frame_end = scene.frame_start, scene.frame_end
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        lay = self.layout
        lay.label(text=bpy.path.basename(bpy.data.filepath), icon="FILE_BLEND")
        lay.prop(self, "where", expand=True)
        if self.where == "CUSTOM":
            lay.prop(self, "custom_folder")
        lay.prop(self, "queue_folder")
        lay.prop(self, "use_range")
        if self.use_range:
            row = lay.row(align=True)
            row.prop(self, "frame_start")
            row.prop(self, "frame_end")
        if bpy.data.is_dirty:
            lay.prop(self, "save_first")
        lay.prop(self, "start")

    def execute(self, context):
        if bpy.data.is_dirty:
            if self.save_first:
                bpy.ops.wm.save_mainfile()
            else:
                self.report({"WARNING"}, "Unsaved changes will not be rendered")
        body = {"blend": bpy.path.abspath(bpy.data.filepath), "start": self.start}
        if self.where == "CUSTOM":
            if not self.custom_folder:
                self.report({"ERROR"}, "Choose a custom folder")
                return {"CANCELLED"}
            body["render_root"] = bpy.path.abspath(self.custom_folder)
        else:
            body["render_root"] = ""
        if self.queue_folder.strip():
            body["folder"] = self.queue_folder.strip()
        if self.use_range:
            body["frame_start"], body["frame_end"] = self.frame_start, self.frame_end
        try:
            job = _request(context, "POST", "/api/v1/jobs", body)
        except Exception as e:
            self.report({"ERROR"}, _error_text(e))
            return {"CANCELLED"}
        msg = f"Added '{job.get('name')}' to the Batch Renderer queue"
        if self.start:
            msg += " – rendering starts"
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class BBR_OT_status(bpy.types.Operator):
    """Open the Batch Renderer status page in the web browser"""
    bl_idname = "render.bbr_open_status"
    bl_label = "Batch Renderer Status Page"

    def execute(self, context):
        webbrowser.open(_base(context) + "/")
        return {"FINISHED"}


class BBR_PT_output(bpy.types.Panel):
    bl_label = "Batch Renderer"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "output"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        col = self.layout.column(align=True)
        if not bpy.data.filepath:
            col.label(text="Save the file first", icon="ERROR")
        col.operator(BBR_OT_send.bl_idname, icon="RENDER_ANIMATION", text="Send to Batch Renderer…")
        col.operator(BBR_OT_status.bl_idname, icon="URL", text="Open status page")


def _menu(self, context):
    self.layout.separator()
    self.layout.operator(BBR_OT_send.bl_idname, icon="RENDER_ANIMATION", text="Send to Batch Renderer…")
    self.layout.operator(BBR_OT_status.bl_idname, icon="URL", text="Batch Renderer Status Page")


classes = (BBR_AddonPreferences, BBR_OT_send, BBR_OT_status, BBR_PT_output)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_render.append(_menu)


def unregister():
    bpy.types.TOPBAR_MT_render.remove(_menu)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
