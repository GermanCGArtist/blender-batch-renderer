"""Node graph tests: resolving settings, the scheduler, presets, syncing with the queue, queue files,
and real renders of several outputs (scenes) of one file with the fake Blender.

Run:  python3 tests/test_nodegraph.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="bbr_nodes_")
os.environ["BBR_DATA_DIR"] = os.path.join(TMP, "appdata")
os.environ["FAKE_FRAME_TIME"] = "0.02"

from bbr.installs import probe  # noqa: E402
from bbr.jobqueue import JobQueue  # noqa: E402
from bbr.models import AppSettings, JobSettings, ON_HOLD, DONE, F_DONE  # noqa: E402
from bbr.nodes import presets  # noqa: E402
from bbr.nodes.graph import (Graph, GraphError, BLENDER, APP, JOB, OUTPUT, ACTIVE_SCENE,  # noqa: E402
                             scene_socket)
from bbr.nodes.probe import list_scenes  # noqa: E402
from bbr.nodes.sync import GraphSync, settings_for  # noqa: E402
from bbr.runner import Runner  # noqa: E402

FAKE = os.path.join(HERE, "fake_blender.py")
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def blend(name, sub="src"):
    p = os.path.join(TMP, sub, name + ".blend")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"BLENDER-v402" + b"\0" * 64)
    return p


def raises(fn):
    try:
        fn()
    except GraphError:
        return True
    return False


# ---------------------------------------------------------------------------------------------
print("resolving")
g = Graph()
check(g.scheduler() is not None, "a new graph has its scheduler node")
shot = blend("shot_010")
final = g.add_node(BLENDER, 0, -300, {"values": {"samples": 512, "resolution_pct": 100}})
app = g.add_node(APP, 0, -150, {"values": {"max_attempts": 5}})
j = g.add_node(JOB, 0, 0, {"blend": shot})
pv = g.add_node(BLENDER, -200, 150, {"values": {"samples": 32, "resolution_pct": 50}})
out_pv = g.add_node(OUTPUT, -200, 300, {"name": "Preview"})
out_final = g.add_node(OUTPUT, 200, 300, {"name": "Final"})
g.connect(final["id"], "out", app["id"])
g.connect(app["id"], "out", j["id"])
g.connect(j["id"], ACTIVE_SCENE, pv["id"])
g.connect(pv["id"], "out", out_pv["id"])
g.connect(j["id"], ACTIVE_SCENE, out_final["id"])

r = g.resolve(out_final["id"])
check(r["values"] == {"samples": 512, "resolution_pct": 100, "max_attempts": 5} and r["active"],
      "an output gets every settings node above its job")
r = g.resolve(out_pv["id"])
check(r["values"]["samples"] == 32 and r["values"]["resolution_pct"] == 50 and r["values"]["max_attempts"] == 5,
      "a lower node overrides the same field of a higher one")
check(r["sources"]["samples"] == pv["id"] and r["sources"]["max_attempts"] == app["id"],
      "the node each value comes from is known (for greyed-out file values)")
check(r["chain"] == [final["id"], app["id"], j["id"], pv["id"], out_pv["id"]], "chain runs top to bottom")

g.set_muted(pv["id"], True)
check(g.resolve(out_pv["id"])["values"]["samples"] == 512 and g.resolve(out_pv["id"])["active"],
      "a muted settings node is bypassed")
g.set_muted(pv["id"], False)
g.set_muted(out_pv["id"], True)
check(not g.resolve(out_pv["id"])["active"], "a muted output is not rendered")
g.set_muted(out_pv["id"], False)
g.set_muted(j["id"], True)
check(not g.resolve(out_final["id"])["active"] and not g.resolve(out_pv["id"])["active"],
      "a muted job switches off all its outputs")
g.set_muted(j["id"], False)

lonely = g.add_node(OUTPUT, 400, 300, {"name": "Lonely"})
r = g.resolve(lonely["id"])
check(not r["active"] and r["problems"] == ["not connected to a job"], "an unconnected output is off, with a reason")
g.disconnect(out_final["id"])
check(not g.resolve(out_final["id"])["active"], "disconnecting an output switches it off")
g.connect(j["id"], ACTIVE_SCENE, out_final["id"])

s = settings_for(g, out_pv["id"], {"max_attempts": 3, "retry_delay_s": 7, "samples": 999})
check(s.samples == 32 and s.retry_delay_s == 7 and s.max_attempts == 5 and s.frame_start is None,
      "settings = app defaults, then nodes; Blender fields default to the file")
check(s.render_root == os.path.join(os.path.dirname(shot), "render"), "render folder defaults to next to the .blend")
g.node(out_pv["id"])["props"]["render_root"] = os.path.join(TMP, "custom")
check(settings_for(g, out_pv["id"]).render_root == os.path.join(TMP, "custom"), "output node owns the render folder")
g.node(out_pv["id"])["props"]["render_root"] = ""

# ---------------------------------------------------------------------------------------------
print("connection rules")
j2 = g.add_node(JOB, 600, 0, {"blend": blend("other")})
check(raises(lambda: g.connect(j["id"], ACTIVE_SCENE, j2["id"])), "a job cannot feed a job")
check(raises(lambda: g.connect(pv["id"], "out", j2["id"])), "settings below a job cannot feed another job")
check(raises(lambda: g.connect(out_pv["id"], "out", final["id"])), "an output has no output socket")
check(raises(lambda: g.connect(app["id"], "out", final["id"])), "loops are refused")
g.connect(final["id"], "out", j2["id"])
check(g.resolve(g.add_job_with_output(blend("third"), 900, 0)[1]["id"])["active"],
      "add_job_with_output wires a job to a new output")
check(g.upstream(j2["id"]) == (final["id"], "out") and len(g.links_from(final["id"])) == 2,
      "one settings node can feed several jobs")
g.set_value(final["id"], "samples", "256")
check(g.node(final["id"])["props"]["values"]["samples"] == 256, "values are converted to their type")
g.set_value(final["id"], "samples", None)
check("samples" not in g.node(final["id"])["props"]["values"], "clearing a field falls back to the file")
check(raises(lambda: g.set_value(final["id"], "max_attempts", 3)), "app fields don't go into a Blender node")

# ---------------------------------------------------------------------------------------------
print("scenes")
g2 = Graph()
jj = g2.add_node(JOB, 0, 0, {"blend": shot})
o_a = g2.add_node(OUTPUT, 0, 200, {"name": "A"})
g2.connect(jj["id"], ACTIVE_SCENE, o_a["id"])
g2.set_scenes(jj["id"], [{"name": "Main", "active": True}, {"name": "Closeup", "active": False}])
check(g2.outputs_of(jj) == [scene_socket("Main"), scene_socket("Closeup")], "one socket per scene")
check(g2.link_into(o_a["id"])["from"][1] == scene_socket("Main"), "the active-scene link moves to the active scene")
o_b = g2.add_node(OUTPUT, 200, 200, {"name": "B"})
g2.connect(jj["id"], scene_socket("Closeup"), o_b["id"])
check(g2.resolve(o_b["id"])["scene"] == "Closeup", "an output knows its scene")
g2.set_scenes(jj["id"], [{"name": "Main", "active": True}])
check(g2.link_into(o_b["id"]) is not None and g2.link_into(o_b["id"])["from"][1] == ACTIVE_SCENE
      and g2.resolve(o_b["id"])["scene"] is None, "with one scene left, links fall back to the single socket")

# ---------------------------------------------------------------------------------------------
print("scheduler")
g3 = Graph()
outs = [g3.add_node(OUTPUT, i * 100, 0, {"name": n})["id"] for i, n in enumerate("ABCDE")]
check(g3.order == outs, "outputs join the scheduler in the order they are made")
g3.move_block([outs[3], outs[2]], -1)
check([g3.node(o)["props"]["name"] for o in g3.order] == list("ACDBE"), "push up moves the selection as a block")
g3.move_block([outs[2], outs[3]], -5)
check([g3.node(o)["props"]["name"] for o in g3.order] == list("CDABE"), "pushing past the top stops at the top")
g3.move_block([outs[4]], 3)
check([g3.node(o)["props"]["name"] for o in g3.order][-1] == "E", "pushing past the end stops at the end")
g3.remove_node(outs[0])
check(outs[0] not in g3.order and len(g3.order) == 4, "deleted outputs leave the scheduler")
check(len({g3.node(o)["props"]["color"] for o in g3.order}) == 4, "outputs get different colours")

# ---------------------------------------------------------------------------------------------
print("presets")
pre = presets.extract(g, [final["id"], app["id"], j["id"]], name="Final quality")
check(len(pre["nodes"]) == 2 and pre["links"] == [{"from": final["id"], "to": app["id"]}],
      "a preset holds the selected settings nodes and their links, not jobs")
pp = os.path.join(TMP, "final.bbrpreset")
presets.save(pp, pre)
g4 = Graph()
ids, mapping = presets.insert(g4, presets.load(pp), 50, 60)
check(len(ids) == 2 and g4.link_into(mapping[app["id"]])["from"][0] == mapping[final["id"]],
      "a preset loads into another graph with its links")
check(g4.node(mapping[final["id"]])["x"] == 50 and g4.node(mapping[app["id"]])["y"] == 60 + 150,
      "a preset keeps its layout")
check(raises(lambda: presets.extract(g, [j["id"]])), "a preset needs settings nodes")
bad = os.path.join(TMP, "bad.bbrpreset")
open(bad, "w").write("{}")
check(raises(lambda: presets.load(bad)), "other files are refused as presets")

# ---------------------------------------------------------------------------------------------
print("syncing with the queue")
st = AppSettings.load()
fake = probe(FAKE)
st.installs = [fake.to_dict()]
st.keep_awake = False
st.defaults["make_preview"] = False
st.defaults["retry_delay_s"] = 0
q = JobQueue()
q.load()

g = Graph()
film = blend("film")
os.environ["FAKE_FRAMES"] = "1-4"
os.environ["FAKE_SCENES"] = "Closeup:5-6"
info = list_scenes(fake.executable, film)
check(info and [s["name"] for s in info["scenes"]] == ["Scene", "Closeup"] and info["scenes"][0]["active"],
      "Blender lists the file's scenes")
jn = g.add_node(JOB, 0, 0, {"blend": film})
g.set_scenes(jn["id"], info["scenes"])
preset = g.add_node(BLENDER, 0, -150, {"values": {"samples": 16}})
g.connect(preset["id"], "out", jn["id"])
o_main = g.add_node(OUTPUT, 0, 200, {"name": "film_main"})
g.connect(jn["id"], scene_socket("Scene"), o_main["id"])
half = g.add_node(BLENDER, 200, 150, {"values": {"resolution_pct": 50}})
g.connect(jn["id"], scene_socket("Closeup"), half["id"])
o_close = g.add_node(OUTPUT, 200, 300, {"name": "film_closeup"})
g.connect(half["id"], "out", o_close["id"])
off = g.add_node(OUTPUT, 400, 300, {"name": "film_off"})

sync = GraphSync(g, q, st.defaults)
sync.apply()
jm = q.find(g.node(o_main["id"])["props"]["job_id"])
jc = q.find(g.node(o_close["id"])["props"]["job_id"])
check(jm and jc and len(q.jobs) == 2, "each connected output becomes a job; an unconnected one doesn't")
check(jm.name == "film_main" and jc.folder.endswith(os.path.join("render", "film_closeup")),
      "job folders are named after their output nodes")
check(jc.settings.scene == "Closeup" and jc.settings.resolution_pct == 50 and jc.settings.samples == 16
      and jm.settings.scene == "Scene" and jm.settings.resolution_pct is None,
      "each job gets its output's resolved settings")
check([x.id for x in q.ordered_jobs()] == [jm.id, jc.id], "queue order = scheduler order")
g.move_block([o_close["id"]], -1)
sync.apply()
check([x.id for x in q.ordered_jobs()] == [jc.id, jm.id], "pushing an output up moves its job up")
g.set_muted(o_main["id"], True)
sync.apply()
check(not jm.enabled and jc.enabled, "muting an output switches its job off")
g.set_muted(o_main["id"], False)
g.disconnect(o_main["id"])
sync.apply()
check(q.find(jm.id) and not jm.enabled, "disconnecting switches the job off and keeps it")
g.connect(jn["id"], scene_socket("Scene"), o_main["id"])
sync.apply()
check(jm.enabled and q.find(jm.id), "reconnecting switches it on again")

print("rendering two scenes of one file")
r = Runner(q, st, lambda k, p: None)
check(r.start(), "batch starts")
r.wait(120)
check(jc.status == DONE and jc.total_frames == 2 and jc.count(F_DONE) == 2, "the Closeup output rendered frames 5-6")
check(jm.status == DONE and jm.count(F_DONE) == 4, "the main output rendered frames 1-4")
check((jc.scene_info or {}).get("scene") == "Closeup" and (jc.scene_info or {}).get("res_pct") == 50,
      "Blender rendered the Closeup scene at 50 %")
log = open(jc.attempts[-1]["log"]).read()
check("FAKE_RENDER scene=Closeup" in log, "the render call names the scene")
check(sorted(os.listdir(jc.frames_dir)) == ["film_closeup_0005.png", "film_closeup_0006.png"],
      "frames are named after the output")

print("changing settings after rendering")
g.set_value(half["id"], "resolution_pct", 75)
sync.apply()
check(jc.status == ON_HOLD and "resolution_pct" in jc.status_note and jc.settings.resolution_pct == 75,
      "changing the image settings of a rendered job puts it on hold")
g.set_value(preset["id"], "frame_start", None)
app2 = g.add_node(APP, 0, 400, {"values": {"max_attempts": 7}})
g.disconnect(o_main["id"])
g.connect(jn["id"], scene_socket("Scene"), app2["id"])
g.connect(app2["id"], "out", o_main["id"])
sync.apply()
check(jm.status == DONE and jm.settings.max_attempts == 7, "app behaviour changes don't put a job on hold")

print("cancel and switches from elsewhere")
jc.status = "cancelled"
jc.save()
g.set_muted(o_close["id"], True)
sync.apply()
g.set_muted(o_close["id"], False)
sync.apply()
check(jc.status == "queued" and jc.enabled, "unmuting a cancelled output continues its job")
q.set_enabled(jc, False)                 # e.g. POST /api/v1/jobs/<id>/enabled
sync.pull_enabled()
sync.apply()
check(g.node(o_close["id"])["muted"] and not jc.enabled, "switching a job off through the API mutes its output")
q.set_enabled(jc, True)
sync.pull_enabled()
sync.apply()
check(not g.node(o_close["id"])["muted"] and jc.enabled, "...and switching it on unmutes it")
check(q.graph and q.graph["order"] == g.order, "apply() stores the graph in the queue")

print("deleting and adopting")
g.remove_node(o_main["id"])
sync.apply()
check(q.find(jm.id) is None and os.path.isdir(jm.folder), "deleting an output removes its job; files stay")
old = q.add(blend("api_job"), JobSettings(samples=8, max_attempts=9, render_root=os.path.join(TMP, "apiout")))
new = sync.adopt()
check(len(new) == 1 and g.node(new[0])["props"]["job_id"] == old.id, "a job added from outside gets an output node")
r2 = g.resolve(new[0])
check(r2["active"] and r2["values"].get("samples") == 8 and r2["values"].get("max_attempts") == 9,
      "its non-default settings become settings nodes")
check(settings_for(g, new[0], st.defaults).render_root == os.path.join(TMP, "apiout"),
      "its render folder is kept")
check(g.order[-1] == new[0], "it joins the end of the scheduler")
sync.apply()
check(q.find(old.id) and q.find(old.id).settings.samples == 8 and len(q.jobs) == 2, "adopting changes nothing")

print("removed elsewhere, missing folders")
api_out = new[0]
q.remove(q.find(old.id))
check(sync.prune_removed() == [api_out] and api_out not in g.nodes, "a job removed through the API loses its output")
sync.apply()
check(q.find(old.id) is None, "...and is not brought back")
o_close_job = q.find(jc.id)
q.remove(o_close_job)
sync.apply()
check(q.find(jc.id) is not None and q.find(jc.id).count(F_DONE) == 2,
      "an output whose job left the queue re-attaches to its job folder")
ghost = g.add_node(OUTPUT, 900, 900, {"name": "ghost", "job_id": "nope", "job_folder": os.path.join(TMP, "gone")})
g.connect(jn["id"], scene_socket("Scene"), ghost["id"])
n_before = len(q.jobs)
sync.apply()
check(len(q.jobs) == n_before and "not found" in ghost["props"].get("problem", ""),
      "a missing job folder is reported, not silently replaced by a new job")
g.remove_node(ghost["id"])
sync.apply()

print("queue files")
q.graph = g.to_dict()
q.save()
qf = os.path.join(TMP, "proj", "film.bbrqueue")
os.makedirs(os.path.dirname(qf), exist_ok=True)
q.save_file(qf)
data = json.load(open(qf))
check(data["graph"]["nodes"][jn["id"]]["props"]["blend_rel"] == os.path.relpath(film, os.path.dirname(qf)),
      "queue files store the graph's paths relative to the file too")
snap = JobQueue.read_file_snapshot(qf)
check(Graph(snap["graph"]).to_dict()["nodes"] == g.to_dict()["nodes"], "the graph survives a queue file")
q2 = JobQueue(os.path.join(TMP, "q2.json"))
q2.load_file(qf)
check(q2.graph and set(q2.graph["nodes"]) == set(g.nodes) and len(q2.jobs) == len(q.jobs), "opening a queue file restores the graph")
q3 = JobQueue()
q3.load()
check(q3.graph and q3.graph["order"] == g.order, "queue.json keeps the graph between runs")

print("old queue files")
q4 = JobQueue(os.path.join(TMP, "q4.json"))
st2 = JobSettings(render_root=os.path.join(TMP, "oldroot"))
a = q4.add(blend("old_a"), st2)
b = q4.add(blend("old_b"), JobSettings(frame_start=3, frame_end=9))
q4.set_enabled(b, False)
g5 = Graph()
GraphSync(g5, q4).adopt()
check([g5.node(o)["props"]["job_id"] for o in g5.order] == [a.id, b.id], "an old queue keeps its order")
check(g5.node(g5.order[1])["muted"], "an unticked job becomes a muted output")
GraphSync(g5, q4).apply()
check(q4.find(a.id).enabled and not q4.find(b.id).enabled and q4.find(b.id).settings.frame_start == 3,
      "converting an old queue changes no job")

shutil.rmtree(TMP, ignore_errors=True)
print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED:\n  " + "\n  ".join(failures))
sys.exit(1 if failures else 0)
