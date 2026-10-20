"""Keeps the batch engine's queue in step with the node graph.

Every output node is one job of the engine (a job folder with its job.json, as before):
  * the job's settings are what the output resolves to (graph.resolve), on top of the app's defaults;
  * a connected, unmuted output is enabled, anything else is switched off (its frames are kept);
  * the scheduler's order is the queue's order;
  * a deleted output's job leaves the queue (its files stay on disk).

Jobs that are in the queue but not in the graph - an old queue file, or a job sent through the API
or the Blender add-on - get a job node and an output node of their own ("adopted").
"""
from __future__ import annotations

import os
from dataclasses import asdict
from typing import List, Optional

from ..models import JobSettings, RENDERING, ON_HOLD, CANCELLED, QUEUED, F_DONE
from .graph import Graph, BLENDER, APP, JOB, OUTPUT, BLENDER_FIELDS, APP_FIELDS, default_output_name

# settings that change how frames look: changing one after frames were rendered puts the job on hold
LOOK_KEYS = ("scene", "view_layer", "camera", "samples", "noise_threshold", "denoise", "resolution_pct",
             "res_x", "res_y", "file_format", "color_depth")
# the settings the graph controls (everything else of a job's settings is left alone)
GRAPH_KEYS = tuple(BLENDER_FIELDS) + tuple(APP_FIELDS) + ("scene", "blender", "machines", "devices")


def default_root(blend: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(blend)), "render")


def settings_for(graph: Graph, out_id: str, defaults: Optional[dict] = None) -> Optional[JobSettings]:
    """The JobSettings an output resolves to, or None if it is not connected to a job with a file."""
    r = graph.resolve(out_id)
    job = r["job"]
    if job is None or not job["props"].get("blend"):
        return None
    base = dict(defaults or {})
    s = JobSettings.from_dict(base)
    for k in BLENDER_FIELDS:            # Blender overrides only come from nodes: default = as in the file
        setattr(s, k, None)
    for k, v in r["values"].items():
        setattr(s, k, v)
    s.scene = r["scene"]
    s.blender = job["props"].get("blender") or "auto"
    s.machines = list(job["props"].get("machines") or [])
    s.devices = dict(job["props"].get("devices") or {})
    root = r["output"]["props"].get("render_root") or ""
    s.render_root = os.path.abspath(os.path.expanduser(root)) if root else default_root(job["props"]["blend"])
    return s


class GraphSync:
    """apply() pushes the graph into the queue; adopt() pulls jobs the graph doesn't know yet."""

    def __init__(self, graph: Graph, queue, defaults: Optional[dict] = None):
        self.graph = graph
        self.queue = queue
        self.defaults = defaults or asdict(JobSettings())
        self.messages: List[str] = []

    # ---- graph -> queue --------------------------------------------------------------------
    def apply(self) -> List[str]:
        """Bring the queue in line with the graph. Returns messages worth showing."""
        self.messages = []
        g, q = self.graph, self.queue
        bound = {}
        with q.lock:
            for out in g.outputs():
                jid = self._apply_output(out)
                if jid:
                    bound[jid] = out["id"]
            # outputs that were deleted: their jobs leave the queue (files stay)
            for j in list(q.jobs):
                if j.id in g.known_jobs and j.id not in bound:
                    if j.status == RENDERING:
                        continue                       # removed once it is no longer rendering
                    q.jobs = [x for x in q.jobs if x.id != j.id]
            g.known_jobs = [jid for jid in g.known_jobs if q.find(jid)]
            # render order = the scheduler's order; folders are replaced by node frames
            rank = {jid: i for i, jid in enumerate(self._order_job_ids())}
            q.jobs.sort(key=lambda j: rank.get(j.id, len(rank)))
            for j in q.jobs:
                if j.group:
                    j.group = ""
                    j.save()
            q.groups = []
            q.graph = g.to_dict()
        q.save()
        return self.messages

    def _order_job_ids(self) -> List[str]:
        return [o["props"].get("job_id") for o in self.graph.outputs() if o["props"].get("job_id")]

    def _apply_output(self, out: dict) -> Optional[str]:
        g, q = self.graph, self.queue
        r = g.resolve(out["id"])
        want = settings_for(g, out["id"], self.defaults)
        jid = out["props"].get("job_id") or ""
        job = q.find(jid) if jid else None
        out["props"].pop("problem", None)
        if jid and job is None:
            job = self._reattach(out)
            if job is None:
                out["props"]["problem"] = "job folder not found: " + (out["props"].get("job_folder") or "?")
                return None
        if want is None:
            # not connected to a job (or no file): keep an existing job, switched off
            if job is not None and job.enabled and job.status != RENDERING:
                job.enabled = False
                job.save()
            return job.id if job else None
        blend = os.path.abspath(r["job"]["props"]["blend"])
        if job is not None and (os.path.abspath(job.blend_path) != blend or
                                os.path.abspath(os.path.dirname(job.folder)) != os.path.abspath(want.render_root)):
            if job.status == RENDERING:
                self.messages.append(f"{out['props'].get('name')}: the new file or render folder is used "
                                     "once this job has stopped rendering")
                return job.id
            # another file or render folder: a different job folder. The old one leaves the queue.
            q.jobs = [x for x in q.jobs if x.id != job.id]
            out["props"]["job_id"] = ""
            job = None
        if job is None:
            name = out["props"].get("name") or default_output_name(blend, r["scene"])
            job = q.add(blend, want, name=name)
            out["props"]["job_id"] = job.id
            out["props"]["job_folder"] = job.folder
            if job.id not in g.known_jobs:
                g.known_jobs.append(job.id)
            if job.count(F_DONE):
                self.messages.append(f"{name}: re-attached to its job folder, {job.count(F_DONE)} finished "
                                     "frames are kept")
        else:
            self._update_settings(job, want, out)
        out["props"]["job_folder"] = job.folder
        enabled = bool(r["active"])
        if job.enabled != enabled:
            job.enabled = enabled
            if enabled and job.status == CANCELLED:
                # cancelled from the scheduler (which mutes the output): unmuting continues the job
                job.status, job.status_note = QUEUED, "continuing; finished frames are kept"
            job.save()
        return job.id

    def _reattach(self, out: dict):
        """An output whose job is not in the queue (e.g. a queue file whose render folder was moved
        back): put its job folder back if it is there. Never makes a new job for it."""
        folder = out["props"].get("job_folder") or ""
        if not folder or not os.path.exists(os.path.join(folder, "job.json")):
            return None
        try:
            job = self.queue.add_existing(folder)
        except ValueError:
            return None
        out["props"]["job_id"] = job.id
        if job.id not in self.graph.known_jobs:
            self.graph.known_jobs.append(job.id)
        return job

    def pull_enabled(self) -> None:
        """A job switched on or off elsewhere (the API, the status page): mute or unmute its output,
        so the next apply() keeps the choice."""
        for o in self.graph.of_type(OUTPUT):
            job = self.queue.find(o["props"].get("job_id") or "")
            if job is None:
                continue
            r = self.graph.resolve(o["id"])
            if job.enabled != r["active"]:
                if not job.enabled:
                    o["muted"] = True
                elif o["muted"]:
                    o["muted"] = False

    def prune_removed(self) -> List[str]:
        """Outputs whose job was removed from the queue elsewhere (the API, the list view) are deleted
        too, so the next apply() doesn't bring the job back. Returns the removed output ids."""
        gone = []
        for o in self.graph.of_type(OUTPUT):
            jid = o["props"].get("job_id")
            if jid and jid in self.graph.known_jobs and self.queue.find(jid) is None:
                gone.append(o["id"])
        for oid in gone:
            self.graph.remove_node(oid)
        self.graph.known_jobs = [j for j in self.graph.known_jobs if self.queue.find(j)]
        return gone

    def _update_settings(self, job, want: JobSettings, out: dict) -> None:
        cur = job.settings
        changed = [k for k in GRAPH_KEYS if getattr(cur, k) != getattr(want, k)]
        if not changed:
            return
        if job.status == RENDERING:
            # the engine reads a job's settings when Blender starts; don't change them under it
            self.messages.append(f"{out['props'].get('name')}: changed settings apply after this job")
            return
        look = [k for k in changed if k in LOOK_KEYS]
        for k in changed:
            setattr(cur, k, getattr(want, k))
        if look and job.count(F_DONE) and job.status != ON_HOLD:
            job.status = ON_HOLD
            job.status_note = ("settings that change the image (" + ", ".join(look) + ") were changed after "
                               "frames were rendered – continue with them or start the job over")
        job.save()

    # ---- queue -> graph ----------------------------------------------------------------------
    def adopt(self) -> List[str]:
        """Give every queued job that has no output node a job node and an output node.
        Returns the ids of the new output nodes."""
        g, q = self.graph, self.queue
        bound = {o["props"].get("job_id") for o in g.of_type(OUTPUT)}
        had_outputs = bool(g.of_type(OUTPUT))
        new_outs = []
        with q.lock:
            jobs = q.ordered_jobs()
        for j in jobs:
            if j.id in bound:
                continue
            x, y = g.free_position()
            s = j.settings
            # one job node per .blend (several outputs of the same file share it)
            jn = next((n for n in g.of_type(JOB)
                       if os.path.abspath(n["props"].get("blend") or "") == os.path.abspath(j.blend_path)
                       and (n["props"].get("blender") or "auto") == (s.blender or "auto")
                       and list(n["props"].get("machines") or []) == list(s.machines or [])
                       and dict(n["props"].get("devices") or {}) == dict(s.devices or {})), None)
            if jn is None:
                jn = g.add_node(JOB, x, y + 200, {"blend": j.blend_path, "blender": s.blender or "auto",
                                                  "machines": list(s.machines or []),
                                                  "devices": dict(s.devices or {})})
                if s.scene:
                    jn["props"]["scenes"] = [{"name": s.scene, "active": False}]
            bx, by = jn["x"], jn["y"]
            upper = g.links_from(jn["id"])
            col = bx + 240 * len(upper)
            last, last_sock = jn["id"], g.outputs_of(jn)[0]
            if s.scene and len(g.outputs_of(jn)) > 1:
                last_sock = "scene:" + s.scene
            # Blender overrides and app settings that differ from the defaults: a settings node each,
            # between the job and the output (so they only apply to this output)
            vals = {k: getattr(s, k) for k in BLENDER_FIELDS if getattr(s, k) is not None}
            yy = by + 150
            if vals:
                bn = g.add_node(BLENDER, col, yy, {"values": vals})
                g.connect(last, last_sock, bn["id"])
                last, last_sock, yy = bn["id"], "out", yy + 150
            dflt = JobSettings.from_dict(self.defaults)
            avals = {k: getattr(s, k) for k in APP_FIELDS if getattr(s, k) != getattr(dflt, k)}
            if avals:
                an = g.add_node(APP, col, yy, {"values": avals})
                g.connect(last, last_sock, an["id"])
                last, last_sock, yy = an["id"], "out", yy + 150
            root = os.path.dirname(os.path.abspath(j.folder))
            on = g.add_node(OUTPUT, col, yy, {"name": j.name, "job_id": j.id, "job_folder": j.folder,
                                              "render_root": "" if root == default_root(j.blend_path) else root})
            on["muted"] = not q.is_active(j)
            g.connect(last, last_sock, on["id"])
            g.known_jobs.append(j.id)
            new_outs.append(on["id"])
        # a whole queue adopted at once (an old queue file): keep its order. Single jobs added
        # later (API, Blender add-on) go to the end of the scheduler, like they used to.
        if new_outs and not had_outputs:
            ids = [j.id for j in jobs]
            rank = {}
            for o in g.outputs():
                jid = o["props"].get("job_id")
                rank[o["id"]] = ids.index(jid) if jid in ids else len(ids)
            g.order.sort(key=lambda oid: rank.get(oid, len(ids)))
        return new_outs
