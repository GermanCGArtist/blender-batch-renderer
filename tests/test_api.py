"""API, queue-file and default-folder tests with the fake Blender.  Run:  python3 tests/test_api.py"""
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="bbr_api_")
os.environ["BBR_DATA_DIR"] = os.path.join(TMP, "appdata")
os.environ["FAKE_FRAME_TIME"] = "0.03"

from bbr.api import ApiServer, DirectExecutor, forward_to_running_app  # noqa: E402
from bbr.control import Controller  # noqa: E402
from bbr.installs import probe  # noqa: E402
from bbr.jobqueue import JobQueue  # noqa: E402
from bbr.models import AppSettings, JobSettings  # noqa: E402
from bbr.runner import Runner  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def blend(name, sub="src"):
    p = os.path.join(TMP, sub, name + ".blend")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"BLENDER-v402" + b"\0" * 64)
    return p


st = AppSettings.load()
check(st.settings_version == 2 and st.defaults["render_root"] == "" and len(st.api_key) > 10,
      "settings migrated: default render folder next to .blend, API key created")
st.installs = [probe(os.path.join(HERE, "fake_blender.py")).to_dict()]
st.keep_awake = False
st.defaults["make_preview"] = False
st.defaults["retry_delay_s"] = 0
st.api_port = free_port()
q = JobQueue()
q.load()
events = []
r = Runner(q, st, lambda k, p: events.append(k))
ctl = Controller(q, r, st)
srv = ApiServer(ctl, DirectExecutor(), st)
check(srv.start(), "API server starts")
BASE = f"http://127.0.0.1:{st.api_port}"


def call(method, path, body=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers if headers is not None else {"X-BBR-Request": "1"})
    data = json.dumps(body).encode() if body is not None else (b"" if method != "GET" else None)
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ct = resp.headers.get("Content-Type", "")
            raw = resp.read()
            return resp.status, (json.loads(raw) if "json" in ct else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("API basics")
code, s = call("GET", "/api/v1/status")
check(code == 200 and s["running"] is False and s["batch"]["jobs"] == 0, "GET status")
code, page = call("GET", "/")
check(code == 200 and b"Blender Batch Renderer" in page, "status page served")
code, _ = call("GET", "/icon.png")
check(code == 200, "icon served")

print("security")
code, e = call("POST", "/api/v1/batch/start", {}, headers={})
check(code == 403 and "X-BBR-Request" in e["error"], "POST without X-BBR-Request header is refused")
code, e = call("GET", "/api/v1/status", headers={"Host": "evil.example.com"})
check(code == 403, "foreign Host header refused (DNS rebinding)")
code, e = call("GET", "/api/v1/status", headers={"Host": "evil.example.com", "X-API-Key": st.api_key})
check(code == 200, "...unless the API key is given")
code, e = call("POST", "/api/v1/jobs", {"blend": "/nope/x.blend"})
check(code == 400, "adding a missing .blend gives 400")
code, e = call("GET", "/api/v1/nothing")
check(code == 404, "unknown endpoint gives 404")

print("default render folder")
b1 = blend("shot_a", "projA")
code, j1 = call("POST", "/api/v1/jobs", {"blend": b1, "folder": "Project A"})
check(code == 200 and j1["job_folder"] == os.path.join(TMP, "projA", "render", "shot_a"),
      f"render folder next to the .blend ({j1.get('job_folder')})")
check(j1["folder"]["name"] == "Project A", "queue folder created by name")
b2 = blend("shot_b", "projB")
custom = os.path.join(TMP, "custom_out")
code, j2 = call("POST", "/api/v1/jobs", {"blend": b2, "render_root": custom, "frame_end": 4})
check(j2["job_folder"] == os.path.join(custom, "shot_b"), "custom render folder respected")

print("queue file")
qf = os.path.join(TMP, "setups", "night.bbrqueue")
os.makedirs(os.path.dirname(qf))
q.set_enabled(q.find(j2["id"]), False)
q.save_file(qf)
check(q.read_file_snapshot(qf)["jobs"] == q.snapshot()["jobs"], "saved file matches the queue (not modified)")
q.move(q.find(j1["id"]), 1)
q.clear()
check(len(q.jobs) == 0, "new empty queue")
w = q.load_file(qf)
check(not w and len(q.jobs) == 2 and not q.find(j2["id"]).enabled and q.groups[0]["name"] == "Project A",
      "queue file loads jobs, folders and on/off switches")
# move queue file together with render folders -> relative paths still work
moved = os.path.join(TMP, "moved")
os.makedirs(moved)
shutil.copytree(os.path.join(TMP, "projA"), os.path.join(moved, "projA"))
data = json.load(open(qf))
data["jobs"][0]["folder"] = "/does/not/exist"
data["jobs"][0]["rel"] = os.path.join("..", "projA", "render", "shot_a")
nq = os.path.join(moved, "setups", "night.bbrqueue")
os.makedirs(os.path.dirname(nq))
json.dump(data, open(nq, "w"))
snap = JobQueue.read_file_snapshot(nq)
check(snap["jobs"][0]["folder"] == os.path.join(moved, "projA", "render", "shot_a"),
      "relative path used when the queue file moved with the render folders")
check(JobQueue.read_file_snapshot(b1) is None, "a non-queue file is rejected")

print("render through the API")
q.load_file(qf)
q.set_enabled(q.find(j2["id"]), True)
code, res = call("POST", "/api/v1/batch/start")
check(code == 200 and res["started"], "start")
seen_live = False
t0 = time.time()
while time.time() - t0 < 30:
    code, s = call("GET", "/api/v1/status")
    if s["live"] and s["live"]["frame"] is not None:
        seen_live = True
    if not s["running"]:
        break
    time.sleep(0.05)
check(seen_live, "live frame info visible while rendering")
code, ql = call("GET", "/api/v1/queue")
check(all(j["status"] == "done" for j in ql["jobs"]), f"all jobs done ({[j['status'] for j in ql['jobs']]})")
code, d = call("GET", f"/api/v1/jobs/{j1['id']}")
check(code == 200 and d["frames"]["done"] == 10 and len(d["frame_times"]) == 10, "job detail")
code, img = call("GET", f"/api/v1/jobs/{j1['id']}/thumbnail")
check(code == 200 and img[:4] == b"\x89PNG", "thumbnail of the last frame")
code, c = call("GET", "/api/v1/console?since=0")
check(code == 200 and any("BBR_FRAME_DONE" in l["text"] for l in c["lines"]), "console lines")
code, d = call("POST", f"/api/v1/jobs/{j2['id']}/enabled", {"enabled": False})
check(code == 200 and d["enabled"] is False, "switch a job off")
code, d = call("DELETE", f"/api/v1/jobs/{j2['id']}")
check(code == 200 and q.find(j2["id"]) is None, "remove a job")
code, d = call("POST", "/api/v1/queue/open", {"path": qf})
check(code == 501, "opening queue files is refused without a window")
check(forward_to_running_app(st, qf) is False, "forwarding to an app without window fails cleanly")

print("Blender add-on against the API")
import importlib.util
import types
bpy = types.ModuleType("bpy")
def _prop(**kw):
    return kw.get("default")
bpy.props = types.SimpleNamespace(BoolProperty=_prop, EnumProperty=_prop, IntProperty=_prop, StringProperty=_prop)
bpy.types = types.SimpleNamespace(Operator=object, Panel=object, AddonPreferences=object,
                                  TOPBAR_MT_render=types.SimpleNamespace(append=lambda f: None, remove=lambda f: None))
b3 = blend("addon_shot", "projC")
bpy.data = types.SimpleNamespace(filepath=b3, is_dirty=False)
bpy.path = types.SimpleNamespace(abspath=lambda p: p, basename=os.path.basename)
bpy.ops = types.SimpleNamespace()
bpy.utils = types.SimpleNamespace(register_class=lambda c: None, unregister_class=lambda c: None)
sys.modules["bpy"] = bpy
sys.modules["bpy.props"] = bpy.props
spec = importlib.util.spec_from_file_location("bbr_send_to_queue",
                                              os.path.join(os.path.dirname(HERE), "blender_addon", "bbr_send_to_queue.py"))
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
addon.register()
ctx = types.SimpleNamespace(preferences=types.SimpleNamespace(addons={
    "bbr_send_to_queue": types.SimpleNamespace(preferences=types.SimpleNamespace(port=st.api_port))}))
op = addon.BBR_OT_send()
reports = []
op.report = lambda kind, msg: reports.append((kind, msg))
op.where, op.custom_folder, op.queue_folder = "NEXT", "", "From Blender"
op.use_range, op.frame_start, op.frame_end, op.save_first, op.start = True, 1, 3, True, False
res = op.execute(ctx)
added = [j for j in q.jobs if j.blend_path == b3]
check(res == {"FINISHED"} and added and added[0].settings.frame_end == 3, f"add-on adds the open file ({reports})")
check(added and added[0].folder == os.path.join(TMP, "projC", "render", "addon_shot"), "add-on: render folder next to .blend")
check(any(g["name"] == "From Blender" for g in q.groups), "add-on: queue folder created")
ctx.preferences.addons["bbr_send_to_queue"].preferences.port = free_port()
reports.clear()
res = op.execute(ctx)
check(res == {"CANCELLED"} and "not running" in reports[-1][1], "add-on explains when the app is not running")

srv.stop()
print()
print("FAILURES:" if failures else "ALL PASSED", *failures, sep="\n  ")
if not failures:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failures else 0)
