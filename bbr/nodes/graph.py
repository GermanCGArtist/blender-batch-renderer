"""The node graph behind the node editor: what gets rendered, how, and in which order.

The graph flows top to bottom:

    settings nodes (Blender overrides / app behaviour)      - optional, can be chained
            |
          job  (a .blend file; one output socket per scene)
            |
    settings nodes                                          - optional, only for this output
            |
          output (name, render folder, colour)              - one per render

A scheduler node lists every output in render order. Each output becomes one job of the
batch engine (see sync.py): the engine itself is unchanged.

Rules
  * Anything no node sets comes from the .blend as saved (or from the app's defaults).
  * Settings stack like a modifier stack: a lower node overrides the same field of a higher one.
  * A muted settings node is bypassed (like a muted node in Blender); a muted job or output,
    or an output that is not connected to a job, is not rendered.

The graph is plain data (dicts and lists) so it can live inside queue.json and .bbrqueue files.
"""
from __future__ import annotations

import copy
import os
import uuid
from typing import Dict, List, Optional, Tuple

GRAPH_VERSION = 1

# ---- node types --------------------------------------------------------------------------
BLENDER = "blender"      # Blender overrides
APP = "app"              # app behaviour
JOB = "job"
OUTPUT = "output"
SCHEDULER = "scheduler"
SETTINGS_TYPES = (BLENDER, APP)
NODE_TYPES = (BLENDER, APP, JOB, OUTPUT, SCHEDULER)

TITLES = {BLENDER: "Blender overrides", APP: "App behaviour", JOB: "Job", OUTPUT: "Output",
          SCHEDULER: "Scheduler"}

# field: (label, kind, choices)   kind = int | float | str | bool | bool3 | choice
BLENDER_FIELDS: Dict[str, tuple] = {
    "frame_start": ("Start frame", "int", None),
    "frame_end": ("End frame", "int", None),
    "frame_step": ("Frame step", "int", None),
    "samples": ("Samples", "int", None),
    "noise_threshold": ("Noise threshold", "float", None),
    "denoise": ("Denoise", "bool3", None),
    "resolution_pct": ("Resolution %", "int", None),
    "res_x": ("Width (px)", "int", None),
    "res_y": ("Height (px)", "int", None),
    "camera": ("Camera", "str", None),
    "view_layer": ("View layer", "str", None),
    "file_format": ("Format", "choice", ["PNG", "OPEN_EXR", "OPEN_EXR_MULTILAYER", "JPEG", "TIFF"]),
    "color_depth": ("Bit depth", "choice", ["8", "16", "32"]),
}
APP_FIELDS: Dict[str, tuple] = {
    "max_attempts": ("Attempts per frame", "int", None),
    "retry_delay_s": ("Wait before retry (s)", "int", None),
    "frame_timeout_min": ("Frame timeout (min)", "float", None),
    "restart_every": ("Restart Blender every N frames", "int", None),
    "make_preview": ("Preview video", "bool", None),
    "preflight": ("Check the file first", "bool", None),
    "consistency_check": ("Consistency check", "bool", None),
    "ignore_addons": ("Ignore missing add-ons", "bool", None),
}
FIELDS = {BLENDER: BLENDER_FIELDS, APP: APP_FIELDS}

# colours for new output nodes (picked in turn); the user can change each one
PALETTE = ["#e8743b", "#19a979", "#5899da", "#e66c9c", "#c9b200", "#9b6bd6", "#13a4b4", "#d64545"]

ACTIVE_SCENE = "active"            # job socket: the scene that is active in the file


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def scene_socket(name: str) -> str:
    return "scene:" + name


def socket_scene(sock: str) -> Optional[str]:
    """Scene name of a job output socket, None for the file's active scene."""
    return sock[6:] if sock.startswith("scene:") else None


class GraphError(ValueError):
    pass


class Graph:
    """Wraps the graph dict: {"version", "nodes": {id: node}, "links": [...], "frames": {id: frame},
    "order": [output ids], "known_jobs": [job ids]}."""

    def __init__(self, data: Optional[dict] = None):
        d = copy.deepcopy(data) if data else {}
        self.nodes: Dict[str, dict] = d.get("nodes") or {}
        self.links: List[dict] = d.get("links") or []
        self.frames: Dict[str, dict] = d.get("frames") or {}
        self.order: List[str] = d.get("order") or []
        self.known_jobs: List[str] = d.get("known_jobs") or []   # job ids the graph created or adopted
        self.view: dict = d.get("view") or {}
        for n in self.nodes.values():
            n.setdefault("muted", False)
            n.setdefault("props", {})
            n.setdefault("frame", "")
            if n["type"] in SETTINGS_TYPES:
                n["props"].setdefault("values", {})
        self.ensure_scheduler()
        self._clean_order()

    # ---- persistence -------------------------------------------------------------------
    def to_dict(self) -> dict:
        return copy.deepcopy({"version": GRAPH_VERSION, "nodes": self.nodes, "links": self.links,
                              "frames": self.frames, "order": self.order, "known_jobs": self.known_jobs,
                              "view": self.view})

    # ---- nodes -------------------------------------------------------------------------
    def add_node(self, ntype: str, x: float = 0, y: float = 0, props: Optional[dict] = None,
                 nid: Optional[str] = None) -> dict:
        if ntype not in NODE_TYPES:
            raise GraphError(f"unknown node type {ntype}")
        if ntype == SCHEDULER and self.scheduler():
            return self.scheduler()
        n = {"id": nid or new_id(), "type": ntype, "x": float(x), "y": float(y), "muted": False,
             "frame": "", "props": copy.deepcopy(props) if props else {}}
        if ntype in SETTINGS_TYPES:
            n["props"].setdefault("values", {})
        if ntype == JOB:
            n["props"].setdefault("blend", "")
            n["props"].setdefault("blender", "auto")
            n["props"].setdefault("scenes", [])
            n["props"].setdefault("machines", [])
            n["props"].setdefault("devices", {})
        if ntype == OUTPUT:
            n["props"].setdefault("name", "")
            n["props"].setdefault("render_root", "")
            n["props"].setdefault("color", self._next_color())
            n["props"].setdefault("job_id", "")
        self.nodes[n["id"]] = n
        if ntype == OUTPUT and n["id"] not in self.order:
            self.order.append(n["id"])
        return n

    def remove_node(self, nid: str) -> None:
        n = self.nodes.get(nid)
        if not n or n["type"] == SCHEDULER:
            return
        del self.nodes[nid]
        self.links = [l for l in self.links if l["from"][0] != nid and l["to"][0] != nid]
        self.order = [o for o in self.order if o != nid]

    def node(self, nid: str) -> Optional[dict]:
        return self.nodes.get(nid)

    def of_type(self, ntype: str) -> List[dict]:
        return [n for n in self.nodes.values() if n["type"] == ntype]

    def outputs(self) -> List[dict]:
        """Output nodes in render order."""
        self._clean_order()
        return [self.nodes[o] for o in self.order]

    def scheduler(self) -> Optional[dict]:
        return next((n for n in self.nodes.values() if n["type"] == SCHEDULER), None)

    def ensure_scheduler(self) -> dict:
        s = self.scheduler()
        if s:
            return s
        s = {"id": new_id(), "type": SCHEDULER, "x": 720.0, "y": 0.0, "muted": False, "frame": "", "props": {}}
        self.nodes[s["id"]] = s
        return s

    def set_muted(self, nid: str, muted: bool) -> None:
        n = self.nodes.get(nid)
        if n and n["type"] != SCHEDULER:
            n["muted"] = bool(muted)

    def set_value(self, nid: str, key: str, value) -> None:
        """Set (or with value None, clear) one field of a settings node."""
        n = self.nodes[nid]
        if n["type"] not in SETTINGS_TYPES or key not in FIELDS[n["type"]]:
            raise GraphError(f"{key} is not a field of {TITLES.get(n['type'], n['type'])}")
        vals = n["props"].setdefault("values", {})
        if value is None or value == "":
            vals.pop(key, None)
        else:
            vals[key] = coerce(FIELDS[n["type"]][key][1], value)

    def _next_color(self) -> str:
        used = [n["props"].get("color") for n in self.of_type(OUTPUT)]
        for c in PALETTE:
            if c not in used:
                return c
        return PALETTE[len(used) % len(PALETTE)]

    def _clean_order(self) -> None:
        outs = [nid for nid, n in self.nodes.items() if n["type"] == OUTPUT]
        seen = set()
        order = []
        for o in self.order:
            if o in self.nodes and self.nodes[o]["type"] == OUTPUT and o not in seen:
                order.append(o)
                seen.add(o)
        order += [o for o in outs if o not in seen]
        self.order = order

    # ---- sockets -----------------------------------------------------------------------
    def inputs_of(self, n: dict) -> List[str]:
        return [] if n["type"] == SCHEDULER else ["in"]

    def outputs_of(self, n: dict) -> List[str]:
        t = n["type"]
        if t in SETTINGS_TYPES:
            return ["out"]
        if t == JOB:
            names = [s["name"] for s in n["props"].get("scenes") or []]
            if len(names) <= 1:
                return [ACTIVE_SCENE]
            return [scene_socket(nm) for nm in names]
        return []

    def socket_label(self, n: dict, sock: str) -> str:
        if n["type"] == JOB:
            sc = socket_scene(sock)
            return sc if sc else "Scene"
        return ""

    # ---- links -------------------------------------------------------------------------
    def link_into(self, nid: str, sock: str = "in") -> Optional[dict]:
        return next((l for l in self.links if l["to"][0] == nid and l["to"][1] == sock), None)

    def links_from(self, nid: str) -> List[dict]:
        return [l for l in self.links if l["from"][0] == nid]

    def upstream(self, nid: str) -> Optional[Tuple[str, str]]:
        l = self.link_into(nid)
        return (l["from"][0], l["from"][1]) if l else None

    def _chain_up(self, nid: str) -> List[str]:
        """Node ids above `nid` (nearest first), following the single input of each node."""
        out, seen, cur = [], {nid}, self.upstream(nid)
        while cur and cur[0] not in seen:
            out.append(cur[0])
            seen.add(cur[0])
            cur = self.upstream(cur[0])
        return out

    def _reaches_down(self, nid: str, target_type: str, seen=None) -> bool:
        seen = seen if seen is not None else set()
        if nid in seen:
            return False
        seen.add(nid)
        for l in self.links_from(nid):
            t = self.nodes[l["to"][0]]
            if t["type"] == target_type or self._reaches_down(t["id"], target_type, seen):
                return True
        return False

    def can_connect(self, src: str, ssock: str, dst: str, dsock: str = "in") -> Optional[str]:
        """None if the link is allowed, else the reason it is not."""
        a, b = self.nodes.get(src), self.nodes.get(dst)
        if not a or not b:
            return "node missing"
        if src == dst:
            return "a node cannot feed itself"
        if ssock not in self.outputs_of(a):
            return "no such output socket"
        if dsock not in self.inputs_of(b):
            return "no such input socket"
        if a["type"] == OUTPUT or b["type"] == SCHEDULER:
            return "nothing connects there"
        if b["type"] == JOB and a["type"] == JOB:
            return "a job cannot feed another job"
        up = self._chain_up(src)
        if dst in up:
            return "that would make a loop"
        job_above = a["type"] == JOB or any(self.nodes[u]["type"] == JOB for u in up)
        if b["type"] == JOB and job_above:
            return "settings below a job only apply to its outputs; they cannot feed another job"
        if b["type"] in SETTINGS_TYPES and job_above and self._reaches_down(dst, JOB):
            return "this settings node already feeds a job"
        # settings straight into an output with no job above are allowed; resolve() reports it
        return None

    def connect(self, src: str, ssock: str, dst: str, dsock: str = "in") -> dict:
        why = self.can_connect(src, ssock, dst, dsock)
        if why:
            raise GraphError(why)
        self.links = [l for l in self.links if not (l["to"][0] == dst and l["to"][1] == dsock)]
        l = {"from": [src, ssock], "to": [dst, dsock]}
        self.links.append(l)
        return l

    def disconnect(self, dst: str, dsock: str = "in") -> None:
        self.links = [l for l in self.links if not (l["to"][0] == dst and l["to"][1] == dsock)]

    def drop_dangling_links(self) -> None:
        """Remove links whose socket disappeared (e.g. a scene was renamed in the file)."""
        keep = []
        for l in self.links:
            a, b = self.nodes.get(l["from"][0]), self.nodes.get(l["to"][0])
            if a and b and l["from"][1] in self.outputs_of(a) and l["to"][1] in self.inputs_of(b):
                keep.append(l)
        self.links = keep

    def set_scenes(self, job_id: str, scenes: List[dict]) -> None:
        """Store the scenes Blender reported for a job's file. With one scene the job keeps a single
        'Scene' socket; links on the old active-scene socket move to the active scene's socket."""
        n = self.nodes[job_id]
        old = self.outputs_of(n)
        n["props"]["scenes"] = scenes
        new = self.outputs_of(n)
        active = next((s["name"] for s in scenes if s.get("active")), scenes[0]["name"] if scenes else None)
        for l in self.links:
            if l["from"][0] != job_id or l["from"][1] in new:
                continue
            if l["from"][1] == ACTIVE_SCENE and active and scene_socket(active) in new:
                l["from"][1] = scene_socket(active)
            elif socket_scene(l["from"][1]) and ACTIVE_SCENE in new:
                l["from"][1] = ACTIVE_SCENE
        if old != new:
            self.drop_dangling_links()

    # ---- resolving -----------------------------------------------------------------------
    def resolve(self, out_id: str) -> dict:
        """Everything the engine needs for one output:
        {"output", "job" (node or None), "scene" (None = active), "values", "sources",
         "chain" (top to bottom), "active", "problems"}."""
        out = self.nodes[out_id]
        chain_up = self._chain_up(out_id)        # nearest first
        job, scene, problems = None, None, []
        for i, nid in enumerate(chain_up):
            n = self.nodes[nid]
            if n["type"] == JOB:
                job = n
                # the socket the chain leaves the job from
                below = chain_up[i - 1] if i > 0 else out_id
                l = self.link_into(below)
                scene = socket_scene(l["from"][1]) if l else None
                break
        chain = list(reversed(chain_up)) + [out_id]
        values, sources = {}, {}
        for nid in chain:
            n = self.nodes[nid]
            if n["type"] in SETTINGS_TYPES and not n["muted"]:
                for k, v in (n["props"].get("values") or {}).items():
                    if v is not None and k in FIELDS[n["type"]]:
                        values[k] = v
                        sources[k] = nid
        if job is None:
            problems.append("not connected to a job")
        elif not job["props"].get("blend"):
            problems.append("the job has no .blend file")
        active = not out["muted"] and job is not None and not job["muted"] and bool(job["props"].get("blend"))
        return {"output": out, "job": job, "scene": scene, "values": values, "sources": sources,
                "chain": chain, "active": active, "problems": problems}

    # ---- scheduler -----------------------------------------------------------------------
    def move_block(self, ids: List[str], delta: int) -> None:
        """Push the selected outputs up (delta < 0) or down as one block, keeping their order."""
        self._clean_order()
        sel = [o for o in self.order if o in set(ids)]
        if not sel or not delta:
            return
        rest = [o for o in self.order if o not in set(sel)]
        first = self.order.index(sel[0])
        # position of the block among the remaining items
        before = len([o for o in self.order[:first] if o not in set(sel)])
        pos = max(0, min(len(rest), before + delta))
        self.order = rest[:pos] + sel + rest[pos:]

    def move_to(self, ids: List[str], index: int) -> None:
        self._clean_order()
        sel = [o for o in self.order if o in set(ids)]
        rest = [o for o in self.order if o not in set(sel)]
        index = max(0, min(len(rest), index))
        self.order = rest[:index] + sel + rest[index:]

    # ---- frames (replace the old queue folders) ------------------------------------------
    def add_frame(self, label: str, x: float, y: float, w: float, h: float, color: str = "#5a4a3a") -> dict:
        f = {"id": new_id(), "label": label or "Frame", "x": float(x), "y": float(y), "w": float(w),
             "h": float(h), "color": color}
        self.frames[f["id"]] = f
        return f

    def remove_frame(self, fid: str) -> None:
        self.frames.pop(fid, None)
        for n in self.nodes.values():
            if n.get("frame") == fid:
                n["frame"] = ""

    def frame_of(self, out_id: str) -> Optional[dict]:
        """The frame an output belongs to: its own, else its job's."""
        n = self.nodes.get(out_id)
        if not n:
            return None
        if n.get("frame") in self.frames:
            return self.frames[n["frame"]]
        r = self.resolve(out_id)
        j = r["job"]
        return self.frames.get(j.get("frame")) if j else None

    # ---- helpers for building graphs -----------------------------------------------------
    def add_job_with_output(self, blend: str, x: float, y: float, name: str = "",
                            render_root: str = "", props: Optional[dict] = None) -> Tuple[dict, dict]:
        """A job node and one output node wired below it - what 'Add .blend' and the API create."""
        p = {"blend": os.path.abspath(blend) if blend else ""}
        p.update(props or {})
        j = self.add_node(JOB, x, y, p)
        o = self.add_node(OUTPUT, x, y + 160, {"name": name or default_output_name(blend),
                                               "render_root": render_root})
        self.connect(j["id"], self.outputs_of(j)[0], o["id"])
        return j, o

    def free_position(self) -> Tuple[float, float]:
        """A spot to the right of everything except the scheduler."""
        xs = [n["x"] for n in self.nodes.values() if n["type"] != SCHEDULER]
        x = (max(xs) + 260) if xs else 0.0
        s = self.scheduler()
        if s and x + 240 > s["x"]:
            s["x"] = x + 320
        return x, 0.0


def default_output_name(blend: str, scene: Optional[str] = None) -> str:
    stem = os.path.splitext(os.path.basename(blend or ""))[0] or "render"
    return f"{stem}_{scene}" if scene else stem


def coerce(kind: str, value):
    if value is None:
        return None
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind in ("bool", "bool3"):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return str(value)


def describe_value(kind: str, value) -> str:
    if value is None:
        return "–"
    if kind in ("bool", "bool3"):
        return "on" if value else "off"
    return str(value)
