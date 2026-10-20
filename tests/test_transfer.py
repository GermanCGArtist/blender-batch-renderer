"""Smart transfer of job files to workers, checksums, fallback to Full copy, version check,
consistency check and the 'is it worth it' rule.  Run:  python3 tests/test_transfer.py"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import check, wait_for, fake_exe, make_blend, Coordinator, make_worker, TMP  # noqa: E402

import bbr.hashing as hashing  # noqa: E402
import bbr.worker as worker_mod  # noqa: E402
import bbr.workerapi as wapi  # noqa: E402
from bbr.package import build_package, load_manifest  # noqa: E402

# ---- a project: one texture, an image sequence of 20 passes ------------------------------------
proj = os.path.join(TMP, "proj")
os.makedirs(os.path.join(proj, "tex"), exist_ok=True)
os.makedirs(os.path.join(proj, "passes"), exist_ok=True)
tex = os.path.join(proj, "tex", "wood.png")
with open(tex, "wb") as f:
    f.write(os.urandom(64 * 1024))
for i in range(1, 21):
    with open(os.path.join(proj, "passes", f"pass_{i:04d}.exr"), "wb") as f:
        f.write(os.urandom(50 * 1024))
SEQ = os.path.join(proj, "passes", "pass_0001.exr")
ENV = dict(FAKE_IMAGES=tex + ";seq:" + SEQ, FAKE_SEQ_USER="1,0,0,0")
coord_inst = fake_exe("x_coord", FAKE_FRAME_TIME="0.05", **ENV)
pc_inst = fake_exe("x_pc", FAKE_FRAME_TIME="0.1", **ENV)

requests = []
_orig_send_file = wapi.WorkerEndpoints.send_file


def logged_send_file(self, h, mid, job_id, version, path):
    requests.append(path)
    return _orig_send_file(self, h, mid, job_id, version, path)


wapi.WorkerEndpoints.send_file = logged_send_file

# -------------------------------------------------------------------------------------------
print("1) the job copy: scene, assets, frame files with their frame mapping")
c = Coordinator(coord_inst)
job = c.add(make_blend("comp", "proj"))
job.ensure_folders()
man = build_package(job, coord_inst, lambda l: None)
job.package = man
M = load_manifest(job)
kinds = {}
for p, e in M["files"].items():
    kinds.setdefault(e["kind"], []).append(p)
check(len(kinds.get("scene", [])) == 1 and kinds["scene"][0] == "main.blend", "scene: main.blend")
check(len(kinds.get("asset", [])) == 1 and kinds["asset"][0].endswith("/wood.png"), "asset: the texture")
check(len(kinds.get("frame", [])) == 10, f"frame files: only the 10 frames the job renders ({len(kinds.get('frame', []))})")
check(M["frames"]["5"] == [p for p in kinds["frame"] if p.endswith("pass_0005.exr")], "frame 5 needs pass_0005")
check(40 * 1024 < man["stream_bytes_per_frame"] < 60 * 1024, "streamed bytes per frame estimated")
check(man["size_upfront"] < 200 * 1024, "only the scene and the texture before the first frame")
main = json.loads((job.package_dir / f"v_{man['hash']}" / "main.blend").read_text())
check(all(fp.startswith("//a/") or fp.startswith("//s/") for fp, _p, _s in main["images"]),
      "paths in the copy point into the layout")
c.close()

# -------------------------------------------------------------------------------------------
print("2) Smart transfer: texture once, one pass file per frame, reused by the next job")
c = Coordinator(coord_inst)
job = c.add(make_blend("comp2", "proj"))
w = make_worker(c, "PC", pc_inst)
w.start()
wait_for(lambda: w.registered, 20)
requests.clear()
c.r.start()
check(wait_for(lambda: job.status == "done" and not c.r.running, 90), f"job done ({job.status} {job.status_note})")
check(all(r.get("machine") == "PC" for r in job.frames.values()), "rendered by the PC")
tex_req = [r for r in requests if r.endswith("wood.png")]
pass_req = [r for r in requests if "pass_" in r]
check(len(tex_req) == 1, f"texture sent once ({len(tex_req)})")
check(sorted(pass_req) == sorted(set(pass_req)) and len(pass_req) == 10, f"each pass sent once ({len(pass_req)})")
ws_files = [p for p in (w.cache_dir / "jobs").rglob("pass_*")]
check(not ws_files, f"frame files deleted after their frame ({len(ws_files)} left)")
check(any((w.cache_dir / "store").rglob("*")), "texture kept in the worker's store")
check(all(r.get("fetch_s") is not None for r in job.frames.values()), "fetch time recorded per frame")
# a second job with the same texture
requests.clear()
job2 = c.add(make_blend("comp3", "proj"))
c.r.start()
check(wait_for(lambda: job2.status == "done" and not c.r.running, 90), "second job done")
check(not [r for r in requests if r.endswith("wood.png")], "texture not sent again (same checksum)")

# -------------------------------------------------------------------------------------------
print("3) damaged on the way: detected by checksum and sent again (both directions)")
real_get = hashing.HashCache.get
bad = {"n": 0}


def flaky_get(self, path):
    d = real_get(self, path)
    if path.endswith("pass_0003.exr") and bad["n"] == 0:
        bad["n"] += 1
        return "0" * 64
    return d


hashing.HashCache.get = flaky_get
real_sha = worker_mod._sha256
upl = {"n": 0}


def flaky_sha(path):
    d = real_sha(path)
    if path.endswith("_0004.png") and upl["n"] == 0:
        upl["n"] += 1
        return "f" * 64
    return d


worker_mod._sha256 = flaky_sha
job3 = c.add(make_blend("comp4", "proj"))
c.r.start()
check(wait_for(lambda: job3.status == "done" and not c.r.running, 90), f"job done ({job3.status})")
cons = "\n".join(t for _s, t in w.console_tail)
check(bad["n"] == 1 and "arrived damaged" in cons, "damaged download noticed and fetched again")
check(upl["n"] == 1 and "arrived damaged at the coordinator" in cons, "damaged upload noticed and sent again")
check(job3.count("done") == 10, "all frames delivered")
hashing.HashCache.get = real_get
worker_mod._sha256 = real_sha

# -------------------------------------------------------------------------------------------
print("4) a file missing with Smart transfer: Blender notices, the worker switches to Full copy")
real_ensure = worker_mod.Worker._ensure_file
skipped = {"n": 0}


def gappy_ensure(self, lease, ws, p, e, store, quiet=False):
    if p.endswith("wood.png") and skipped["n"] == 0:
        skipped["n"] += 1
        return                       # "forgot" the texture
    return real_ensure(self, lease, ws, p, e, store, quiet)


worker_mod.Worker._ensure_file = gappy_ensure
shutil = __import__("shutil")
shutil.rmtree(w.cache_dir / "store", ignore_errors=True)
shutil.rmtree(w.cache_dir / "jobs", ignore_errors=True)
job4 = c.add(make_blend("comp5", "proj"))
requests.clear()
c.r.start()
check(wait_for(lambda: job4.status == "done" and not c.r.running, 90), f"job done ({job4.status})")
check(skipped["n"] == 1, "the texture was left out once")
check(any("switching to Full copy" in t for t in c.messages()), "coordinator told about the switch")
check(len([r for r in requests if "pass_" in r]) == 10, "Full copy fetched every pass file")
check(job4.count("failed") == 0 and not any(r.get("failed_on") for r in job4.frames.values()),
      "no frame counted as failed")
worker_mod.Worker._ensure_file = real_ensure
w.stop()
c.close()

# -------------------------------------------------------------------------------------------
print("5) different app versions are refused with a clear message")
c = Coordinator(coord_inst)
req = urllib.request.Request(c.url + "/api/v1/worker/hello", data=b"{}", method="POST",
                             headers={"X-BBR-Machine": "wold001", "X-BBR-Worker-Key": "x"})
try:
    urllib.request.urlopen(req, timeout=5)
    check(False, "old worker refused")
except urllib.error.HTTPError as e:
    body = json.loads(e.read())
    check(e.code == 426 and "update" in body["error"], f"old worker refused ({e.code}: {body.get('error')})")
c.close()

# -------------------------------------------------------------------------------------------
print("6) consistency check: a matching computer joins, a different one is left out")
c = Coordinator(fake_exe("x_coord_slow", FAKE_FRAME_TIME="0.6", FAKE_FRAMES="1-20", **ENV), render_locally=True)
jobA = c.add(make_blend("consA", "proj"), consistency_check=True)
same = make_worker(c, "Same", fake_exe("x_same", FAKE_FRAME_TIME="0.1", **ENV))
same.start()
wait_for(lambda: same.registered, 20)
c.r.start()
check(wait_for(lambda: jobA.status == "done" and not c.r.running, 120), f"job done ({jobA.status})")
wait_for(lambda: "Same" in (jobA.approvals or {}), 20)
ap = (jobA.approvals or {}).get("Same") or {}
check(ap.get("ok") is True and ap.get("diff") is not None, f"test frame compared and approved ({ap})")
check(any(r.get("machine") == "Same" for r in jobA.frames.values()), "the matching computer rendered frames")
check(list((jobA.data_dir / "consistency").glob("Same_*.png")), "test frame kept in data/consistency")
same.stop()
jobB = c.add(make_blend("consB", "proj"), consistency_check=True)
odd = make_worker(c, "Odd", fake_exe("x_odd", FAKE_FRAME_TIME="0.1", FAKE_PNG_COLOR="255,0,0", **ENV))
odd.start()
wait_for(lambda: odd.registered, 20)
c.r.start()
check(wait_for(lambda: jobB.status == "done" and not c.r.running, 120), f"job done ({jobB.status})")
wait_for(lambda: "Odd" in (jobB.approvals or {}), 20)
ap = (jobB.approvals or {}).get("Odd") or {}
check(ap.get("ok") is False and "differs" in (ap.get("reason") or ""), f"different frame noticed ({ap.get('reason')})")
check(not any(r.get("machine") == "Odd" for r in jobB.frames.values()), "the different computer delivered nothing")
check(any("left out" in t for t in c.messages()), "message about it")
odd.stop()
c.close()

# -------------------------------------------------------------------------------------------
print("7) 'is it worth it': a slow computer gets no frame the others finish sooner")
from bbr.scheduler import Machine, Scheduler, Lease  # noqa: E402
c = Coordinator(coord_inst)
j = c.add(make_blend("tail", "proj"), frame_start=1, frame_end=100)
j.scene_info = {"frame_start": 1, "frame_end": 100, "frame_step": 1}
for i in range(1, 91):
    j.frames[str(i)] = {"status": "done", "render_s": 1.0 if i <= 80 else 60.0,
                        "machine": c.r.scheduler.local.name if i <= 80 else "Slow"}
S = c.r.scheduler
S.local_busy_job = j.id
slow = S.register({"id": "wslow01", "name": "Slow"})
check(S._others_finish_first(j, slow, 5) is True, "5 frames left: this computer is faster alone")
check(S._others_finish_first(j, slow, 500) is False, "500 frames left: the slow one still helps")
c.close()

# -------------------------------------------------------------------------------------------
print("8) checksums are remembered until a file changes")
hc = hashing.HashCache(os.path.join(TMP, "hc.json"))
reads = {"n": 0}
real_file = hashing.sha256_file


def counting(path):
    reads["n"] += 1
    return real_file(path)


hashing.sha256_file = counting
a1 = hc.get(tex)
a2 = hc.get(tex)
check(a1 == a2 and reads["n"] == 1, "second request uses the remembered checksum")
time.sleep(0.01)
with open(tex, "ab") as f:
    f.write(b"x")
a3 = hc.get(tex)
check(a3 != a1 and reads["n"] == 2, "changed file is read again")
hashing.sha256_file = real_file

shutil.rmtree(TMP, ignore_errors=True)
print()
if H.failures:
    print(f"{len(H.failures)} FAILED:")
    for f in H.failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
