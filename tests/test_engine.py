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

print()
print("FAILURES:" if failures else "ALL PASSED", *failures, sep="\n  ")
shutil.rmtree(TMP, ignore_errors=True) if not failures else print("kept", TMP)
sys.exit(1 if failures else 0)
