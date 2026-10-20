"""The node editor: a vertical node graph (settings -> job -> output) with a scheduler node.

The graph itself lives in bbr/nodes/graph.py; this module only draws and edits it. Every change
emits `changed`, and the main window pushes the graph into the queue (bbr/nodes/sync.py).

Mouse and keys (like Blender's node editor where it makes sense)
  drag from a socket            connect; drag from a connected input to pick its wire up
  middle mouse drag / Alt+drag  pan            wheel  zoom             Home  show everything
  Shift+A / right click         add a node     M      mute / unmute    X / Delete  delete
  Ctrl+J                        put the selected nodes in a frame       Ctrl+D  duplicate settings nodes
"""
from __future__ import annotations

import copy
import os
import threading
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QEvent, QObject, QPointF, QRectF, Signal, QTimer
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetricsF, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPixmap, QIcon, QTransform)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsProxyWidget,
                               QGraphicsScene, QGraphicsView, QMenu, QWidget, QVBoxLayout, QHBoxLayout,
                               QListWidget, QListWidgetItem, QPushButton, QLabel, QAbstractItemView,
                               QSplitter, QScrollArea, QFrame, QFormLayout, QCheckBox, QSpinBox,
                               QDoubleSpinBox, QLineEdit, QComboBox, QToolButton, QFileDialog, QColorDialog,
                               QMessageBox)

from ..nodes import presets as preset_io
from ..nodes.graph import (Graph, GraphError, BLENDER, APP, JOB, OUTPUT, SCHEDULER, SETTINGS_TYPES, TITLES,
                           FIELDS, BLENDER_FIELDS, describe_value, default_output_name, socket_scene)

NODE_W = 210.0
TITLE_H = 24.0
LINE_H = 16.0
PAD = 8.0
SOCK_R = 6.0

TYPE_COLORS = {BLENDER: "#7a5bb5", APP: "#3f7f8f", JOB: "#b4683a", OUTPUT: "#3d7a3d", SCHEDULER: "#555c66"}
SOCKET_COLOR = "#c8c8c8"
WIRE_COLOR = "#9a9a9a"
MUTED_WIRE = "#b84040"


def _theme_dark(widget: Optional[QWidget]) -> bool:
    if widget is None:
        return True
    return widget.palette().window().color().lightness() < 128


def _int(v) -> int:
    """Key codes as plain ints (QKeyEvent.key() is an int, Qt.Key an enum)."""
    return v.value if hasattr(v, "value") else int(v)


def swatch(color: str, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(QColor(color or "#888888"))
    return QIcon(pm)


# =============================================================================================
# items
# =============================================================================================
class SocketItem(QGraphicsItem):
    def __init__(self, node_item: "NodeItem", name: str, is_output: bool):
        super().__init__(node_item)
        self.node_item = node_item
        self.name = name
        self.is_output = is_output
        self.setAcceptHoverEvents(True)
        self.setZValue(2)
        self._hover = False

    def boundingRect(self) -> QRectF:
        r = SOCK_R + 3
        return QRectF(-r, -r, 2 * r, 2 * r)

    def paint(self, p: QPainter, opt, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#222222"), 1.2))
        p.setBrush(QBrush(QColor("#ffffff" if self._hover else SOCKET_COLOR)))
        p.drawEllipse(QPointF(0, 0), SOCK_R, SOCK_R)

    def hoverEnterEvent(self, e):
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, e):
        self._hover = False
        self.update()

    def center(self) -> QPointF:
        return self.scenePos()


class WireItem(QGraphicsPathItem):
    """A link, drawn as a vertical S-curve from an output socket down to an input socket."""

    def __init__(self, link: Optional[dict] = None):
        super().__init__()
        self.link = link
        self.setZValue(-1)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, link is not None)
        self.a = QPointF()
        self.b = QPointF()
        self.muted = False

    def set_ends(self, a: QPointF, b: QPointF):
        self.a, self.b = QPointF(a), QPointF(b)
        dy = max(40.0, abs(b.y() - a.y()) * 0.5)
        path = QPainterPath(a)
        path.cubicTo(QPointF(a.x(), a.y() + dy), QPointF(b.x(), b.y() - dy), b)
        self.setPath(path)

    def paint(self, p: QPainter, opt, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = QColor("#ffb347") if self.isSelected() else QColor(MUTED_WIRE if self.muted else WIRE_COLOR)
        pen = QPen(col, 2.2 if self.isSelected() else 1.8)
        if self.link is None:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(self.path())

    def shape(self):
        s = QPainterPathStroker()
        s.setWidth(10)
        return s.createStroke(self.path())


class NodeItem(QGraphicsObject):
    """One node: a title bar, a few lines of text, an input socket on top, outputs at the bottom."""

    def __init__(self, scene: "NodeScene", node: dict):
        super().__init__()
        self.ns = scene
        self.node = node
        self.inputs: Dict[str, SocketItem] = {}
        self.outputs: Dict[str, SocketItem] = {}
        self._lines: List[tuple] = []
        self._status = None            # (text, colour, fraction) for output nodes
        self._h = TITLE_H + 2 * PAD
        self.setPos(node["x"], node["y"])
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(1)
        self.rebuild()

    @property
    def nid(self) -> str:
        return self.node["id"]

    # ---- content ----------------------------------------------------------------------------
    def title(self) -> str:
        n = self.node
        label = n["props"].get("label") or ""
        if n["type"] == JOB:
            b = n["props"].get("blend") or ""
            return label or (os.path.basename(b) if b else "Job – no file")
        if n["type"] == OUTPUT:
            return n["props"].get("name") or "Output"
        return label or TITLES.get(n["type"], n["type"])

    def rebuild(self):
        self.prepareGeometryChange()
        self._lines = self.ns.summary(self.node)
        g = self.ns.graph
        ins = g.inputs_of(self.node)
        outs = g.outputs_of(self.node)
        for d, want, is_out in ((self.inputs, ins, False), (self.outputs, outs, True)):
            for name in list(d):
                if name not in want:
                    item = d.pop(name)
                    item.setParentItem(None)
                    if item.scene():
                        item.scene().removeItem(item)
            for name in want:
                if name not in d:
                    d[name] = SocketItem(self, name, is_out)
        labels = len(outs) > 1
        self._h = TITLE_H + PAD + LINE_H * max(1, len(self._lines)) + PAD + (LINE_H if labels else 0)
        if self.node["type"] == OUTPUT:
            self._h += 10                  # progress bar
        if self.node["type"] == SCHEDULER:
            self._h = self.ns.scheduler_height()
        w = self.width()
        for name, s in self.inputs.items():
            s.setPos(w / 2, 0)
        for i, name in enumerate(outs):
            s = self.outputs[name]
            s.setPos(w * (i + 1) / (len(outs) + 1), self._h)
        self.update()

    def width(self) -> float:
        return 300.0 if self.node["type"] == SCHEDULER else NODE_W

    def set_status(self, st):
        if st != self._status:
            text_changed = (st or (None,))[0] != (self._status or (None,))[0]
            self._status = st
            if text_changed:               # the status line is part of the node's text
                self.rebuild()
                self.ns.update_wires(self.nid)
            else:
                self.update()

    # ---- drawing ----------------------------------------------------------------------------
    def boundingRect(self) -> QRectF:
        return QRectF(-3, -3, self.width() + 6, self._h + 6)

    def paint(self, p: QPainter, opt, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = self.node
        w, h = self.width(), self._h
        muted = n.get("muted")
        dark = self.ns.dark
        body = QColor("#2e3136" if dark else "#f4f4f4")
        text = QColor("#e8e8e8" if dark else "#202020")
        quiet = QColor("#9aa0a6" if dark else "#707070")
        head = QColor(TYPE_COLORS.get(n["type"], "#666666"))
        if n["type"] == OUTPUT:
            head = QColor(n["props"].get("color") or TYPE_COLORS[OUTPUT])
        if muted:
            head = QColor("#6a6a6a")
            body = body.darker(115) if dark else body.lighter(102)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), 7, 7)
        p.setPen(QPen(QColor("#ffb347"), 2.0) if self.isSelected() else QPen(QColor("#151515"), 1.0))
        p.setBrush(QBrush(body))
        p.drawPath(path)
        # title bar
        tpath = QPainterPath()
        tpath.addRoundedRect(QRectF(0, 0, w, TITLE_H), 7, 7)
        tpath.addRect(QRectF(0, TITLE_H / 2, w, TITLE_H / 2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(head))
        p.drawPath(tpath.simplified())
        f = QFont(p.font())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(QColor("#ffffff")))
        fm = QFontMetricsF(f)
        title = fm.elidedText(self.title() + ("   (muted)" if muted else ""), Qt.TextElideMode.ElideRight, w - 16)
        p.drawText(QRectF(8, 0, w - 16, TITLE_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)
        if n["type"] == SCHEDULER:
            return
        # lines
        f.setBold(False)
        p.setFont(f)
        fm = QFontMetricsF(f)
        y = TITLE_H + PAD
        for line in self._lines or [("–", True)]:
            s, is_quiet = line[0], line[1]
            p.setPen(QPen(quiet if is_quiet else text))
            p.drawText(QRectF(10, y, w - 20, LINE_H), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(s, Qt.TextElideMode.ElideRight, w - 20))
            y += LINE_H
        # socket labels (job with several scenes)
        outs = list(self.outputs)
        if len(outs) > 1:
            g = self.ns.graph
            for i, name in enumerate(outs):
                cx = w * (i + 1) / (len(outs) + 1)
                lab = fm.elidedText(g.socket_label(n, name), Qt.TextElideMode.ElideRight, w / len(outs) - 4)
                p.setPen(QPen(quiet))
                p.drawText(QRectF(cx - w / len(outs) / 2, h - LINE_H - 6, w / len(outs), LINE_H),
                           Qt.AlignmentFlag.AlignCenter, lab)
        # status / progress on outputs
        if n["type"] == OUTPUT and self._status:
            txt, col, frac = self._status
            bar = QRectF(10, h - 12, w - 20, 5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#444a52" if dark else "#d8d8d8")))
            p.drawRoundedRect(bar, 2, 2)
            if frac:
                p.setBrush(QBrush(QColor(col or "#3d8bfd")))
                p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * max(0.0, min(1.0, frac)), bar.height()), 2, 2)

    # ---- movement ---------------------------------------------------------------------------
    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.node["x"], self.node["y"] = float(self.pos().x()), float(self.pos().y())
            self.ns.update_wires(self.nid)
            self.ns.moved()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, e):
        super().mouseDoubleClickEvent(e)
        ns, nid = self.ns, self.nid
        # later: the handler may redraw the scene, which deletes this item
        QTimer.singleShot(0, lambda: ns.node_double_clicked.emit(nid))


class FrameItem(QGraphicsObject):
    """A frame around nodes (replaces the old queue folders). Moving it moves the nodes inside."""

    HANDLE = 14.0

    def __init__(self, scene: "NodeScene", frame: dict):
        super().__init__()
        self.ns = scene
        self.frame = frame
        self._resizing = False
        self.setPos(frame["x"], frame["y"])
        self._last = QPointF(self.pos())
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(-10)

    @property
    def fid(self) -> str:
        return self.frame["id"]

    def rect(self) -> QRectF:
        return QRectF(0, 0, self.frame["w"], self.frame["h"])

    def boundingRect(self) -> QRectF:
        return self.rect().adjusted(-2, -2, 2, 2)

    def paint(self, p: QPainter, opt, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self.frame.get("color") or "#5a4a3a")
        fill = QColor(c)
        fill.setAlpha(90)
        p.setPen(QPen(QColor("#ffb347"), 2) if self.isSelected() else QPen(c.lighter(130), 1))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(self.rect(), 6, 6)
        f = QFont(p.font())
        if f.pointSizeF() > 0:
            f.setPointSizeF(f.pointSizeF() * 1.25)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(QColor("#f0f0f0") if self.ns.dark else QColor("#303030")))
        p.drawText(QRectF(10, 4, self.frame["w"] - 20, 26), Qt.AlignmentFlag.AlignVCenter, self.frame.get("label", ""))
        hp = QPainterPath()
        r = self.rect()
        hp.moveTo(r.right() - self.HANDLE, r.bottom() - 3)
        hp.lineTo(r.right() - 3, r.bottom() - self.HANDLE)
        p.setPen(QPen(c.lighter(150), 1.5))
        p.drawPath(hp)

    def _in_handle(self, pos: QPointF) -> bool:
        r = self.rect()
        return pos.x() > r.right() - self.HANDLE * 1.5 and pos.y() > r.bottom() - self.HANDLE * 1.5

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._in_handle(e.pos()):
            self._resizing = True
            e.accept()
            return
        self._last = QPointF(self.pos())
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._resizing:
            self.prepareGeometryChange()
            self.frame["w"] = max(120.0, e.pos().x())
            self.frame["h"] = max(80.0, e.pos().y())
            self.update()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._resizing:
            self._resizing = False
            self.ns.frame_resized(self.fid)
            return
        super().mouseReleaseEvent(e)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            new = QPointF(self.pos())
            d = new - self._last
            self._last = new
            self.frame["x"], self.frame["y"] = float(new.x()), float(new.y())
            self.ns.move_frame_members(self.fid, d)
            self.ns.moved()
        return super().itemChange(change, value)


# =============================================================================================
# the scheduler node's widget
# =============================================================================================
class SchedulerWidget(QWidget):
    """Lists every output in render order. Push up/down moves the selection as a block."""

    def __init__(self, ns: "NodeScene"):
        super().__init__()
        self.ns = ns
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setToolTip("Render order, top first. Select outputs and push them up or down.\n"
                             "Moving a job above the one rendering lets the rendering job finish first.")
        self.list.itemDoubleClicked.connect(lambda it: self.ns.focus_node.emit(it.data(Qt.ItemDataRole.UserRole)))
        self.list.itemSelectionChanged.connect(self._selected)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.b_up = QPushButton("Push up")
        self.b_up.setToolTip("Move the selected outputs up one place, as a block")
        self.b_up.clicked.connect(lambda: self.ns.push(self.selected_ids(), -1))
        self.b_down = QPushButton("Push down")
        self.b_down.clicked.connect(lambda: self.ns.push(self.selected_ids(), 1))
        self.b_top = QToolButton()
        self.b_top.setText("⤒")
        self.b_top.setToolTip("Move the selected outputs to the top (they render next, after the current job)")
        self.b_top.clicked.connect(lambda: self.ns.push_to_top(self.selected_ids()))
        for b in (self.b_up, self.b_down, self.b_top):
            row.addWidget(b)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        self.b_mute = QPushButton("Mute")
        self.b_mute.setToolTip("Switch the selected outputs off or on (M on the node does the same)")
        self.b_mute.clicked.connect(lambda: self.ns.toggle_mute(self.selected_ids()))
        self.b_cancel = QPushButton("Cancel")
        self.b_cancel.setToolTip("Stop the selected job if it is rendering and mute its output. "
                                 "Finished frames are kept; unmute it to continue later.")
        self.b_cancel.clicked.connect(lambda: self.ns.cancel_requested.emit(self.selected_ids()))
        row2.addWidget(self.b_mute)
        row2.addWidget(self.b_cancel)
        lay.addLayout(row2)
        mrow = QHBoxLayout()
        self.l_machines = QLabel("Machines")
        self.l_machines.setWordWrap(True)
        self.b_machines = QPushButton("Machines…")
        self.b_machines.setToolTip("Pair, switch on and off, and watch the computers that render this queue")
        self.b_machines.clicked.connect(self.ns.open_machines.emit)
        mrow.addWidget(self.l_machines, 1)
        mrow.addWidget(self.b_machines)
        lay.addLayout(mrow)
        self.setFixedWidth(288)
        self.setFixedHeight(int(self.ns.scheduler_height() - TITLE_H - 8))
        self._updating = False

    def selected_ids(self) -> List[str]:
        return [it.data(Qt.ItemDataRole.UserRole) for it in self.list.selectedItems()]

    def _selected(self):
        if not self._updating:
            self.ns.select_nodes(self.selected_ids())

    def _row(self, i: int, o: dict):
        g = self.ns.graph
        st = self.ns.status_for(o)
        txt = f"{i + 1}  {o['props'].get('name') or 'Output'}"
        if st and st[0]:
            txt += f"   –  {st[0]}"
        active = g.resolve(o["id"])["active"]
        if o.get("muted"):
            txt += "   (muted)"
        elif not active:
            txt += "   (not connected)"
        return txt, (o.get("muted") or not active)

    def refresh(self):
        g = self.ns.graph
        outs = g.outputs()
        ids = [o["id"] for o in outs]
        cur = [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]
        if ids == cur:
            # same rows: update texts in place (keeps the selection and the scroll position)
            for i, o in enumerate(outs):
                it = self.list.item(i)
                txt, quiet = self._row(i, o)
                if it.text() != txt:
                    it.setText(txt)
                it.setIcon(swatch(o["props"].get("color")))
                it.setForeground(QColor("#888888") if quiet else self.list.palette().text().color())
            self.l_machines.setText(self.ns.machines_text())
            return
        keep = set(self.selected_ids())
        self._updating = True
        self.list.clear()
        for i, o in enumerate(outs):
            txt, quiet = self._row(i, o)
            it = QListWidgetItem(swatch(o["props"].get("color")), txt)
            it.setData(Qt.ItemDataRole.UserRole, o["id"])
            if quiet:
                it.setForeground(QColor("#888888"))
            self.list.addItem(it)
            if o["id"] in keep:
                it.setSelected(True)
        self._updating = False
        self.l_machines.setText(self.ns.machines_text())


# =============================================================================================
# the scene
# =============================================================================================
class _Bridge(QObject):
    done = Signal(str, object)


class NodeScene(QGraphicsScene):
    changed_graph = Signal()           # structure or values changed -> sync with the queue
    moved_only = Signal()              # positions only (still saved, but nothing to sync)
    selection_nodes = Signal(list)     # selected node ids
    node_double_clicked = Signal(str)
    focus_node = Signal(str)
    cancel_requested = Signal(list)    # output ids
    open_machines = Signal()
    message = Signal(str, str)         # level, text

    def __init__(self, graph: Graph, parent=None):
        super().__init__(parent)
        self.graph = graph
        self.items_by_id: Dict[str, NodeItem] = {}
        self.frames_by_id: Dict[str, FrameItem] = {}
        self.wires: List[WireItem] = []
        self.dark = True
        self.status_provider: Callable[[str], Optional[tuple]] = lambda jid: None
        self.machines_provider: Callable[[], str] = lambda: ""
        self.installs_provider: Callable[[], list] = lambda: []
        self._drag = None                  # (fixed socket item, temp wire)
        self._sched_widget: Optional[SchedulerWidget] = None
        self.setSceneRect(QRectF(-20000, -20000, 40000, 40000))
        self.selectionChanged.connect(self._sel_changed)
        self._bridge = _Bridge()
        self._bridge.done.connect(self._probe_done, Qt.ConnectionType.QueuedConnection)
        self._probing = set()

    # ---- building ------------------------------------------------------------------------------
    def set_graph(self, graph: Graph):
        self.graph = graph
        self.rebuild()

    def rebuild(self):
        sel = self.selected_ids()
        self.blockSignals(True)
        self._sched_widget = None
        self.clear()
        self.items_by_id, self.frames_by_id, self.wires = {}, {}, []
        for f in self.graph.frames.values():
            it = FrameItem(self, f)
            self.addItem(it)
            self.frames_by_id[f["id"]] = it
        for n in self.graph.nodes.values():
            self._add_item(n)
        self.rebuild_wires()
        for nid in sel:
            it = self.items_by_id.get(nid) or self.frames_by_id.get(nid)
            if it:
                it.setSelected(True)
        self.blockSignals(False)
        self.refresh_scheduler()

    def _add_item(self, n: dict) -> NodeItem:
        it = NodeItem(self, n)
        self.addItem(it)
        self.items_by_id[n["id"]] = it
        if n["type"] == SCHEDULER:
            w = SchedulerWidget(self)
            proxy = QGraphicsProxyWidget(it)
            proxy.setWidget(w)
            proxy.setPos(6, TITLE_H + 2)
            self._sched_widget = w
        return it

    def scheduler_height(self) -> float:
        return 360.0

    def rebuild_wires(self):
        for w in self.wires:
            self.removeItem(w)
        self.wires = []
        for l in self.graph.links:
            w = WireItem(l)
            self.addItem(w)
            self.wires.append(w)
        self.update_wires()

    def update_wires(self, nid: Optional[str] = None):
        for w in self.wires:
            l = w.link
            if nid and nid not in (l["from"][0], l["to"][0]):
                continue
            a = self.items_by_id.get(l["from"][0])
            b = self.items_by_id.get(l["to"][0])
            if not a or not b or l["from"][1] not in a.outputs or l["to"][1] not in b.inputs:
                w.setVisible(False)
                continue
            w.setVisible(True)
            w.muted = bool(a.node.get("muted") or b.node.get("muted"))
            w.set_ends(a.outputs[l["from"][1]].center(), b.inputs[l["to"][1]].center())

    def refresh_node(self, nid: str):
        it = self.items_by_id.get(nid)
        if it:
            it.rebuild()
            self.update_wires(nid)

    def refresh_all_nodes(self):
        for it in self.items_by_id.values():
            it.rebuild()
        self.update_wires()

    def refresh_scheduler(self):
        if self._sched_widget is not None:
            self._sched_widget.refresh()

    def refresh_status(self):
        for it in self.items_by_id.values():
            if it.node["type"] == OUTPUT:
                it.set_status(self.status_for(it.node))
        self.refresh_scheduler()

    def status_for(self, out: dict) -> Optional[tuple]:
        jid = out["props"].get("job_id")
        if out["props"].get("problem"):
            return (out["props"]["problem"], "#dc3545", None)
        return self.status_provider(jid) if jid else None

    def machines_text(self) -> str:
        return self.machines_provider()

    # ---- what a node shows -----------------------------------------------------------------------
    def summary(self, n: dict) -> List[tuple]:
        """Lines of (text, quiet) drawn in a node."""
        g = self.graph
        t = n["type"]
        if t in SETTINGS_TYPES:
            vals = n["props"].get("values") or {}
            if not vals:
                return [("nothing set – all from the file" if t == BLENDER else "nothing set – app defaults", True)]
            return [(f"{FIELDS[t][k][0]}: {describe_value(FIELDS[t][k][1], v)}", False)
                    for k, v in vals.items() if k in FIELDS[t]]
        if t == JOB:
            p = n["props"]
            lines = []
            if not p.get("blend"):
                lines.append(("double-click to choose a .blend", True))
            else:
                sc = p.get("scenes") or []
                lines.append((f"{len(sc)} scenes" if len(sc) > 1 else
                              (f"scene: {sc[0]['name']}" if sc else "scenes: reading…" if n["id"] in self._probing
                               else "scenes not read yet"), True))
                lines.append(("Blender: " + ("auto" if (p.get("blender") or "auto") == "auto"
                                             else os.path.basename(os.path.dirname(p["blender"])) or p["blender"]), True))
                dev = p.get("devices") or {}
                if dev:
                    lines.append(("devices: " + ", ".join(f"{k} {v}" for k, v in dev.items()), False))
            return lines
        if t == OUTPUT:
            r = g.resolve(n["id"])
            lines = []
            if r["problems"]:
                lines.append((r["problems"][0], True))
            else:
                if r["scene"]:
                    lines.append((f"scene: {r['scene']}", False))
                shown = 0
                for k, v in r["values"].items():
                    kind = (BLENDER_FIELDS.get(k) or FIELDS[APP].get(k))
                    if kind and shown < 4:
                        lines.append((f"{kind[0]}: {describe_value(kind[1], v)}", False))
                        shown += 1
                if len(r["values"]) > shown:
                    lines.append((f"+ {len(r['values']) - shown} more", True))
                lines.append(("everything else from the file", True))
            st = self.status_for(n)
            if st and st[0]:
                lines.append((st[0], True))
            return lines
        return []

    # ---- selection -------------------------------------------------------------------------------
    def selected_ids(self) -> List[str]:
        out = []
        for it in self.selectedItems():
            if isinstance(it, NodeItem):
                out.append(it.nid)
            elif isinstance(it, FrameItem):
                out.append(it.fid)
        return out

    def selected_node_items(self) -> List[NodeItem]:
        return [it for it in self.selectedItems() if isinstance(it, NodeItem)]

    def select_nodes(self, ids: List[str]):
        self.blockSignals(True)
        self.clearSelection()
        for i in ids:
            it = self.items_by_id.get(i)
            if it:
                it.setSelected(True)
        self.blockSignals(False)
        self._sel_changed()

    def _sel_changed(self):
        self.selection_nodes.emit(self.selected_ids())

    # ---- edits (each ends in changed_graph) --------------------------------------------------------
    def edited(self, nids: Optional[List[str]] = None):
        """Something in the graph changed: redraw what depends on it and tell the window."""
        self.refresh_all_nodes()
        self.refresh_scheduler()
        self.changed_graph.emit()

    def moved(self):
        self.moved_only.emit()

    def add_node(self, ntype: str, pos: QPointF, props: Optional[dict] = None) -> Optional[dict]:
        if ntype == SCHEDULER and self.graph.scheduler():
            return None
        n = self.graph.add_node(ntype, pos.x(), pos.y(), props)
        self._add_item(n)
        self.select_nodes([n["id"]])
        if ntype == JOB and n["props"].get("blend"):
            self.probe_scenes(n["id"])
        self.edited()
        return n

    def add_blends(self, files: List[str], pos: QPointF):
        x, y = pos.x(), pos.y()
        new = []
        for f in files:
            j, o = self.graph.add_job_with_output(f, x, y)
            new += [j["id"], o["id"]]
            x += NODE_W + 50
        self.rebuild()
        for nid in new:
            if self.graph.nodes[nid]["type"] == JOB:
                self.probe_scenes(nid)
        self.select_nodes(new)
        self.edited()

    def set_blend(self, nid: str, path: str):
        """Give a job node another .blend: its scenes are read again, links fall back to the one socket."""
        n = self.graph.nodes.get(nid)
        if not n:
            return
        n["props"]["blend"] = os.path.abspath(path)
        n["props"]["scenes"] = []
        first = self.graph.outputs_of(n)[0]
        for l in self.graph.links_from(nid):
            l["from"][1] = first
        self.graph.drop_dangling_links()
        self.rebuild()
        self.probe_scenes(nid)
        self.edited()

    def delete_selected(self):
        nids = [it.nid for it in self.selected_node_items() if it.node["type"] != SCHEDULER]
        frames = [it.fid for it in self.selectedItems() if isinstance(it, FrameItem)]
        wires = [w.link for w in self.wires if w.isSelected()]
        if not (nids or frames or wires):
            return
        for l in wires:
            if l in self.graph.links:
                self.graph.links.remove(l)
        for nid in nids:
            self.graph.remove_node(nid)
        for fid in frames:
            self.graph.remove_frame(fid)
        self.rebuild()
        self.edited()

    def toggle_mute(self, ids: Optional[List[str]] = None):
        ids = ids if ids is not None else [it.nid for it in self.selected_node_items()]
        nodes = [self.graph.nodes[i] for i in ids if i in self.graph.nodes and self.graph.nodes[i]["type"] != SCHEDULER]
        if not nodes:
            return
        mute = not all(n.get("muted") for n in nodes)
        for n in nodes:
            self.graph.set_muted(n["id"], mute)
        self.edited()

    def push(self, ids: List[str], delta: int):
        if ids:
            self.graph.move_block(ids, delta)
            self.edited()

    def push_to_top(self, ids: List[str]):
        if ids:
            self.graph.move_to(ids, 0)
            self.edited()

    def duplicate_selected(self):
        """Duplicate settings nodes (Ctrl+D) - handy for making a variant of a preset."""
        src = [it.node for it in self.selected_node_items() if it.node["type"] in SETTINGS_TYPES]
        if not src:
            return
        new = []
        for n in src:
            m = self.graph.add_node(n["type"], n["x"] + 40, n["y"] + 40, copy.deepcopy(n["props"]))
            m["muted"] = n.get("muted", False)
            new.append(m["id"])
        self.rebuild()
        self.select_nodes(new)
        self.edited()

    def frame_selected(self):
        items = self.selected_node_items()
        if not items:
            return
        r = QRectF()
        for it in items:
            r = r.united(it.sceneBoundingRect())
        r = r.adjusted(-24, -44, 24, 24)
        f = self.graph.add_frame("Frame", r.x(), r.y(), r.width(), r.height())
        for it in items:
            it.node["frame"] = f["id"]
        self.rebuild()
        self.select_nodes([])
        fi = self.frames_by_id.get(f["id"])
        if fi:
            fi.setSelected(True)
        self.edited()

    def move_frame_members(self, fid: str, delta: QPointF):
        if delta.isNull():
            return
        for it in self.items_by_id.values():
            if it.node.get("frame") == fid and not it.isSelected():
                it.setPos(it.pos() + delta)

    def frame_resized(self, fid: str):
        self._assign_frames(list(self.items_by_id.values()))
        self.moved()

    def _assign_frames(self, items: List[NodeItem]):
        """A node belongs to the frame its centre sits in (the smallest one, if frames overlap)."""
        changed = False
        for it in items:
            if it.node["type"] == SCHEDULER:
                continue
            c = it.sceneBoundingRect().center()
            best = None
            for fi in self.frames_by_id.values():
                if fi.sceneBoundingRect().contains(c):
                    if best is None or fi.frame["w"] * fi.frame["h"] < best.frame["w"] * best.frame["h"]:
                        best = fi
            fid = best.fid if best else ""
            if it.node.get("frame", "") != fid:
                it.node["frame"] = fid
                changed = True
        if changed:
            self.refresh_scheduler()

    # ---- connecting ------------------------------------------------------------------------------
    def _socket_at(self, pos: QPointF) -> Optional[SocketItem]:
        for it in self.items(pos):
            if isinstance(it, SocketItem):
                return it
        # forgiving: anywhere near a socket
        for it in self.items(QRectF(pos.x() - 10, pos.y() - 10, 20, 20)):
            if isinstance(it, SocketItem):
                return it
        return None

    def _node_at(self, pos: QPointF) -> Optional[NodeItem]:
        for it in self.items(pos):
            if isinstance(it, NodeItem):
                return it
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            s = self._socket_at(e.scenePos())
            if s is not None:
                if s.is_output:
                    fixed = s
                else:
                    # pick up the wire plugged into this input, like Blender
                    l = self.graph.link_into(s.node_item.nid, s.name)
                    if l is None:
                        fixed = s
                    else:
                        self.graph.links.remove(l)
                        a = self.items_by_id.get(l["from"][0])
                        fixed = a.outputs.get(l["from"][1]) if a else None
                        self.rebuild_wires()
                        if fixed is None:
                            self.edited()
                            return
                tmp = WireItem(None)
                self.addItem(tmp)
                tmp.set_ends(fixed.center(), e.scenePos()) if fixed.is_output else tmp.set_ends(e.scenePos(), fixed.center())
                self._drag = (fixed, tmp, s is not fixed)
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag:
            fixed, tmp, _ = self._drag
            if fixed.is_output:
                tmp.set_ends(fixed.center(), e.scenePos())
            else:
                tmp.set_ends(e.scenePos(), fixed.center())
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag:
            fixed, tmp, picked_up = self._drag
            self._drag = None
            self.removeItem(tmp)
            target = self._socket_at(e.scenePos())
            if target is None:
                node = self._node_at(e.scenePos())
                if node is not None:            # dropped on a node: use its input / first output
                    if fixed.is_output and node.inputs:
                        target = next(iter(node.inputs.values()))
                    elif not fixed.is_output and node.outputs:
                        target = next(iter(node.outputs.values()))
            if target is not None and target.is_output != fixed.is_output:
                src, dst = (fixed, target) if fixed.is_output else (target, fixed)
                try:
                    self.graph.connect(src.node_item.nid, src.name, dst.node_item.nid, dst.name)
                except GraphError as err:
                    self.message.emit("warn", f"Can't connect: {err}")
            self.rebuild_wires()
            self.edited()
            e.accept()
            return
        moved = [it for it in self.selectedItems() if isinstance(it, NodeItem)]
        super().mouseReleaseEvent(e)
        if moved:
            self._assign_frames(moved)

    # ---- scenes of a job's file --------------------------------------------------------------------
    def probe_scenes(self, nid: str):
        """Ask Blender (in the background) which scenes the job's file has."""
        n = self.graph.nodes.get(nid)
        if not n or not n["props"].get("blend") or nid in self._probing:
            return
        from ..blendfile import read_blend_version
        from ..installs import choose_install
        from ..nodes.probe import list_scenes
        blend = n["props"]["blend"]
        insts = self.installs_provider()
        chosen = n["props"].get("blender") or "auto"
        inst = None
        if chosen != "auto":
            inst = next((i for i in insts if i.path == chosen or i.executable == chosen), None)
        if inst is None and insts:
            inst, _reason = choose_install(insts, read_blend_version(blend))
        if inst is None:
            self.message.emit("warn", "No Blender install to read the scenes of " + os.path.basename(blend))
            return
        self._probing.add(nid)
        self.refresh_node(nid)
        exe = inst.executable

        def work():
            res = list_scenes(exe, blend)
            self._bridge.done.emit(nid, res)

        threading.Thread(target=work, daemon=True, name="bbr-scenes").start()

    def _probe_done(self, nid: str, res):
        self._probing.discard(nid)
        n = self.graph.nodes.get(nid)
        if not n:
            return
        if not res or not res.get("scenes"):
            self.message.emit("warn", f"Could not read the scenes of {os.path.basename(n['props'].get('blend', ''))}")
            self.refresh_node(nid)
            return
        self.graph.set_scenes(nid, res["scenes"])
        self.rebuild()
        self.edited()


# =============================================================================================
# the view
# =============================================================================================
class NodeView(QGraphicsView):
    files_dropped = Signal(list, QPointF)

    def __init__(self, scene: NodeScene):
        super().__init__(scene)
        self.ns = scene
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pan = None
        self.add_menu_provider: Callable[[QPointF], QMenu] = lambda pos: QMenu(self)

    def drawBackground(self, p: QPainter, rect: QRectF):
        dark = self.ns.dark
        p.fillRect(rect, QColor("#1f2124" if dark else "#e6e6e6"))
        step = 24
        pen = QPen(QColor("#2a2d31" if dark else "#d6d6d6"), 1)
        p.setPen(pen)
        left = int(rect.left()) - int(rect.left()) % step
        top = int(rect.top()) - int(rect.top()) % step
        x = left
        while x < rect.right():
            p.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            x += step
        y = top
        while y < rect.bottom():
            p.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            y += step

    def _zoom(self, f: float):
        cur = self.transform().m11()
        if 0.2 < cur * f < 3.0:
            self.scale(f, f)

    def wheelEvent(self, e):
        ctrl = bool(e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        pd = e.pixelDelta()
        if not pd.isNull() and not ctrl:
            # trackpad: two fingers pan (pinch zooms, see viewportEvent)
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - pd.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - pd.y())
            e.accept()
            return
        dy = e.angleDelta().y() or pd.y()
        if dy:
            self._zoom(1.15 if dy > 0 else 1 / 1.15)
        e.accept()

    def viewportEvent(self, e):
        if e.type() == QEvent.Type.NativeGesture and e.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            self._zoom(1.0 + float(e.value()))
            return True
        return super().viewportEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton or (
                e.button() == Qt.MouseButton.LeftButton and e.modifiers() & Qt.KeyboardModifier.AltModifier):
            self._pan = e.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._pan is not None:
            d = e.position() - self._pan
            self._pan = e.position()
            self.horizontalScrollBar().setValue(int(self.horizontalScrollBar().value() - d.x()))
            self.verticalScrollBar().setValue(int(self.verticalScrollBar().value() - d.y()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pan is not None:
            self._pan = None
            self.unsetCursor()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if isinstance(self.ns.focusItem(), QGraphicsProxyWidget):
            super().keyPressEvent(e)           # typing in the scheduler's list
            return
        k, mods = _int(e.key()), e.modifiers()
        ctrl = bool(mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if k in (_int(Qt.Key.Key_Delete), _int(Qt.Key.Key_Backspace), _int(Qt.Key.Key_X)) and not ctrl:
            self.ns.delete_selected()
        elif k == _int(Qt.Key.Key_M) and not ctrl:
            self.ns.toggle_mute()
        elif k == _int(Qt.Key.Key_A) and shift:
            self._add_menu_at_cursor()
        elif k == _int(Qt.Key.Key_J) and ctrl:
            self.ns.frame_selected()
        elif k == _int(Qt.Key.Key_D) and ctrl:
            self.ns.duplicate_selected()
        elif k == _int(Qt.Key.Key_Home):
            self.show_all()
        else:
            super().keyPressEvent(e)
            return
        e.accept()

    def _add_menu_at_cursor(self):
        vp = self.mapFromGlobal(QCursor.pos())
        m = self.add_menu_provider(self.mapToScene(vp))
        m.exec(QCursor.pos())

    def show_all(self):
        r = QRectF()
        for it in self.ns.items_by_id.values():
            r = r.united(it.sceneBoundingRect())
        for it in self.ns.frames_by_id.values():
            r = r.united(it.sceneBoundingRect())
        if r.isNull():
            return
        self.resetTransform()
        self.fitInView(r.adjusted(-60, -60, 60, 60), Qt.AspectRatioMode.KeepAspectRatio)
        if self.transform().m11() > 1.0:
            self.resetTransform()
            self.centerOn(r.center())

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile().lower().endswith(".blend")]
        if files:
            self.files_dropped.emit(files, self.mapToScene(e.position().toPoint()))
            e.acceptProposedAction()


# =============================================================================================
# the properties panel (right of the editor, like Blender's N panel)
# =============================================================================================
INT_RANGES = {"frame_start": (-1000000, 1000000), "frame_end": (-1000000, 1000000), "frame_step": (1, 10000),
              "samples": (1, 1000000), "resolution_pct": (1, 1000), "res_x": (4, 65536), "res_y": (4, 65536),
              "max_attempts": (1, 100), "retry_delay_s": (0, 86400), "restart_every": (0, 100000)}
FLOAT_RANGES = {"noise_threshold": (0.0, 1.0, 4, 0.001), "frame_timeout_min": (0.0, 100000.0, 1, 1.0)}


class PropertiesPanel(QScrollArea):
    def __init__(self, ns: NodeScene, machines_provider: Callable[[], list]):
        super().__init__()
        self.ns = ns
        self.machines_provider = machines_provider
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(260)
        self.nid: Optional[str] = None
        self.show_for(None)

    # ---- helpers ---------------------------------------------------------------------------------
    def _page(self, title: str):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; font-size: 14px;")
        t.setWordWrap(True)
        lay.addWidget(t)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        lay.addLayout(form)
        return w, lay, form

    def _note(self, lay, text: str):
        l = QLabel(text)
        l.setWordWrap(True)
        l.setStyleSheet("color: gray;")
        lay.addWidget(l)
        return l

    def _changed(self):
        if self.nid:
            self.ns.refresh_node(self.nid)
        self.ns.edited()

    # ---- pages -----------------------------------------------------------------------------------
    def show_for(self, nid: Optional[str]):
        self.nid = nid
        g = self.ns.graph
        n = g.nodes.get(nid) if nid else None
        f = g.frames.get(nid) if nid else None
        if f:
            w = self._frame_page(f)
        elif not n:
            w, lay, _ = self._page("Node editor")
            self._note(lay, "Select a node to see its settings.\n\n"
                            "Shift+A adds a node. Drop .blend files here to add jobs.\n"
                            "Drag from a socket to connect. M mutes, X deletes, Ctrl+J puts nodes in a frame.\n\n"
                            "Settings nodes only change the fields you tick; everything else comes from the "
                            ".blend as saved. A lower node overrides the same field of a higher one.")
            lay.addStretch(1)
        elif n["type"] in SETTINGS_TYPES:
            w = self._settings_page(n)
        elif n["type"] == JOB:
            w = self._job_page(n)
        elif n["type"] == OUTPUT:
            w = self._output_page(n)
        else:
            w, lay, _ = self._page("Scheduler")
            self._note(lay, "Lists every output in render order. Select outputs in the list and push them "
                            "up or down; several selected outputs move as a block. The job that is rendering "
                            "finishes first.")
            lay.addStretch(1)
        old = self.takeWidget()
        if old is not None:
            old.deleteLater()
        self.setWidget(w)

    def _frame_page(self, f: dict):
        w, lay, form = self._page("Frame")
        name = QLineEdit(f.get("label", ""))
        name.editingFinished.connect(lambda: (f.__setitem__("label", name.text().strip() or "Frame"),
                                              self.ns.update(), self.ns.edited()))
        form.addRow("Label", name)
        col = QPushButton()
        col.setIcon(swatch(f.get("color")))
        col.setText(f.get("color"))

        def pick():
            c = QColorDialog.getColor(QColor(f.get("color") or "#5a4a3a"), self, "Frame colour")
            if c.isValid():
                f["color"] = c.name()
                col.setIcon(swatch(f["color"]))
                col.setText(f["color"])
                self.ns.update()
                self.ns.edited()
        col.clicked.connect(pick)
        form.addRow("Colour", col)
        self._note(lay, "Nodes whose centre is inside the frame move with it.")
        lay.addStretch(1)
        return w

    def _settings_page(self, n: dict):
        t = n["type"]
        w, lay, form = self._page(TITLES[t])
        label = QLineEdit(n["props"].get("label", ""))
        label.setPlaceholderText(TITLES[t])
        label.setToolTip("A name for this node, e.g. 'Final quality'")

        def set_label():
            n["props"]["label"] = label.text().strip()
            self._changed()
        label.editingFinished.connect(set_label)
        form.addRow("Name", label)
        self._note(lay, "Tick a field to override it. Unticked fields come from the .blend (Blender overrides) "
                        "or from the app's defaults (app behaviour)." if t == BLENDER else
                        "Tick a field to override the app's default for jobs below this node.")
        form2 = QFormLayout()
        lay.addLayout(form2)
        vals = n["props"].setdefault("values", {})
        hints = self._scene_hints()
        for key, (lab, kind, choices) in FIELDS[t].items():
            cb = QCheckBox(lab)
            ed = self._editor(key, kind, choices, vals.get(key), hints.get(key))
            ed.setEnabled(key in vals)
            cb.setChecked(key in vals)

            def toggled(on, key=key, ed=ed, kind=kind):
                ed.setEnabled(on)
                if on:
                    self.ns.graph.set_value(n["id"], key, self._read(ed, kind))
                else:
                    self.ns.graph.set_value(n["id"], key, None)
                self._changed()
            cb.toggled.connect(toggled)
            self._connect_editor(ed, kind, lambda v, key=key, cb=cb: (
                cb.isChecked() and (self.ns.graph.set_value(n["id"], key, v), self._changed())))
            form2.addRow(cb, ed)
        row = QHBoxLayout()
        b_save = QPushButton("Save as preset…")
        b_save.setToolTip("Save the selected settings nodes as a .bbrpreset file you can load into any queue")
        b_save.clicked.connect(lambda: self.ns.message.emit("preset-save", ""))
        row.addWidget(b_save)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _scene_hints(self) -> dict:
        cams, layers = set(), set()
        for j in self.ns.graph.of_type(JOB):
            for s in j["props"].get("scenes") or []:
                cams.update(s.get("cameras") or [])
                layers.update(s.get("view_layers") or [])
        return {"camera": sorted(cams), "view_layer": sorted(layers)}

    def _editor(self, key, kind, choices, value, hints):
        if kind == "int":
            ed = QSpinBox()
            lo, hi = INT_RANGES.get(key, (-1000000, 1000000))
            ed.setRange(lo, hi)
            ed.setValue(int(value) if value is not None else max(lo, min(hi, {"samples": 128, "resolution_pct": 100,
                                                                              "res_x": 1920, "res_y": 1080,
                                                                              "max_attempts": 3}.get(key, 1))))
        elif kind == "float":
            ed = QDoubleSpinBox()
            lo, hi, dec, step = FLOAT_RANGES.get(key, (0.0, 1e6, 2, 1.0))
            ed.setRange(lo, hi)
            ed.setDecimals(dec)
            ed.setSingleStep(step)
            ed.setValue(float(value) if value is not None else (0.01 if key == "noise_threshold" else 0.0))
        elif kind in ("bool", "bool3"):
            ed = QComboBox()
            ed.addItem("On", True)
            ed.addItem("Off", False)
            ed.setCurrentIndex(0 if value is None or value else 1)
        elif kind == "choice":
            ed = QComboBox()
            for c in choices or []:
                ed.addItem(c, c)
            if value is not None:
                ed.setCurrentIndex(max(0, ed.findData(value)))
        else:
            ed = QComboBox()
            ed.setEditable(True)
            for h in hints or []:
                ed.addItem(h, h)
            ed.setCurrentText(str(value or (hints[0] if hints else "")))
        return ed

    @staticmethod
    def _read(ed, kind):
        if isinstance(ed, QSpinBox):
            return ed.value()
        if isinstance(ed, QDoubleSpinBox):
            return ed.value()
        if isinstance(ed, QComboBox):
            if ed.isEditable():
                return ed.currentText().strip() or None
            return ed.currentData()
        return None

    def _connect_editor(self, ed, kind, cb):
        if isinstance(ed, (QSpinBox, QDoubleSpinBox)):
            ed.editingFinished.connect(lambda: cb(ed.value()))
        elif isinstance(ed, QComboBox) and ed.isEditable():
            ed.lineEdit().editingFinished.connect(lambda: cb(ed.currentText().strip() or None))
            ed.activated.connect(lambda _i: cb(ed.currentText().strip() or None))
        elif isinstance(ed, QComboBox):
            ed.activated.connect(lambda _i: cb(ed.currentData()))

    def _job_page(self, n: dict):
        p = n["props"]
        w, lay, form = self._page("Job")
        blend = QLineEdit(p.get("blend", ""))
        blend.setReadOnly(True)
        blend.setToolTip(p.get("blend", ""))
        b = QToolButton()
        b.setText("…")

        def choose():
            start = os.path.dirname(p.get("blend") or "") or os.path.expanduser("~")
            f, _ = QFileDialog.getOpenFileName(self, "Choose a .blend file", start, "Blender files (*.blend)")
            if f:
                self.ns.set_blend(n["id"], f)
                self.show_for(n["id"])
        b.clicked.connect(choose)
        row = QHBoxLayout()
        row.addWidget(blend, 1)
        row.addWidget(b)
        form.addRow("File", row)
        label = QLineEdit(p.get("label", ""))
        label.setPlaceholderText(os.path.basename(p.get("blend") or "") or "Job")

        def set_label():
            p["label"] = label.text().strip()
            self._changed()
        label.editingFinished.connect(set_label)
        form.addRow("Name", label)
        # Blender install
        bl = QComboBox()
        bl.addItem("Auto (version of the file)", "auto")
        for inst in self.ns.installs_provider():
            bl.addItem(inst.label, inst.path)
        bl.setCurrentIndex(max(0, bl.findData(p.get("blender") or "auto")))

        def set_blender(_i):
            p["blender"] = bl.currentData()
            self._changed()
        bl.activated.connect(set_blender)
        form.addRow("Blender", bl)
        # scenes
        sc = p.get("scenes") or []
        txt = ", ".join(s["name"] + (" (active)" if s.get("active") else "") for s in sc) or "not read yet"
        l_sc = QLabel(txt)
        l_sc.setWordWrap(True)
        b_sc = QPushButton("Read scenes")
        b_sc.setToolTip("Ask Blender which scenes the file has. Each scene gets its own output socket.")
        b_sc.clicked.connect(lambda: self.ns.probe_scenes(n["id"]))
        form.addRow("Scenes", l_sc)
        form.addRow("", b_sc)
        # machines and hardware
        dev = p.setdefault("devices", {})
        hdr = QLabel("Computers and hardware")
        hdr.setStyleSheet("font-weight: 600; margin-top: 8px;")
        lay.addWidget(hdr)
        self._note(lay, "Per computer: the file's device, GPU, CPU, or Off (this job never renders there). "
                        "Cycles only.")
        f2 = QFormLayout()
        lay.addLayout(f2)
        for name, desc in self.machines_provider():
            cb = QComboBox()
            for text, val in (("From file", ""), ("GPU", "gpu"), ("CPU", "cpu"), ("Off", "off")):
                cb.addItem(text, val)
            cb.setCurrentIndex(max(0, cb.findData(dev.get(name, ""))))
            cb.setToolTip(desc or name)

            def set_dev(_i, name=name, cb=cb):
                v = cb.currentData()
                if v:
                    dev[name] = v
                else:
                    dev.pop(name, None)
                self._changed()
            cb.activated.connect(set_dev)
            f2.addRow(name, cb)
        lay.addStretch(1)
        return w

    def _output_page(self, n: dict):
        p = n["props"]
        g = self.ns.graph
        w, lay, form = self._page("Output")
        name = QLineEdit(p.get("name", ""))
        name.setToolTip("Also the name of the job folder and of the frame files. Renaming after the job "
                        "was created keeps the existing folder.")

        def set_name():
            p["name"] = name.text().strip() or p.get("name") or "Output"
            self._changed()
        name.editingFinished.connect(set_name)
        form.addRow("Name", name)
        # render folder
        nxt = QCheckBox("Next to the .blend (render/)")
        nxt.setChecked(not p.get("render_root"))
        root = QLineEdit(p.get("render_root", ""))
        root.setEnabled(bool(p.get("render_root")))
        b = QToolButton()
        b.setText("…")
        b.setEnabled(bool(p.get("render_root")))

        def set_root():
            p["render_root"] = root.text().strip() if not nxt.isChecked() else ""
            self._changed()

        def browse():
            d = QFileDialog.getExistingDirectory(self, "Render folder", root.text() or os.path.expanduser("~"))
            if d:
                root.setText(d)
                set_root()

        def toggled(on):
            root.setEnabled(not on)
            b.setEnabled(not on)
            if on or root.text().strip():
                set_root()
        nxt.toggled.connect(toggled)
        root.editingFinished.connect(set_root)
        b.clicked.connect(browse)
        form.addRow("Render folder", nxt)
        row = QHBoxLayout()
        row.addWidget(root, 1)
        row.addWidget(b)
        form.addRow("", row)
        col = QPushButton(p.get("color", ""))
        col.setIcon(swatch(p.get("color")))

        def pick():
            c = QColorDialog.getColor(QColor(p.get("color") or "#3d7a3d"), self, "Output colour")
            if c.isValid():
                p["color"] = c.name()
                col.setIcon(swatch(p["color"]))
                col.setText(p["color"])
                self._changed()
        col.clicked.connect(pick)
        form.addRow("Colour", col)
        # effective values
        r = g.resolve(n["id"])
        hdr = QLabel("What this output renders")
        hdr.setStyleSheet("font-weight: 600; margin-top: 8px;")
        lay.addWidget(hdr)
        lines = []
        if r["problems"]:
            lines.append(f"<i>{r['problems'][0]}</i>")
        if r["job"]:
            lines.append(f"File: {os.path.basename(r['job']['props'].get('blend') or '–')}")
            lines.append(f"Scene: {r['scene'] or 'the active scene'}")
        file_scene = self._file_scene(r)
        for key, (lab, kind, _c) in list(BLENDER_FIELDS.items()) + list(FIELDS[APP].items()):
            if key in r["values"]:
                src = g.nodes.get(r["sources"].get(key), {})
                src_name = (src.get("props", {}).get("label") or TITLES.get(src.get("type"), ""))
                lines.append(f"{lab}: <b>{describe_value(kind, r['values'][key])}</b> "
                             f"<span style='color:gray'>({src_name})</span>")
            elif key in BLENDER_FIELDS:
                fv = self._file_value(file_scene, key)
                lines.append(f"<span style='color:gray'>{lab}: {fv if fv is not None else 'from file'}</span>")
        info = QLabel("<br>".join(lines))
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        lay.addWidget(info)
        st = self.ns.status_for(n)
        if st and st[0]:
            s = QLabel("Status: " + st[0])
            s.setWordWrap(True)
            lay.addWidget(s)
        b_open = QPushButton("Open render folder")
        b_open.setEnabled(bool(p.get("job_folder")))
        b_open.clicked.connect(lambda: self.ns.message.emit("open-folder", p.get("job_folder") or ""))
        lay.addWidget(b_open)
        lay.addStretch(1)
        return w

    @staticmethod
    def _file_scene(r: dict) -> Optional[dict]:
        j = r["job"]
        if not j:
            return None
        scenes = j["props"].get("scenes") or []
        if r["scene"]:
            return next((s for s in scenes if s["name"] == r["scene"]), None)
        return next((s for s in scenes if s.get("active")), scenes[0] if scenes else None)

    @staticmethod
    def _file_value(sc: Optional[dict], key: str):
        if not sc:
            return None
        m = {"frame_start": "frame_start", "frame_end": "frame_end", "frame_step": "frame_step",
             "resolution_pct": "res_pct", "res_x": "res_x", "res_y": "res_y", "camera": "camera"}
        return sc.get(m[key]) if key in m else None


# =============================================================================================
# the whole editor
# =============================================================================================
class NodeEditor(QWidget):
    """Node view + properties panel. The main window connects `changed` to the queue sync."""
    changed = Signal()
    moved = Signal()
    selection = Signal(list)

    def __init__(self, graph: Graph, installs_provider, machines_provider, parent=None):
        super().__init__(parent)
        self.scene = NodeScene(graph, self)
        self.scene.installs_provider = installs_provider
        self.view = NodeView(self.scene)
        self.view.add_menu_provider = self.add_menu
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.files_dropped.connect(lambda files, pos: self.scene.add_blends(files, pos))
        self.props = PropertiesPanel(self.scene, machines_provider)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.view)
        split.addWidget(self.props)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        b_add = QToolButton()
        b_add.setText("Add ▾")
        b_add.setToolTip("Add a node (Shift+A)")
        b_add.clicked.connect(lambda: self.add_menu(self.view.mapToScene(self.view.viewport().rect().center()))
                              .exec(b_add.mapToGlobal(b_add.rect().bottomLeft())))
        bar.addWidget(b_add)
        for text, tip, slot in (("Load preset…", "Insert a .bbrpreset file", self.load_preset),
                                ("Save preset…", "Save the selected settings nodes as a .bbrpreset file",
                                 self.save_preset),
                                ("Frame all", "Show every node (Home)", self.view.show_all)):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        self.hint = QLabel("Shift+A add · drag sockets to connect · M mute · X delete · Ctrl+J frame")
        self.hint.setStyleSheet("color: gray;")
        bar.addWidget(self.hint)
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        self.scene.changed_graph.connect(self.changed.emit)
        self.scene.moved_only.connect(self.moved.emit)
        self.scene.selection_nodes.connect(self._on_sel)
        self.scene.node_double_clicked.connect(self._double_clicked)
        self.scene.focus_node.connect(self.focus)
        self.scene.message.connect(self._scene_message)
        self.message_sink: Callable[[str, str], None] = lambda level, text: None
        self.open_folder: Callable[[str], None] = lambda path: None
        self._props_timer = QTimer(self)
        self._props_timer.setSingleShot(True)
        self._props_timer.setInterval(0)
        self._props_timer.timeout.connect(self._refresh_props)
        self._sel: List[str] = []
        self.apply_theme()
        self.scene.rebuild()
        QTimer.singleShot(0, self.view.show_all)

    # ---- api for the main window -------------------------------------------------------------------
    @property
    def graph(self) -> Graph:
        return self.scene.graph

    def set_graph(self, graph: Graph):
        self.scene.set_graph(graph)
        self.props.show_for(None)
        QTimer.singleShot(0, self.view.show_all)

    def reload(self):
        """The graph was changed outside the editor (adopted jobs, a sync): redraw it."""
        self.scene.rebuild()
        self._props_timer.start()

    def refresh_status(self):
        self.scene.refresh_status()

    def apply_theme(self):
        self.scene.dark = _theme_dark(self)
        self.scene.update()

    def focus(self, nid: str):
        it = self.scene.items_by_id.get(nid)
        if it:
            self.scene.select_nodes([nid])
            self.view.centerOn(it)

    def select_output_of_job(self, job_id: str):
        for o in self.graph.of_type(OUTPUT):
            if o["props"].get("job_id") == job_id:
                if o["id"] not in self._sel:
                    self.scene.select_nodes([o["id"]])
                return

    # ---- internals -------------------------------------------------------------------------------
    def _on_sel(self, ids: List[str]):
        self._sel = ids
        self.selection.emit(ids)
        self._props_timer.start()

    def _refresh_props(self):
        ids = [i for i in self._sel if i in self.graph.nodes or i in self.graph.frames]
        self.props.show_for(ids[0] if len(ids) == 1 else None)

    def _double_clicked(self, nid: str):
        n = self.graph.nodes.get(nid)
        if n and n["type"] == JOB:
            start = os.path.dirname(n["props"].get("blend") or "") or os.path.expanduser("~")
            f, _ = QFileDialog.getOpenFileName(self, "Choose a .blend file", start, "Blender files (*.blend)")
            if f:
                self.scene.set_blend(nid, f)
                self.props.show_for(nid)

    def _scene_message(self, level: str, text: str):
        if level == "preset-save":
            self.save_preset()
        elif level == "open-folder":
            self.open_folder(text)
        else:
            self.message_sink(level, text)

    def add_menu(self, pos: QPointF) -> QMenu:
        m = QMenu(self)
        for t in (BLENDER, APP, JOB, OUTPUT):
            a = m.addAction(TITLES[t])
            a.triggered.connect(lambda _=False, t=t: self.scene.add_node(t, pos))
        m.addSeparator()
        m.addAction("Job from .blend file…").triggered.connect(lambda: self._add_job_file(pos))
        m.addAction("Preset…").triggered.connect(lambda: self.load_preset(pos))
        if not self.graph.scheduler():
            m.addAction(TITLES[SCHEDULER]).triggered.connect(lambda: self.scene.add_node(SCHEDULER, pos))
        return m

    def _context_menu(self, vp_pos):
        pos = self.view.mapToScene(vp_pos)
        it = self.scene.itemAt(pos, QTransform())
        while it is not None and not isinstance(it, (NodeItem, FrameItem)):
            it = it.parentItem()
        m = QMenu(self)
        if isinstance(it, NodeItem):
            if not it.isSelected():
                self.scene.select_nodes([it.nid])
            m.addAction("Mute / unmute  (M)").triggered.connect(lambda: self.scene.toggle_mute())
            if it.node["type"] in SETTINGS_TYPES:
                m.addAction("Duplicate  (Ctrl+D)").triggered.connect(self.scene.duplicate_selected)
                m.addAction("Save as preset…").triggered.connect(self.save_preset)
            if it.node["type"] == JOB:
                m.addAction("Read scenes").triggered.connect(lambda: self.scene.probe_scenes(it.nid))
                m.addAction("Add output per scene").triggered.connect(lambda: self.output_per_scene(it.nid))
            m.addAction("Put in a frame  (Ctrl+J)").triggered.connect(self.scene.frame_selected)
            m.addSeparator()
            m.addAction("Delete  (X)").triggered.connect(self.scene.delete_selected)
        elif isinstance(it, FrameItem):
            it.setSelected(True)
            m.addAction("Delete frame (keeps the nodes)").triggered.connect(self.scene.delete_selected)
        else:
            sub = self.add_menu(pos)
            sub.setTitle("Add")
            m.addMenu(sub)
        m.exec(self.view.viewport().mapToGlobal(vp_pos))

    def _add_job_file(self, pos: QPointF):
        files, _ = QFileDialog.getOpenFileNames(self, "Add .blend files", os.path.expanduser("~"),
                                                "Blender files (*.blend)")
        if files:
            self.scene.add_blends(files, pos)

    def output_per_scene(self, job_id: str):
        """One output node for every scene socket that has none yet."""
        g = self.graph
        j = g.nodes[job_id]
        socks = g.outputs_of(j)
        used = {l["from"][1] for l in g.links_from(job_id)}
        x = j["x"] - (len(socks) - 1) * (NODE_W + 30) / 2
        for i, s in enumerate(socks):
            if s in used:
                continue
            o = g.add_node(OUTPUT, x + i * (NODE_W + 30), j["y"] + 180,
                           {"name": default_output_name(j["props"].get("blend"), socket_scene(s))})
            g.connect(job_id, s, o["id"])
        self.scene.rebuild()
        self.scene.edited()

    def save_preset(self):
        ids = [i for i in self._sel if i in self.graph.nodes]
        try:
            data = preset_io.extract(self.graph, ids)
        except GraphError as e:
            QMessageBox.information(self, "Save preset", str(e))
            return
        first = self.graph.nodes[ids[0]]["props"].get("label") or "preset"
        path, _ = QFileDialog.getSaveFileName(self, "Save preset", os.path.join(os.path.expanduser("~"),
                                              first + preset_io.PRESET_EXT),
                                              f"Batch Renderer preset (*{preset_io.PRESET_EXT})")
        if not path:
            return
        if not path.lower().endswith(preset_io.PRESET_EXT):
            path += preset_io.PRESET_EXT
        data["name"] = os.path.splitext(os.path.basename(path))[0]
        try:
            preset_io.save(path, data)
        except OSError as e:
            QMessageBox.warning(self, "Save preset", f"Could not save:\n{e}")
            return
        self.message_sink("info", f"Saved preset {os.path.basename(path)}")

    def load_preset(self, pos: Optional[QPointF] = None):
        path, _ = QFileDialog.getOpenFileName(self, "Load preset", os.path.expanduser("~"),
                                              f"Batch Renderer preset (*{preset_io.PRESET_EXT})")
        if not path:
            return
        try:
            data = preset_io.load(path)
        except (GraphError, OSError) as e:
            QMessageBox.warning(self, "Load preset", f"Could not load {os.path.basename(path)}:\n{e}")
            return
        if not isinstance(pos, QPointF):
            pos = self.view.mapToScene(self.view.viewport().rect().center())
        ids, _m = preset_io.insert(self.graph, data, pos.x(), pos.y())
        self.scene.rebuild()
        self.scene.select_nodes(ids)
        self.scene.edited()
        self.message_sink("info", f"Loaded preset {os.path.basename(path)}")
