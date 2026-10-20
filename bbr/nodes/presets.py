"""Settings presets: settings nodes (and the links between them) saved as a .bbrpreset file
that any queue can load. Loading makes a copy; later changes to the file don't reach queues
that loaded it earlier."""
from __future__ import annotations

import copy
from typing import Dict, List, Tuple

from ..storage import atomic_write_json, read_json
from .graph import Graph, GraphError, SETTINGS_TYPES, new_id

PRESET_FORMAT = "bbr-preset"
PRESET_EXT = ".bbrpreset"


def extract(graph: Graph, node_ids: List[str], name: str = "") -> dict:
    """The settings nodes among `node_ids`, positions relative to the top-left one."""
    nodes = [graph.nodes[i] for i in node_ids if i in graph.nodes and graph.nodes[i]["type"] in SETTINGS_TYPES]
    if not nodes:
        raise GraphError("select one or more settings nodes to save as a preset")
    x0 = min(n["x"] for n in nodes)
    y0 = min(n["y"] for n in nodes)
    ids = {n["id"] for n in nodes}
    return {"format": PRESET_FORMAT, "version": 1, "name": name,
            "nodes": [{"id": n["id"], "type": n["type"], "x": n["x"] - x0, "y": n["y"] - y0,
                       "muted": n.get("muted", False), "label": n["props"].get("label", ""),
                       "values": copy.deepcopy(n["props"].get("values") or {})} for n in nodes],
            "links": [{"from": l["from"][0], "to": l["to"][0]} for l in graph.links
                      if l["from"][0] in ids and l["to"][0] in ids]}


def save(path: str, preset: dict) -> None:
    atomic_write_json(path, preset)


def load(path: str) -> dict:
    d = read_json(path)
    if not d or d.get("format") != PRESET_FORMAT:
        raise GraphError("not a Blender Batch Renderer preset file")
    return d


def insert(graph: Graph, preset: dict, x: float, y: float) -> Tuple[List[str], Dict[str, str]]:
    """Add a copy of the preset's nodes at (x, y). Returns (new node ids, old id -> new id)."""
    mapping: Dict[str, str] = {}
    for pn in preset.get("nodes") or []:
        if pn.get("type") not in SETTINGS_TYPES:
            continue
        props = {"values": copy.deepcopy(pn.get("values") or {})}
        label = pn.get("label") or preset.get("name") or ""
        if label:
            props["label"] = label
        n = graph.add_node(pn["type"], x + float(pn.get("x", 0)), y + float(pn.get("y", 0)), props, nid=new_id())
        n["muted"] = bool(pn.get("muted"))
        mapping[pn["id"]] = n["id"]
    for l in preset.get("links") or []:
        a, b = mapping.get(l.get("from")), mapping.get(l.get("to"))
        if a and b:
            try:
                graph.connect(a, "out", b)
            except GraphError:
                pass
    return list(mapping.values()), mapping
