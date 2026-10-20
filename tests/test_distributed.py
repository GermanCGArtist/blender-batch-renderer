"""Rendering on several computers, simulated on one: a coordinator (runner + API server) and
worker clients, each with its own stand-in Blender.  Run:  python3 tests/test_distributed.py"""
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="bbr_dist_")
os.environ["BBR_DATA_DIR"] = os.path.join(TMP, "appdata")
os.environ["BBR_DISCOVERY_PORT"] = str(20000 + os.getpid() % 20000)

from bbr import scheduler as sched_mod  # noqa: E402
from bbr.api import ApiServer, DirectExecutor  # noqa: E402
from bbr.control import Controller  # noqa: E402
from bbr.installs import probe  # noqa: E402
from bbr.jobqueue import JobQueue  # noqa: E402
from bbr.models import AppSettings, JobSettings  # noqa: E402
from bbr.runner import Runner  # noqa: E402
from bbr.worker import Worker, pair, CoordinatorError, list_addons  # noqa: E402
from bbr.workerapi import discover  # noqa: E402
from bbr.package import build_package  # noqa: E402

FAKE = os.path.join(HERE, "fake_blender.py")
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def fake_exe(name, **env):
    """A Blender stand-in with its own environment (add-ons, speed, crashes) per machine."""
    p = os.path.join(TMP, "bin", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    lines = ["#!/bin/sh"] + [f"export {k}='{v}'" for k, v in env.items()]
    lines.append(f'exec "{sys.executable}" "{FAKE}" "$@"')
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return probe(p)


def wait_for(cond, timeout=60, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def make_blend(name, sub="src"):
    p = os.path.join(TMP, sub, name + ".blend")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"BLENDER-v402" + b"\0" * 100)
    return p


class Coordinator:
    def __init__(self, inst, render_locally=False):
        for k in list(os.environ):
            if k.startswith("FAKE_"):
                del os.environ[k]
        shutil.rmtree(os.environ["BBR_DATA_DIR"], ignore_errors=True)
        self.st = AppSettings.load()
        self.st.installs = [inst.to_dict()]
        self.st.keep_awake = False
        self.st.share_with_workers = True
        self.st.render_locally = render_locally
        self.st.api_port = 0
        self.q = JobQueue()
        self.q.load()
        self.events = []
        self.r = Runner(self.q, self.st, lambda k, p: self.events.append((k, p)))
        self.api = ApiServer(Controller(self.q, self.r, self.st), DirectExecutor(), self.st)
        assert self.api.start(), self.api.error
        self.port = self.api.httpd.server_address[1]
        self.st.api_port = self.port
        self.url = f"http://127.0.0.1:{self.port}"

    def add(self, blend, **kw):
        d = dict(render_root=os.path.join(TMP, "render"), retry_delay_s=0, make_preview=False,
                 consistency_check=False)
        d.update(kw)
        return self.q.add(blend, JobSettings(**d))

    def messages(self):
        return [p["text"] for k, p in self.events if k == "message"]

    def close(self):
        self.r.stop_now()
        self.r.wait(10)
        self.api.stop()
        self.r.scheduler.close()


def make_worker(c, name, inst, paired=True):
    ws = AppSettings()
    ws.save = lambda: None
    ws.installs = [inst.to_dict()]
    ws.worker_id = "w" + name.lower().replace(" ", "")[:10] + "x"
    ws.worker_name = name
    ws.mode = "worker"
    w = Worker(ws, lambda k, p: None)
    w.cache_dir = __import__("pathlib").Path(TMP) / f"cache_{ws.worker_id}"
    w.cache_dir.mkdir(parents=True, exist_ok=True)
    if paired:
        ws.worker_server = c.url
        ws.worker_key = pair(c.url, c.st.pairing_code, ws.worker_id, name)["key"]
    return w


# ---------------------------------------------------------------------------------
print("1) job package: linked libraries (also nested), textures, add-on detection")
libdir = os.path.join(TMP, "proj", "libs")
os.makedirs(libdir, exist_ok=True)
for n in ("chars.blend", "props.blend", "shared.blend"):
    open(os.path.join(libdir, n), "w").write("lib " + n)
tex = os.path.join(TMP, "proj", "tex", "wood.png")
os.makedirs(os.path.dirname(tex), exist_ok=True)
open(tex, "wb").write(b"png")
tree = {"main": ["//libs/chars.blend", "//libs/props.blend"],
        os.path.normpath(os.path.join(libdir, "chars.blend")): ["//shared.blend"],
        os.path.normpath(os.path.join(libdir, "props.blend")): ["//shared.blend"]}
coord_inst = fake_exe("blender_coord", FAKE_ADDONS="lens_sim:LensSim:1.2.0;tools:Handy Tools:2.0",
                      FAKE_NODE_GROUPS="LensSim Lens,Other", FAKE_LIBTREE=json.dumps(tree),
                      FAKE_IMAGES=tex + ";//tex/missing.png", FAKE_FRAME_TIME="0.05")
c = Coordinator(coord_inst)
blend = make_blend("shot", "proj")
job = c.add(blend)
job.ensure_folders()
lines = []
man = build_package(job, coord_inst, lines.append)
from bbr.package import load_manifest  # noqa: E402
job.package = man
M = load_manifest(job)
vdir = job.package_dir / f"v_{man['hash']}"
check(man.get("hash") and (vdir / "main.blend").exists(), "job copy written (version folder)")
names = sorted(p for p, e in M["files"].items() if e["kind"] == "scene")
main = json.loads((vdir / "main.blend").read_text())
libs = {n: json.loads((vdir / n).read_text()) for n in names if n.startswith("libs/")}
check("main.blend" in names and len(libs) == 3, f"main + 3 libraries (shared once) ({names})")
check(all(p.startswith("//libs/") for p in main["libraries"]), "main file links point into libs/")
check(all(p.startswith("//") and "/" not in p[2:] for d in libs.values() for p in d["libraries"]),
      "libraries link each other side by side")
check(main["images"][0][0].startswith("//a/") and main["images"][0][1] is False,
      "textures are separate files, not packed")
check(any(e["kind"] == "asset" and e["src"] == tex and e["hash"] for e in M["files"].values()),
      "texture listed as an asset with its checksum")
check(not list(job.package_dir.glob("*.zip")), "no zip any more")
check(any("missing image" in w for w in man["warnings"]), "missing texture reported")
check(man["addons_required"] == ["lens_sim"], f"LensSim detected as required ({man['addons_required']})")
check(len(man["libraries"]) == 3 and man["blender"] == "4.2", "libraries + Blender version in manifest")
check(man["scene_info"]["frame_end"] == 10, "scene info in manifest")
info = list_addons(coord_inst)
check(set(info["addons"]) == {"lens_sim", "tools"} and info["addons"]["lens_sim"]["version"] == "1.2.0",
      "add-on listing with versions")
c.close()

# ---------------------------------------------------------------------------------
print("2) pairing and access checks")
c = Coordinator(coord_inst)
try:
    pair(c.url, "000000" if c.st.pairing_code != "000000" else "111111", "wbadx1", "Bad")
    check(False, "wrong pairing code refused")
except CoordinatorError as e:
    check(e.status == 401, "wrong pairing code refused")
req = urllib.request.Request(c.url + "/api/v1/worker/lease", data=b"{}", method="POST",
                             headers={"X-BBR-Machine": "wnokey1", "X-BBR-Protocol": "2"})
try:
    urllib.request.urlopen(req, timeout=5)
    check(False, "worker endpoint without key refused")
except urllib.error.HTTPError as e:
    check(e.code == 401, "worker endpoint without key refused")
d = pair(c.url, c.st.pairing_code, "wgood1", "Good")
check(d["key"] and d["key"] != c.st.api_key, "pairing hands out a worker key (not the API key)")
from bbr.worker import _request  # noqa: E402
H_ok = {"X-BBR-Worker-Key": d["key"], "X-BBR-Machine": "wgood1", "X-BBR-Protocol": "2"}
check(_request(c.url, "POST", "/api/v1/worker/hello", {"name": "Good"}, H_ok)[0] == 200, "its own key works")
check(_request(c.url, "POST", "/api/v1/worker/hello", {"name": "X"}, dict(H_ok, **{"X-BBR-Machine": "wother1"}))[0] == 401,
      "the key does not work for another machine id")
old_code = c.st.pairing_code
d2 = pair(c.url, old_code, "wsecond1", "Second")
check(c.st.pairing_code != old_code, "the pairing code changes after each pairing")
try:
    pair(c.url, old_code, "wthird1", "Third")
    check(False, "a pairing code works only once")
except CoordinatorError as e:
    check(e.status == 401, "a pairing code works only once")
c.r.scheduler.forget("wgood1")
check(_request(c.url, "POST", "/api/v1/worker/hello", {"name": "Good"}, H_ok)[0] == 401,
      "after 'Unpair and remove' the old key is refused")
H2 = {"X-BBR-Worker-Key": d2["key"], "X-BBR-Machine": "wsecond1", "X-BBR-Protocol": "2"}
check(_request(c.url, "POST", "/api/v1/worker/hello", {"name": "Second"}, H2)[0] == 200, "other workers keep working")
c.r.scheduler.close(); c.r.scheduler = __import__("bbr.scheduler", fromlist=["Scheduler"]).Scheduler(c.q, c.st)
check(c.r.scheduler.machines["wsecond1"].key_hash and "wgood1" not in c.r.scheduler.machines,
      "keys survive a restart, removed ones stay removed")
found = discover(timeout=1.0)
check(any(f["url"].endswith(f":{c.port}") for f in found), f"coordinator found on the network ({found})")
from bbr.workerapi import _safe_rel, WorkerError  # noqa: E402
for bad in ("../x.png", "/etc/passwd", "a/../../b", ".hidden"):
    try:
        _safe_rel(bad)
        check(False, f"unsafe upload path {bad!r} refused")
    except WorkerError:
        check(True, f"unsafe upload path {bad!r} refused")
c.close()

# ---------------------------------------------------------------------------------
print("3) two workers (fast PC, slower Mac) render a job, coordinator only coordinates")
c = Coordinator(coord_inst)
job = c.add(make_blend("anim", "proj"), frame_start=1, frame_end=12)
pc = make_worker(c, "Gaming PC", fake_exe("blender_pc", FAKE_ADDONS="lens_sim:LensSim:1.2.0",
                                           FAKE_FRAME_TIME="0.1", FAKE_NEW_LOG="1"))
mac = make_worker(c, "MacBook", fake_exe("blender_mac", FAKE_ADDONS="lens_sim:LensSim:1.1.0",
                                          FAKE_FRAME_TIME="0.3"))
pc.start(); mac.start()
c.r.start()
ok = wait_for(lambda: job.status == "done", 90)
check(ok, f"job done ({job.status} {job.status_note}, {job.count('done')} frames)")
machines = {r.get("machine") for r in job.frames.values()}
check(machines == {"Gaming PC", "MacBook"}, f"both machines rendered frames ({machines})")
files = sorted(os.listdir(job.frames_dir))
check(files == [f"anim_{i:04d}.png" for i in range(1, 13)], f"all 12 frames on the coordinator ({files})")
by = {d["machine"]: d for d in c.r.scheduler.job_machine_stats(job)}
check(by["Gaming PC"]["frames"] > by["MacBook"]["frames"], f"faster machine did more frames "
      f"({by['Gaming PC']['frames']} vs {by['MacBook']['frames']})")
pub = c.r.scheduler.machines_public()
check(pub["combined"]["batch"]["frames"] == 12, "combined statistics count every frame")
mp = {m["name"]: m for m in pub["machines"]}
check(mp["Gaming PC"]["batch"]["frames"] + mp["MacBook"]["batch"]["frames"] == 12, "per-machine statistics")
check(mp["Gaming PC"]["installs"][0]["addons"].get("lens_sim"), "coordinator knows the workers' add-ons")
check(pc.session["frames"] == by["Gaming PC"]["frames"], "worker's own statistics match")
wait_for(lambda: not c.r.running, 10)
from bbr import reports  # noqa: E402
reports.write_xlsx(job)
check(os.path.exists(job.data_dir / "report.xlsx"), "report with Machines sheet written")
csv_text = open(job.data_dir / "frames.csv").read()
check("Gaming PC" in csv_text and "MacBook" in csv_text, "frames.csv has the machine column")
check(not any(p.name.endswith(".part") for p in pc.cache_dir.iterdir()), "package cached, no partial download")
check(len(list((pc.cache_dir / "work" / job.id / "frames").glob("*.png"))) == 0, "worker deleted delivered frames")
pc.stop(); mac.stop()
c.close()

# ---------------------------------------------------------------------------------
print("4) missing add-on: the worker without LensSim gets no frames")
c = Coordinator(coord_inst, render_locally=True)
job = c.add(make_blend("lens", "proj"), frame_start=1, frame_end=6)
bare = make_worker(c, "Laptop", fake_exe("blender_bare", FAKE_FRAME_TIME="0.05"))
bare.start()
c.r.start()
check(wait_for(lambda: job.status == "done", 60), f"job done by the coordinator itself ({job.status})")
m = next(m for m in c.r.scheduler.machines.values() if m.name == "Laptop")
check(not any(r.get("machine") == "Laptop" for r in job.frames.values()), "no frames for the worker")
check("LensSim" in (c.r.scheduler.compatible(job, m) or ""), "reason names the missing add-on")
check("missing add-on" in bare.note or bare.state == "idle", f"worker shows why it is idle ({bare.note})")
# the same job with "ignore add-ons": the worker joins in
job.settings.ignore_addons = True
check(c.r.scheduler.compatible(job, m) is None, "with 'ignore add-ons' the worker may render the job")
bare.stop()
c.close()

# ---------------------------------------------------------------------------------
print("5) a worker crashes on a frame: retried elsewhere, counted per machine")
c = Coordinator(coord_inst)
job = c.add(make_blend("crashy", "proj"), frame_start=1, frame_end=8, max_attempts=3)
a = make_worker(c, "Crashy", fake_exe("blender_crashy", FAKE_ADDONS="lens_sim:LensSim:1.2.0",
                                       FAKE_FRAME_TIME="0.1", FAKE_ALWAYS_CRASH="3,4"))
b = make_worker(c, "Solid", fake_exe("blender_solid", FAKE_ADDONS="lens_sim:LensSim:1.2.0",
                                      FAKE_FRAME_TIME="0.15"))
a.start(); b.start()
c.r.start()
check(wait_for(lambda: job.status in ("done", "done (with errors)") and not c.r.running, 90),
      f"job finished ({job.status})")
check(job.count("done") == 8, f"all 8 frames done ({job.count('done')})")
for f in ("3", "4"):
    rec = job.frames[f]
    if "Crashy" in (rec.get("failed_on") or []):
        check(rec.get("machine") == "Solid", f"frame {f} failed on Crashy, finished on Solid")
mc = next(m for m in c.r.scheduler.machines.values() if m.name == "Crashy")
check(mc.batch["failed"] >= 1 or job.frames["3"].get("machine") == "Solid", "failures counted for that machine")
a.stop(); b.stop()
c.close()

# ---------------------------------------------------------------------------------
print("6) a machine disappears (Wi-Fi drop): its frame goes back into the pool")
old_lease = sched_mod.LEASE_SECONDS
sched_mod.LEASE_SECONDS = 2
c = Coordinator(coord_inst)
job = c.add(make_blend("wifi", "proj"), frame_start=1, frame_end=5)
S = c.r.scheduler
S.register({"id": "ghost1", "name": "Ghost", "installs": [{"version": "4.2", "label": "x",
                                                             "addons": {"lens_sim": {}}}]})
c.r.start()
got = {}
wait_for(lambda: got.update(S.lease_remote("ghost1")) or got.get("lease"), 30, 0.2)
ghost_frame = (got.get("lease") or {}).get("frame")
check(ghost_frame is not None, f"ghost machine got frame {ghost_frame}")
w = make_worker(c, "Desk PC", fake_exe("blender_desk", FAKE_ADDONS="lens_sim:LensSim:1.2.0", FAKE_FRAME_TIME="0.1"))
w.start()
check(wait_for(lambda: job.status == "done", 60), f"job done without the ghost ({job.status})")
check(job.frames[str(ghost_frame)].get("machine") == "Desk PC", "the ghost's frame was rendered by another machine")
check(any("stopped answering" in t for t in c.messages()), "message about the silent machine")
w.stop()
c.close()
sched_mod.LEASE_SECONDS = old_lease

# ---------------------------------------------------------------------------------
print("7) 'stop now' on the coordinator: workers drop their frame, nothing counted as failed, resume later")
c = Coordinator(coord_inst)
job = c.add(make_blend("stopnow", "proj"), frame_start=1, frame_end=20)
w1 = make_worker(c, "One", fake_exe("blender_one", FAKE_ADDONS="lens_sim:LensSim:1.2.0", FAKE_FRAME_TIME="0.4"))
w2 = make_worker(c, "Two", fake_exe("blender_two", FAKE_ADDONS="lens_sim:LensSim:1.2.0", FAKE_FRAME_TIME="0.4"))
w1.start(); w2.start()
c.r.start()
wait_for(lambda: job.count("done") >= 3, 60)
c.r.stop_now()
check(wait_for(lambda: not c.r.running, 20), "batch stopped")
check(wait_for(lambda: w1.current is None and w2.current is None, 20), "workers dropped their frames")
check(job.count("failed") == 0 and not any(r.get("failed_on") for r in job.frames.values()), "nothing failed")
check(job.status == "stopped", f"job stopped ({job.status})")
done_before = job.count("done")
time.sleep(1)
check(job.count("done") == done_before, "no frames arrive after the stop")
c.r.start()
check(wait_for(lambda: job.status == "done", 90), f"resumed and finished ({job.status}, {job.count('done')})")
w1.stop(); w2.stop()
c.close()

# ---------------------------------------------------------------------------------
print("8) 'stop after frame': workers finish and deliver their frame first")
c = Coordinator(coord_inst)
job = c.add(make_blend("drain", "proj"), frame_start=1, frame_end=20)
w1 = make_worker(c, "Uno", fake_exe("blender_uno", FAKE_ADDONS="lens_sim:LensSim:1.2.0", FAKE_FRAME_TIME="0.5"))
w1.start()
c.r.start()
wait_for(lambda: w1.state == "rendering", 60)
before = job.count("done")
c.r.stop_after_frame()
check(wait_for(lambda: not c.r.running, 30), "batch stopped after the frame")
check(job.count("done") == before + 1, f"the frame in progress was delivered ({before} -> {job.count('done')})")
w1.stop()
c.close()

# ---------------------------------------------------------------------------------
print("9) coordinator renders too, together with a worker")
c = Coordinator(fake_exe("blender_coord_slow", FAKE_ADDONS="lens_sim:LensSim:1.2.0", FAKE_NODE_GROUPS="LensSim Lens",
                         FAKE_FRAME_TIME="0.25"), render_locally=True)
job = c.add(make_blend("together", "proj"), frame_start=1, frame_end=40)
w = make_worker(c, "Helper", fake_exe("blender_helper", FAKE_ADDONS="lens_sim:LensSim:1.2.0", FAKE_FRAME_TIME="0.1"))
w.start()
wait_for(lambda: w.registered, 20)
t0 = time.time()
c.r.start()
check(wait_for(lambda: job.status == "done" and not c.r.running, 90), f"done ({job.status})")
names = {r.get("machine") for r in job.frames.values()}
print("     took %.1fs, helper did %d" % (time.time() - t0, sum(1 for r in job.frames.values() if r.get("machine") == "Helper")))
check(len(names) == 2 and "Helper" in names, f"both rendered ({names})")
check(job.count("done") == 40 and len(os.listdir(job.frames_dir)) == 40, "40 frames, no duplicates")
w.stop()
c.close()

# ---------------------------------------------------------------------------------
print("10) device per computer: GPU on the PC, this computer switched off for the job; CPU here for another")
c = Coordinator(coord_inst, render_locally=True)
local = c.r.scheduler.local.name
job = c.add(make_blend("lenskit", "proj"), frame_start=1, frame_end=6)
job.settings.devices = {local: "off", "GPU PC": "gpu"}
job2 = c.add(make_blend("bigvram", "proj"), frame_start=1, frame_end=3)
job2.settings.devices = {local: "cpu", "GPU PC": "off"}
w = make_worker(c, "GPU PC", fake_exe("blender_gpupc", FAKE_ADDONS="lens_sim:LensSim:1.2.0", FAKE_FRAME_TIME="0.05"))
w.start()
wait_for(lambda: w.registered, 20)
c.r.start()
check(wait_for(lambda: job.status == "done" and job2.status == "done" and not c.r.running, 90),
      f"both jobs done ({job.status}, {job2.status})")
check({r.get("machine") for r in job.frames.values()} == {"GPU PC"}, "job 1 rendered only on the PC")
check({r.get("machine") for r in job2.frames.values()} == {local}, "job 2 rendered only here")
log = "\n".join(t for _s, t in w.console_tail)
check("device=GPU" in log, "the PC was told to use its GPU")
check("device=CPU" in str((job2.scene_info or {}).get("overrides_applied")), "this computer used the CPU for job 2")
m = next(m for m in c.r.scheduler.machines.values() if m.name == "GPU PC")
check(c.r.scheduler.compatible(job2, m) == "switched off for this job", "reason shown for the switched-off computer")
w.stop()
c.close()

shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
