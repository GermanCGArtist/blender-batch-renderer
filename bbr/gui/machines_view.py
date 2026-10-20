"""Coordinator: the computers taking part, with per-machine and combined statistics."""
from __future__ import annotations

import secrets
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QCheckBox, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMenu,
                               QMessageBox, QGridLayout)

from ..parse import fmt_duration, fmt_bytes

STATE_COLORS = {"rendering": "#3d8bfd", "downloading": "#c678dd", "loading": "#7aa2f7", "idle": "#2fa84f",
                "paused": "#e67e22", "error": "#dc3545", "offline": "#888888"}
COLS = ["On", "Machine", "State", "Working on", "Device", "Blender", "Frames", "Avg frame",
        "Failed", "Data", "All time"]
COL_TIPS = ["Off: this computer gets no frames (when no batch is running)", "", "What the computer is doing now",
            "Job and frame it renders, or why it is idle", "GPU or CPU it renders with",
            "Blender versions and add-ons installed there", "Frames delivered in this batch",
            "Average render time per frame in this batch", "Failed attempts in this batch",
            "Data sent in this batch (measured transfer speed)", "Frames and render time since it was paired"]


def _state_text(m: dict) -> str:
    if not m["online"]:
        return "offline"
    return m.get("state") or "idle"


class MachinesPanel(QWidget):
    settings_changed = Signal()

    def __init__(self, runner, settings, api, parent=None):
        super().__init__(parent)
        self.runner = runner
        self.sched = runner.scheduler
        self.settings = settings
        self.api = api
        lay = QVBoxLayout(self)

        g = QGroupBox("Sharing")
        gl = QGridLayout(g)
        self.share = QCheckBox("Share jobs with workers")
        self.share.setChecked(settings.share_with_workers)
        self.share.setToolTip("Other computers on your home network (in Worker mode) render frames of this queue. "
                              "They get the files they need from here and send the finished frames back.")
        self.local = QCheckBox("This computer renders too")
        self.local.setChecked(settings.render_locally)
        self.local.setToolTip("Off: this computer only hands out frames and collects them")
        self.code = QLabel()
        self.code.setStyleSheet("font-size: 18px; font-weight: 600; letter-spacing: 3px;")
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        new_code = QPushButton("New code")
        new_code.setToolTip("Each code works for one pairing and changes afterwards. Already paired workers keep "
                            "working; to lock one out, right-click it → Unpair and remove.")
        new_code.clicked.connect(self._new_code)
        self.addr = QLabel()
        self.addr.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.addr.setWordWrap(True)
        gl.addWidget(self.share, 0, 0, 1, 3)
        gl.addWidget(self.local, 1, 0, 1, 3)
        plab = QLabel("Pairing code")
        plab.setToolTip("A new worker enters this code once. Each code works for one pairing and then changes.")
        self.code.setToolTip(plab.toolTip())
        gl.addWidget(plab, 2, 0)
        gl.addWidget(self.code, 2, 1)
        gl.addWidget(new_code, 2, 2)
        gl.addWidget(self.addr, 3, 0, 1, 3)
        self.lock_note = QLabel()
        self.lock_note.setStyleSheet("color: gray;")
        gl.addWidget(self.lock_note, 4, 0, 1, 3)
        lay.addWidget(g)
        self.share.toggled.connect(self._toggled)
        self.local.toggled.connect(self._toggled)

        self.combined = QLabel()
        self.combined.setTextFormat(Qt.TextFormat.RichText)
        self.combined.setWordWrap(True)
        lay.addWidget(self.combined)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        for i, tip in enumerate(COL_TIPS):
            if tip:
                self.table.horizontalHeaderItem(i).setToolTip(tip)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.itemChanged.connect(self._item_changed)
        lay.addWidget(self.table, 1)
        brow = QHBoxLayout()
        self.b_pause = QPushButton("Pause after frame")
        self.b_pause.setToolTip("The selected computer finishes and delivers its current frame, then takes no "
                                "more. Its other frames go to the other computers.")
        self.b_stop = QPushButton("Stop now")
        self.b_stop.setToolTip("The selected computer drops its frame right away and pauses; the frame goes to "
                               "another computer. Nothing is counted as failed.")
        self.b_resume = QPushButton("Resume")
        self.b_resume.setToolTip("The selected computer takes frames again")
        for b, cmd in ((self.b_pause, "pause"), (self.b_stop, "stop"), (self.b_resume, "resume")):
            b.clicked.connect(lambda _=False, c=cmd: self._command_selected(c))
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        hint = QLabel("Workers: open the app on the other computer, switch it to <b>Worker</b> mode (top right) "
                      "and enter the pairing code. Each worker needs the same Blender version as the job and "
                      "the add-ons the job uses (e.g. LensSim) – they are not copied. When your firewall asks, "
                      "allow incoming connections for the app.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        lay.addWidget(hint)
        self._filling = False
        self.refresh()

    # ---- settings -------------------------------------------------------------------
    def _toggled(self):
        if self._filling:
            return
        changed_share = self.share.isChecked() != self.settings.share_with_workers
        self.settings.share_with_workers = self.share.isChecked()
        self.settings.render_locally = self.local.isChecked()
        self.settings.save()
        if changed_share:
            self.settings_changed.emit()      # the window restarts the server (home network on/off)
        self.refresh()

    def _new_code(self):
        self.settings.pairing_code = "".join(secrets.choice("0123456789") for _ in range(6))
        self.settings.save()
        self.refresh()

    def _addresses(self) -> str:
        if not (self.api and self.api.running):
            return "The status page / API server is off (Settings → Status page & API) – workers cannot connect."
        urls = [u.split("/?")[0] for u in self.api.lan_urls()]
        if not urls:
            return "No home-network address found."
        return "Workers connect to: " + ",  ".join(urls) + "  (found automatically on the same network)"

    # ---- table ----------------------------------------------------------------------
    def refresh(self):
        self._filling = True
        try:
            running = self.runner.running
            self.share.setChecked(self.settings.share_with_workers)
            self.local.setChecked(self.settings.render_locally)
            self.share.setEnabled(not running)
            self.local.setEnabled(not running)
            self.lock_note.setText("Locked while the batch is running." if running else "")
            self.code.setText(self.settings.pairing_code if self.settings.share_with_workers else "–")
            self.addr.setText(self._addresses() if self.settings.share_with_workers else
                              "Switch on sharing to let other computers help.")
            pub = self.sched.machines_public()
            ms = sorted(pub["machines"], key=lambda m: (m["kind"] != "local", m["name"].lower()))
            if not self.settings.share_with_workers:
                ms = [m for m in ms if m["kind"] == "local"]
            self.table.setRowCount(len(ms))
            for r, m in enumerate(ms):
                self._fill_row(r, m, running)
            self.table.resizeColumnsToContents()
            self._fill_combined(pub, ms)
            self._update_buttons()
        finally:
            self._filling = False

    def _fill_row(self, r: int, m: dict, running: bool):
        local = m["kind"] == "local"
        on = QTableWidgetItem()
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if not local and not running:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        on.setFlags(flags)
        enabled = self.settings.render_locally if local else m["enabled"]
        on.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        on.setData(Qt.ItemDataRole.UserRole, m["id"])
        state = _state_text(m)
        live = m.get("live") or {}
        working = ""
        if live.get("job"):
            working = live["job"]
            if live.get("frame") is not None:
                working += f" · frame {live['frame']}"
                if live.get("samples"):
                    working += f" ({live.get('sample') or 0}/{live['samples']})"
                if live.get("remaining_s") is not None:
                    working += f" · {fmt_duration(live['remaining_s'])} left"
            elif live.get("download_of"):
                working += f" · downloading {int(100 * live.get('download', 0) / max(1, live['download_of']))}%"
        elif m.get("note") and state not in ("rendering",):
            working = m["note"]
        excl = m.get("excluded_jobs") or {}
        if excl and not working:
            working = "; ".join(excl.values())
        blender = []
        for i in m.get("installs") or []:
            ads = sorted((a.get("name") or k) for k, a in (i.get("addons") or {}).items()
                         if not a.get("bundled"))
            blender.append(f"{i.get('version')}" + (f" ({', '.join(ads)})" if ads else ""))
        b = m["batch"]
        t = m["total"]
        vals = [None,
                m["name"] + ("  (this computer)" if local else ""),
                state,
                working,
                m.get("gpu") or "",
                "; ".join(blender) if not local else "",
                str(b["frames"]),
                fmt_duration(b["avg_frame_s"]) if b.get("avg_frame_s") else "–",
                str(b["failed"]) if b["failed"] else "",
                (fmt_bytes(b["bytes"]) if b["bytes"] else "")
                + (f"  ({fmt_bytes(m['rate'])}/s)" if m.get("rate") else ""),
                f"{t['frames']} frames, {fmt_duration(t['render_s'])}" if t["frames"] else ""]
        self.table.setItem(r, 0, on)
        for c, v in enumerate(vals[1:], 1):
            it = QTableWidgetItem(v)
            if c == 2:
                it.setForeground(QColor(STATE_COLORS.get(state, "#888888")))
            if c == 3 and excl:
                it.setToolTip("\n".join(excl.values()))
            self.table.setItem(r, c, it)

    def _fill_combined(self, pub: dict, ms: list):
        comb = pub["combined"]["batch"]
        rendering = [m for m in ms if _state_text(m) == "rendering"]
        speed = 0.0
        for m in ms:
            if m["online"] and m["batch"].get("avg_frame_s") and (m["kind"] != "local" or self.settings.render_locally):
                speed += 3600.0 / m["batch"]["avg_frame_s"]
        parts = [f"<b>{len([m for m in ms if m['online']])}</b> online",
                 f"<b>{len(rendering)}</b> rendering",
                 f"this batch: <b>{comb['frames']}</b> frames"]
        if comb.get("avg_frame_s"):
            parts.append(f"average {fmt_duration(comb['avg_frame_s'])} per frame per machine")
        if speed:
            parts.append(f"together ≈ <b>{speed:.0f}</b> frames/hour")
        if comb["failed"]:
            parts.append(f"<span style='color:#dc3545'>{comb['failed']} failed attempts</span>")
        if comb["bytes"]:
            parts.append(fmt_bytes(comb["bytes"]))
        self.combined.setText("All machines: " + " · ".join(parts))

    def _selected_mid(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        it = self.table.item(rows.pop(), 0)
        mid = it.data(Qt.ItemDataRole.UserRole) if it else None
        return None if mid == "local" else mid

    def _update_buttons(self):
        mid = self._selected_mid()
        m = self.sched.machines.get(mid) if mid else None
        ok = bool(m and m.online)
        for b in (self.b_pause, self.b_stop, self.b_resume):
            b.setEnabled(ok)

    def _command_selected(self, cmd):
        mid = self._selected_mid()
        if mid:
            self.sched.command(mid, cmd)
            self.refresh()

    def _item_changed(self, it):
        if self._filling or it.column() != 0:
            return
        mid = it.data(Qt.ItemDataRole.UserRole)
        on = it.checkState() == Qt.CheckState.Checked
        if mid == "local":
            self.settings.render_locally = on
            self.settings.save()
        else:
            self.sched.set_enabled(mid, on)
        self.refresh()

    def _menu(self, pos):
        it = self.table.itemAt(pos) if pos is not None else None
        if it is None:
            return
        mid = self.table.item(it.row(), 0).data(Qt.ItemDataRole.UserRole)
        if mid == "local":
            return
        m = self.sched.machines.get(mid)
        menu = QMenu(self)
        acts = {}
        for text, cmd in (("Pause after frame", "pause"), ("Stop now", "stop"), ("Resume", "resume")):
            acts[menu.addAction(text)] = cmd
        menu.addSeparator()
        a = menu.addAction("Unpair and remove this machine")
        a.setEnabled(not self.runner.running)
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen in acts:
            self.sched.command(mid, acts[chosen])
            self.refresh()
            return
        if chosen is a:
            if QMessageBox.question(self, "Unpair machine",
                                    f"Remove {m.name if m else mid}? Its key stops working right away; to render "
                                    "for this computer again it has to pair again with the current pairing code."
                                    ) == QMessageBox.StandardButton.Yes:
                self.sched.forget(mid)
                self.refresh()


def job_machines_text(sched, job) -> str:
    """'PC 30 (avg 12 s) · MacBook 10 (avg 41 s)' for the Live tab."""
    stats = sched.job_machine_stats(job)
    if (not stats or (len(stats) == 1 and stats[0]["machine"] == sched.local.name and not stats[0]["failed_attempts"])) \
            and not job.approvals and not any(k[0] == job.id for k in sched.probes):
        return ""
    out = []
    for d in stats:
        t = f"{d['machine']} {d['frames']}"
        if d.get("avg_frame_s"):
            t += f" (avg {fmt_duration(d['avg_frame_s'])})"
        if d["failed_attempts"]:
            t += f", {d['failed_attempts']} failed"
        out.append(t)
    with sched.lock:
        now = [f"{sched.machines[l.machine_id].name}: {l.frame}" for l in sched.leases.values()
               if l.job_id == job.id and l.machine_id in sched.machines]
    s = " · ".join(out)
    for name, ap in sorted((job.approvals or {}).items()):
        if ap.get("ok") is False and job.settings.consistency_check:
            s += f"\n{name} left out: {ap.get('reason')}"
    with sched.lock:
        probing = [p["machine"] + " (" + p["state"] + ")" for (jid, _m), p in sched.probes.items() if jid == job.id]
    if probing:
        s += "\ntest frame: " + ", ".join(probing)
    if now:
        s += "   — rendering now: " + ", ".join(sorted(now))
    return s
