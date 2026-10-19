"""The queue as a tree: folders containing jobs, with checkboxes and drag & drop."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QAbstractItemView, QHeaderView, QProgressBar

ROLE = Qt.ItemDataRole.UserRole


class QueueTree(QTreeWidget):
    COLS = ["Job", "Status", "Progress", "Frames", "Blender", "ETA"]

    order_changed = Signal(list, list)          # top-level job ids, [(group id, [job ids])]
    files_dropped = Signal(list, str)           # .blend paths, group id
    check_changed = Signal(str, str, bool)      # "j"/"g", id, checked
    expanded_changed = Signal(str, bool)        # group id, expanded

    def __init__(self):
        super().__init__()
        self.setColumnCount(len(self.COLS))
        self.setHeaderLabels(self.COLS)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        hh = self.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(self.COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.job_items: Dict[str, QTreeWidgetItem] = {}
        self.group_items: Dict[str, QTreeWidgetItem] = {}
        self._building = False
        self.itemChanged.connect(self._item_changed)
        self.itemExpanded.connect(lambda it: self._expanded(it, True))
        self.itemCollapsed.connect(lambda it: self._expanded(it, False))

    # ---- building ---------------------------------------------------------
    def _make_bar(self, item):
        bar = QProgressBar()
        bar.setMaximumHeight(16)
        bar.setRange(0, 1000)
        bar.setTextVisible(True)
        self.setItemWidget(item, 2, bar)

    def _job_item(self, job, parent) -> QTreeWidgetItem:
        it = QTreeWidgetItem(parent)
        it.setData(0, ROLE, ("j", job.id))
        it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDragEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(0, Qt.CheckState.Checked if job.enabled else Qt.CheckState.Unchecked)
        self.job_items[job.id] = it
        self._make_bar(it)
        return it

    def rebuild(self, queue, select: Optional[Tuple[str, str]] = None):
        self._building = True
        try:
            self.clear()
            self.job_items, self.group_items = {}, {}
            for j in queue.jobs_in(""):
                self._job_item(j, self)
            bold = QFont(self.font())
            bold.setBold(True)
            for g in queue.groups:
                gi = QTreeWidgetItem(self)
                gi.setData(0, ROLE, ("g", g["id"]))
                gi.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDragEnabled
                            | Qt.ItemFlag.ItemIsDropEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                gi.setCheckState(0, Qt.CheckState.Checked if g.get("enabled", True) else Qt.CheckState.Unchecked)
                for c in range(len(self.COLS)):
                    gi.setFont(c, bold)
                self.group_items[g["id"]] = gi
                self._make_bar(gi)
                for j in queue.jobs_in(g["id"]):
                    self._job_item(j, gi)
                gi.setExpanded(bool(g.get("expanded", True)))
        finally:
            self._building = False
        if select:
            self.select(*select)

    def select(self, kind: str, ident: str):
        it = (self.job_items if kind == "j" else self.group_items).get(ident)
        if it:
            self.setCurrentItem(it)

    def selected(self) -> Optional[Tuple[str, str]]:
        it = self.currentItem()
        return it.data(0, ROLE) if it else None

    @staticmethod
    def signature(queue):
        return (tuple((g["id"], g["name"]) for g in queue.groups),
                tuple((j.id, j.group or "") for j in queue.ordered_jobs()))

    # ---- signals -----------------------------------------------------------
    def _item_changed(self, item, col):
        if self._building or col != 0:
            return
        d = item.data(0, ROLE)
        if d:
            self.check_changed.emit(d[0], d[1], item.checkState(0) == Qt.CheckState.Checked)

    def _expanded(self, item, state):
        if self._building:
            return
        d = item.data(0, ROLE)
        if d and d[0] == "g":
            self.expanded_changed.emit(d[1], state)

    # ---- drag & drop ------------------------------------------------------
    @staticmethod
    def _blend_urls(md) -> List[str]:
        if not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.toLocalFile().lower().endswith(".blend")]

    def dragEnterEvent(self, e):
        if self._blend_urls(e.mimeData()):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if self._blend_urls(e.mimeData()):
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        files = self._blend_urls(e.mimeData())
        if files:
            it = self.itemAt(e.position().toPoint())
            gid = ""
            while it is not None:
                d = it.data(0, ROLE)
                if d and d[0] == "g":
                    gid = d[1]
                    break
                it = it.parent()
            e.acceptProposedAction()
            self.files_dropped.emit(files, gid)
            return
        super().dropEvent(e)
        # rebuild only after Qt has completely finished the drag
        QTimer.singleShot(0, self._emit_order)

    def _emit_order(self):
        """Read the order back from the tree. Jobs belong to their nearest folder;
        a folder dropped into another folder simply stays a top-level folder."""
        top: List[str] = []
        groups: List[tuple] = []
        gjobs: Dict[str, list] = {}

        def walk(item, gid):
            for i in range(item.childCount()):
                ch = item.child(i)
                d = ch.data(0, ROLE)
                if not d:
                    continue
                if d[0] == "g":
                    gjobs[d[1]] = []
                    groups.append((d[1], gjobs[d[1]]))
                    walk(ch, d[1])
                else:
                    (gjobs[gid] if gid else top).append(d[1])
                    walk(ch, gid)   # a job dropped onto a job
        walk(self.invisibleRootItem(), "")
        self.order_changed.emit(top, groups)
