"""End-to-end tests of the engine with the fake Blender.  Run:  python3 tests/test_engine.py"""
import gzip
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="bbr_test_")
os.environ["BBR_DATA_DIR"] = os.path.join(TMP, "appdata")

from bbr.blendfile import read_blend_version  # noqa: E402
from bbr.installs import BlenderInstall, choose_install, probe  # noqa: E402
from bbr.jobqueue import JobQueue  # noqa: E402
from bbr.models import AppSettings, JobSettings, Job  # noqa: E402
from bbr.runner import Runner  # noqa: E402

FAKE = os.path.join(HERE, "fake_blender.py")
events = []
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def make_blend(name, header=b"BLENDER-v402"):
    p = os.path.join(TMP, "src", name + ".blend")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(header + b"\0" * 100)
    return p


def fresh(envs=None, **settings_kw):
    for k in list(os.environ):
        if k.startswith("FAKE_"):
            del os.environ[k]
    os.environ.update(envs or {})
    shutil.rmtree(os.environ["BBR_DATA_DIR"], ignore_errors=True)
    inst = probe(FAKE)
    st = AppSettings.load()
    st.installs = [inst.to_dict()]
    st.keep_awake = False
    q = JobQueue()
    q.load()
    return st, q


def run(st, q, until=None, timeout=60):
    events.clear()
    r = Runner(q, st, lambda k, p: events.append((k, p)))
    r.start()
    t0 = time.time()
    while r.running and time.time() - t0 < timeout:
        if until:
            until(r)
        time.sleep(0.02)
    r.wait(5)
    return r


def js(**kw):
    d = dict(render_root=os.path.join(TMP, "render"), retry_delay_s=0, make_preview=True)
    d.update(kw)
    return JobSettings(**d)


print("blend header parsing")
check(read_blend_version(make_blend("v402")) == (4, 2), "4.2 header")
check(read_blend_version(make_blend("v500", b"BLENDER17-01v0500")) == (5, 0), "5.0 header")
gz = os.path.join(TMP, "src", "gz.blend")
with gzip.open(gz, "wb") as f:
    f.write(b"BLENDER_v279" + b"\0" * 50)
check(read_blend_version(gz) == (2, 79), "gzip 2.79 header")

print("install matching")
I = lambda v: BlenderInstall("p%s" % (v,), "e", v, "Blender %s" % (v,))
insts = [I((3, 6, 5)), I((4, 2, 3)), I((4, 2, 1)), I((5, 0, 0))]
check(choose_install(insts, (4, 2))[0].version == (4, 2, 3), "exact match picks newest patch")
check(choose_install(insts, (4, 1))[0].version == (4, 2, 3), "missing version -> next newer")
check(choose_install(insts, (5, 1))[0].version == (5, 0, 0), "too new -> newest with warning")
check(probe(FAKE).version == (4, 2, 3), "probe reads --version")

print("1) crash once on frame 5, then complete + preview + report")
st, q = fresh({"FAKE_CRASH_ONCE": "5", "FAKE_FRAME_TIME": "0.05"})
j = q.add(make_blend("shot_a"), js())
run(st, q)
check(j.status == "done", f"status done (got {j.status} {j.status_note})")
check(j.count("done") == 10, f"10 frames done ({j.count('done')})")
check(j.frames["5"]["attempts"] == 2, "frame 5 needed 2 attempts")
check(len(j.attempts) == 2, "2 Blender runs")
check(os.path.exists(j.data_dir / "report.xlsx"), "report.xlsx written")
check(os.path.exists(j.data_dir / "frames.csv"), "frames.csv written")
check(os.path.exists(j.preview_dir / "shot_a.mp4"), "preview mp4 created")
check(sorted(os.listdir(j.frames_dir)) == [f"shot_a_{i:04d}.png" for i in range(1, 11)], "frame files named correctly, no temp files")
check(len(os.listdir(j.crash_dir)) >= 1, "crash log copied")
check(j.scene_info and j.scene_info["engine"] == "CYCLES", "scene info captured")
check(j.frames["1"].get("blender_peak_mb") == 150.0, "Blender peak memory parsed per frame")

print("2) frame 7 always crashes, max_attempts=2 -> skipped")
st, q = fresh({"FAKE_ALWAYS_CRASH": "7", "FAKE_FRAME_TIME": "0.02"})
j = q.add(make_blend("shot_b"), js(max_attempts=2, make_preview=False))
run(st, q)
check(j.status.startswith("done (with"), f"done with errors ({j.status})")
check(j.count("done") == 9 and j.frames["7"]["status"] == "failed", "9 done, frame 7 failed")
check(j.frames["7"]["attempts"] == 2, "frame 7 tried twice")
check(any("fake.crash.txt" in f for f in os.listdir(j.crash_dir)), "Blender crash report collected")

print("3) stop after frame, then resume")
st, q = fresh({"FAKE_FRAME_TIME": "0.15"})
j = q.add(make_blend("shot_c"), js(make_preview=False))
def stopper(r):
    if j.count("done") >= 3 and not r.stopping:
        r.stop_after_frame()
run(st, q, until=stopper)
check(j.status == "stopped", f"stopped ({j.status})")
done_at_stop = j.count("done")
check(3 <= done_at_stop <= 4, f"stopped right after a frame ({done_at_stop} done)")
check(not [f for f in os.listdir(j.frames_dir) if f.startswith(".tmp")], "no temp files left")
# simulate quitting the app: reload queue from disk
q2 = JobQueue(); q2.load()
j2 = q2.jobs[0]
check(j2.count("done") == done_at_stop, "progress survives reload")
run(st, q2)
check(j2.status == "done" and j2.count("done") == 10, f"resumed and finished ({j2.status}, {j2.count('done')})")
check(j2.attempts[-1]["frames_rendered"] == 10 - done_at_stop, "resume rendered only missing frames")

print("4) stop now mid-frame (simulated app quit while rendering)")
st, q = fresh({"FAKE_FRAME_TIME": "0.4"})
j = q.add(make_blend("shot_d"), js(make_preview=False))
def killer(r):
    if j.count("done") >= 2 and r.live.phase == "rendering" and not r.stopping:
        time.sleep(0.15)
        r.stop_now()
run(st, q, until=killer)
check(j.status == "stopped", "stopped")
n_done = j.count("done")
check(all(r.get("attempts", 0) <= 1 for r in j.frames.values()), "killed frame not counted as a failed attempt")
j.status = "rendering"; j.save()   # pretend the app died hard mid render
q2 = JobQueue(); q2.load()
check(q2.jobs[0].status == "stopped", "a job left 'rendering' becomes 'stopped' on launch")
os.environ["FAKE_FRAME_TIME"] = "0.02"
run(st, q2)
check(q2.jobs[0].count("done") == 10, "finished after restart")

print("5) file that cannot load -> job failed, batch continues")
st, q = fresh({"FAKE_FRAME_TIME": "0.02"})
os.environ["FAKE_LOAD_FAIL"] = "1"
jf = q.add(make_blend("broken"), js(max_attempts=2, make_preview=False))
run(st, q)
check(jf.status == "failed" and jf.load_failures == 2, f"failed after 2 starts ({jf.status})")

print("6) no camera -> failed immediately")
st, q = fresh({"FAKE_NO_CAMERA": "1"})
jn = q.add(make_blend("nocam"), js(make_preview=False))
run(st, q)
check(jn.status == "failed" and len(jn.attempts) == 1, f"failed with 1 attempt ({jn.status}, {jn.status_note})")

print("7) video output switched to PNG, overrides applied, cancel moves on")
st, q = fresh({"FAKE_VIDEO": "1", "FAKE_FRAME_TIME": "0.1"})
jv = q.add(make_blend("vid"), js(frame_start=3, frame_end=6, samples=32, make_preview=False))
jw = q.add(make_blend("after"), js(make_preview=False, frame_end=2))
def canceller(r):
    if jv.count("done") >= 1 and r.current is jv:
        r.cancel_current_job()
run(st, q, until=canceller)
check(jv.scene_info["file_extension"] == ".png" and jv.scene_info["format_note"], "video output switched to PNG")
check(jv.scene_info["frame_start"] == 3 and jv.scene_info["samples"] == 32, "frame range + samples override applied")
check(jv.status == "cancelled", f"cancelled ({jv.status})")
check(jw.status == "done", f"next job still rendered ({jw.status})")

print("8) re-adding the same file re-attaches to its folder")
q3 = JobQueue(); q3.load()
q3.remove(q3.jobs[0])
again = q3.add(jv.blend_path, js())
check(again.folder == jv.folder and again.count("done") == jv.count("done"), "kept frames from before")

print("9) Blender 5.x style output: live progress + memory come from the stats handler")
st, q = fresh({"FAKE_NEW_LOG": "1", "FAKE_FRAME_TIME": "0.4", "FAKE_FRAMES": "1-3"})
j = q.add(make_blend("new_log"), js(make_preview=False))
seen = {}
def watch_live(r):
    if r.live.sample:
        seen["sample"] = (r.live.sample, r.live.samples, r.live.remaining_s)
run(st, q, until=watch_live)
check(j.status == "done", "done")
check(seen.get("sample") and seen["sample"][1] == 128 and seen["sample"][2] is not None,
      f"live samples + remaining time seen ({seen.get('sample')})")
check(j.frames["2"].get("blender_peak_mb") == 202.0, f"memory from stats ({j.frames['2'].get('blender_peak_mb')})")

print("10) restart Blender every 4 frames")
st, q = fresh({"FAKE_FRAME_TIME": "0.02"})
j = q.add(make_blend("chunks"), js(make_preview=False, restart_every=4))
run(st, q)
check(j.status == "done" and j.count("done") == 10, f"done ({j.status}, {j.count('done')})")
check(len(j.attempts) == 3 and all(f["attempts"] == 1 for f in j.frames.values()),
      f"3 Blender runs, no frame charged a failure ({[a['reason'] for a in j.attempts]})")

print("11) disk full while saving -> batch stops, no frame charged")
st, q = fresh({"FAKE_FRAME_TIME": "0.02", "FAKE_DISK_FULL": "4"})
j = q.add(make_blend("diskfull"), js(make_preview=False))
j2 = q.add(make_blend("diskfull_next"), js(make_preview=False))
run(st, q)
check(j.status == "stopped" and "disk" in j.status_note, f"stopped: {j.status_note}")
check(j.count("done") == 3 and "4" not in j.frames, "frames 1-3 kept, frame 4 not counted as failed")
check(j2.status == "queued", "batch did not continue onto the next job")

print("12) free-space limit -> batch stops before rendering")
st, q = fresh({"FAKE_FRAME_TIME": "0.02"})
st.min_free_gb = 10 ** 9
j = q.add(make_blend("nospace"), js(make_preview=False))
run(st, q)
check(j.status == "stopped" and not j.attempts, f"stopped without starting Blender ({j.status_note})")

print("13) .blend saved again mid-job -> on hold; accept -> continues")
st, q = fresh({"FAKE_FRAME_TIME": "0.1"})
bp = make_blend("changed")
j = q.add(bp, js(make_preview=False))
jn = q.add(make_blend("other"), js(make_preview=False, frame_end=2))
def stop3(r):
    if j.count("done") >= 3 and not r.stopping:
        r.stop_after_frame()
run(st, q, until=stop3)
time.sleep(0.05)
with open(bp, "ab") as f:
    f.write(b"changed")
run(st, q)
check(j.status == "on hold", f"on hold ({j.status}: {j.status_note})")
check(jn.status == "done", "the next job rendered meanwhile")
q.accept_changed_blend(j)
run(st, q)
check(j.status == "done" and j.count("done") == 10, "continued after accepting")

print("14) missing .blend -> on hold, not failed")
st, q = fresh({})
bp = make_blend("gone")
j = q.add(bp, js(make_preview=False))
os.remove(bp)
run(st, q)
check(j.status == "on hold" and not j.attempts, f"on hold ({j.status_note})")

print("15) render folder moved -> frames still found")
st, q = fresh({"FAKE_FRAME_TIME": "0.02"})
j = q.add(make_blend("mover"), js(make_preview=False, render_root=os.path.join(TMP, "r_old")))
run(st, q)
newroot = os.path.join(TMP, "r_new")
shutil.move(os.path.join(TMP, "r_old"), newroot)
q2 = JobQueue(); w = q2.load()
check(len(q2.jobs) == 0 and w, "moved folder is reported as missing")
jm = q2.add_existing(os.path.join(newroot, "mover"))
check(jm.verify_done_frames() == 0 and jm.count("done") == 10, "all 10 frames found at the new place")
check(os.path.exists(jm.done_records()[0]["path"]), "frame paths resolve to the new place")

print("16) folders: render order, disabled folder skipped")
st, q = fresh({"FAKE_FRAME_TIME": "0.01", "FAKE_FRAMES": "1-2"})
ga = q.add_group("A")
gb = q.add_group("B")
jb = q.add(make_blend("in_b"), js(make_preview=False), group=gb["id"])
ja = q.add(make_blend("in_a"), js(make_preview=False), group=ga["id"])
jt = q.add(make_blend("top"), js(make_preview=False))
check([x.name for x in q.ordered_jobs()] == ["top", "in_a", "in_b"], "top level first, then folders in order")
q.set_order([jt.id], [(gb["id"], [jb.id, ja.id]), (ga["id"], [])])
check([x.name for x in q.ordered_jobs()] == ["top", "in_b", "in_a"] and ja.group == gb["id"], "drag & drop order applied")
gb["enabled"] = False; q.save()
order = []
st2 = st
r = Runner(q, st2, lambda k, p: order.append(q.find(p["job_id"]).name) if k == "frame_done" else None)
r.start(); r.wait(30)
check(jt.status == "done" and jb.status == "queued" and ja.status == "queued", "disabled folder skipped")
q3 = JobQueue(); q3.load()
check([g["name"] for g in q3.groups] == ["B", "A"] and not q3.groups[0]["enabled"], "folders saved")

print("17) only one app instance at a time")
from bbr.storage import InstanceLock
l1, l2 = InstanceLock(), InstanceLock()
check(l1.acquire() and not l2.acquire(), "second instance refused")
l1.release()
check(l2.acquire(), "lock free again after release")
l2.release()

print()
print("FAILURES:" if failures else "ALL PASSED", *failures, sep="\n  ")
shutil.rmtree(TMP, ignore_errors=True) if not failures else print("kept", TMP)
sys.exit(1 if failures else 0)
