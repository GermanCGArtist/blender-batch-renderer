"""Worker mode: this computer renders frames for a coordinator."""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QCheckBox, QLabel, QPushButton,
                               QLineEdit, QProgressBar, QPlainTextEdit, QTableWidget, QTableWidgetItem,
                               QHeaderView, QAbstractItemView, QGridLayout, QDoubleSpinBox, QMessageBox,
                               QSplitter, QComboBox)

from ..parse import fmt_duration, fmt_bytes
from ..worker import Worker, pair, normalize_server, CoordinatorError

STATE_TEXT = {"not paired": "Not paired", "offline": "Coordinator not reachable", "paused": "Paused",
              "idle": "Waiting for work", "downloading": "Downloading the job", "loading": "Opening the file",
              "rendering": "Rendering", "error": "Problem", "starting": "Starting…", "stopped": "Stopped"}


class WorkerView(QWidget):
    def __init__(self, settings, post, parent=None):
        """post(kind, payload) delivers results from background threads to the GUI thread."""
        super().__init__(parent)
        self.settings = settings
        self.post = post
        self.worker: Worker = None
        outer = QVBoxLayout(self)

        # ---- connection -----------------------------------------------------------
        g = QGroupBox("Coordinator")
        gl = QGridLayout(g)
        self.server = QComboBox()
        self.server.setEditable(True)
        self.server.setMinimumWidth(260)
        self.server.lineEdit().setPlaceholderText("address of the coordinator, e.g. 192.168.1.20")
        self.server.setEditText(settings.worker_server.replace("http://", ""))
        self.b_find = QPushButton("Find on network")
        self.b_find.clicked.connect(self._find)
        self.code = QLineEdit()
        self.code.setPlaceholderText("6-digit pairing code")
        self.code.setMaxLength(8)
        self.b_pair = QPushButton("Pair")
        self.b_pair.clicked.connect(self._pair)
        self.b_forget = QPushButton("Forget pairing")
        self.b_forget.clicked.connect(self._forget)
        self.name = QLineEdit(settings.worker_name)
        self.name.setPlaceholderText("name shown on the coordinator (default: computer name)")
        self.name.editingFinished.connect(self._name_changed)
        self.conn = QLabel()
        self.conn.setTextFormat(Qt.TextFormat.RichText)
        gl.addWidget(QLabel("Address"), 0, 0)
        gl.addWidget(self.server, 0, 1)
        gl.addWidget(self.b_find, 0, 2)
        gl.addWidget(QLabel("Pairing code"), 1, 0)
        gl.addWidget(self.code, 1, 1)
        row = QHBoxLayout()
        row.addWidget(self.b_pair)
        row.addWidget(self.b_forget)
        gl.addLayout(row, 1, 2)
        gl.addWidget(QLabel("This computer"), 2, 0)
        gl.addWidget(self.name, 2, 1, 1, 2)
        gl.addWidget(self.conn, 3, 0, 1, 3)
        outer.addWidget(g)

        # ---- control + now ---------------------------------------------------------
        g2 = QGroupBox("Rendering")
        g2l = QGridLayout(g2)
        self.accept = QCheckBox("Accept jobs from the coordinator")
        self.accept.setChecked(bool(getattr(settings, "worker_accepting", True)))
        self.accept.toggled.connect(self._accept_toggled)
        self.b_pause = QPushButton("Pause after this frame")
        self.b_pause.clicked.connect(lambda: self.worker and self.worker.set_accepting(False))
        self.b_stop = QPushButton("■  Stop now")
        self.b_stop.setToolTip("Drop the frame being rendered – the coordinator gives it to another machine")
        self.b_stop.clicked.connect(self._stop_now)
        self.lock_note = QLabel()
        self.lock_note.setStyleSheet("color: gray;")
        self.state = QLabel()
        self.state.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.bar = QProgressBar()
        self.l_detail = QLabel()
        self.l_detail.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.accept)
        row.addStretch(1)
        row.addWidget(self.b_pause)
        row.addWidget(self.b_stop)
        g2l.addLayout(row, 0, 0, 1, 2)
        g2l.addWidget(self.lock_note, 1, 0, 1, 2)
        g2l.addWidget(self.state, 2, 0, 1, 2)
        g2l.addWidget(self.note, 3, 0, 1, 2)
        g2l.addWidget(self.bar, 4, 0, 1, 2)
        g2l.addWidget(self.l_detail, 5, 0, 1, 2)
        outer.addWidget(g2)

        split = QSplitter(Qt.Orientation.Horizontal)
        # ---- statistics --------------------------------------------------------------
        g3 = QGroupBox("Statistics")
        g3l = QVBoxLayout(g3)
        self.stats = QLabel()
        self.stats.setTextFormat(Qt.TextFormat.RichText)
        self.stats.setWordWrap(True)
        g3l.addWidget(self.stats)
        self.jobs = QTableWidget(0, 4)
        self.jobs.setHorizontalHeaderLabels(["Job", "Frames", "Render time", "Avg frame"])
        self.jobs.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.jobs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs.verticalHeader().setVisible(False)
        g3l.addWidget(self.jobs, 1)
        split.addWidget(g3)
        # ---- this computer -------------------------------------------------------------
        g4 = QGroupBox("Blender on this computer")
        g4l = QVBoxLayout(g4)
        self.installs = QTableWidget(0, 2)
        self.installs.setHorizontalHeaderLabels(["Blender", "Enabled add-ons"])
        self.installs.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.installs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.installs.verticalHeader().setVisible(False)
        g4l.addWidget(self.installs, 1)
        r2 = QHBoxLayout()
        self.b_addons = QPushButton("Check add-ons again")
        self.b_addons.setToolTip("After installing or enabling an add-on in Blender")
        self.b_addons.clicked.connect(self._refresh_addons)
        r2.addWidget(self.b_addons)
        r2.addStretch(1)
        g4l.addLayout(r2)
        r3 = QHBoxLayout()
        self.cache = QLabel()
        self.cache_gb = QDoubleSpinBox()
        self.cache_gb.setRange(2, 10000)
        self.cache_gb.setDecimals(0)
        self.cache_gb.setSuffix(" GB")
        self.cache_gb.setValue(float(settings.worker_cache_gb))
        self.cache_gb.setToolTip("Downloaded jobs are kept so the next frames start right away. "
                                 "The oldest are deleted when this size is reached.")
        self.cache_gb.valueChanged.connect(self._cache_changed)
        self.b_clear = QPushButton("Clear")
        self.b_clear.clicked.connect(self._clear_cache)
        r3.addWidget(self.cache, 1)
        r3.addWidget(QLabel("keep up to"))
        r3.addWidget(self.cache_gb)
        r3.addWidget(self.b_clear)
        g4l.addLayout(r3)
        split.addWidget(g4)
        outer.addWidget(split, 1)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.console.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.console.setMaximumBlockCount(5000)
        self.console.setMinimumHeight(140)
        outer.addWidget(self.console, 1)
        self._last_cache = 0.0
        self._cache_text = ""

    # ---- lifecycle ----------------------------------------------------------------------
    def start(self, emit):
        if self.worker is None:
            self.worker = Worker(self.settings, emit)
        self.worker.start()
        self.refresh()

    def stop(self, now: bool = True):
        if self.worker:
            self.worker.stop(now=now)
        self.refresh()

    @property
    def busy(self) -> bool:
        return bool(self.worker and self.worker.busy)

    # ---- actions ------------------------------------------------------------------------
    def _find(self):
        self.b_find.setEnabled(False)
        self.conn.setText("Looking for coordinators on the home network…")

        def work():
            from ..workerapi import discover
            self.post("worker_found", {"found": discover()})
        threading.Thread(target=work, daemon=True).start()

    def found(self, found: list):
        self.b_find.setEnabled(True)
        self.server.clear()
        for f in found:
            self.server.addItem(f"{f['url'].replace('http://', '')}   ({f['name']})", f["url"])
        if found:
            self.server.setCurrentIndex(0)
            self.conn.setText(f"Found {len(found)} coordinator(s). Enter the pairing code shown on it, then Pair.")
        else:
            self.conn.setText("No coordinator found. Check that sharing is on there (Machines tab), or type its "
                              "address.")

    def _server_text(self) -> str:
        i = self.server.currentIndex()
        if i >= 0 and self.server.itemData(i) and self.server.currentText() == self.server.itemText(i):
            return self.server.itemData(i)
        return normalize_server(self.server.currentText().split()[0] if self.server.currentText().strip() else "")

    def _pair(self):
        server = self._server_text()
        code = self.code.text().strip()
        if not server or not code:
            QMessageBox.information(self, "Pair", "Enter the coordinator's address and its pairing code "
                                    "(Machines tab on the coordinator).")
            return
        self.b_pair.setEnabled(False)
        self.conn.setText(f"Pairing with {server}…")

        def work():
            try:
                d = pair(server, code, self.settings.worker_id, self.settings.worker_name or "")
                self.post("worker_paired", {"server": server, "key": d["key"], "name": d.get("coordinator")})
            except CoordinatorError as e:
                self.post("worker_paired", {"error": e.message})
        threading.Thread(target=work, daemon=True).start()

    def paired(self, p: dict):
        self.b_pair.setEnabled(True)
        if p.get("error"):
            self.conn.setText(f"<span style='color:#dc3545'>Pairing failed: {p['error']}</span>")
            return
        self.settings.worker_server, self.settings.worker_key = p["server"], p["key"]
        self.settings.save()
        self.code.clear()
        if self.worker:
            self.worker.registered = False
            self.worker._wake.set()
        self.conn.setText(f"Paired with <b>{p.get('name')}</b>.")
        self.refresh()

    def _forget(self):
        if self.busy:
            return
        if self.worker:
            self.worker.forget_pairing()
        else:
            self.settings.worker_key = ""
            self.settings.save()
        self.refresh()

    def _name_changed(self):
        self.settings.worker_name = self.name.text().strip()
        self.settings.save()
        if self.worker:
            self.worker.registered = False

    def _accept_toggled(self, on: bool):
        if self.worker:
            self.worker.set_accepting(on)
        else:
            self.settings.worker_accepting = on
            self.settings.save()
        self.refresh()

    def _stop_now(self):
        if self.worker:
            self.worker.stop_now()
        self.refresh()

    def _refresh_addons(self):
        if not self.worker or self.busy:
            return
        self.b_addons.setEnabled(False)
        self.b_addons.setText("Checking…")

        def work():
            self.worker.refresh_installs(force=True)
            self.post("worker_addons", {})
        threading.Thread(target=work, daemon=True).start()

    def addons_done(self):
        self.b_addons.setText("Check add-ons again")
        self.refresh()

    def _cache_changed(self, v):
        self.settings.worker_cache_gb = float(v)
        self.settings.save()

    def _clear_cache(self):
        if self.worker and not self.busy:
            self.worker.clear_cache()
            self._last_cache = 0
        self.refresh()

    def append_console(self, lines):
        if lines:
            self.console.appendPlainText("\n".join(lines))

    # ---- display --------------------------------------------------------------------------
    def refresh(self):
        w = self.worker
        st = w.status() if w else {"state": "stopped", "note": "", "accepting": self.accept.isChecked(),
                                   "paired": bool(self.settings.worker_key), "current": None, "live": {},
                                   "session": {}, "total": self.settings.worker_stats or {}, "installs": [],
                                   "coordinator": "", "uploads_waiting": 0}
        busy = self.busy
        rendering = st["current"] is not None
        # toggles are locked while a job is running
        for wdg in (self.accept, self.server, self.b_find, self.code, self.b_pair, self.b_forget, self.name,
                    self.b_addons, self.b_clear, self.cache_gb):
            wdg.setEnabled(not busy)
        if self.b_addons.text() == "Checking…":
            self.b_addons.setEnabled(False)
        self.accept.blockSignals(True)
        self.accept.setChecked(st["accepting"])
        self.accept.blockSignals(False)
        self.b_pause.setEnabled(rendering and st["accepting"])
        self.b_stop.setEnabled(rendering)
        self.lock_note.setText("Settings are locked while a job is running – use Pause or Stop." if busy else "")

        if st["paired"]:
            name = st.get("coordinator") or self.settings.worker_server
            ok = st["state"] not in ("offline", "not paired", "error", "stopped")
            self.conn.setText(("<span style='color:#2fa84f'>●</span> Connected to " if ok else
                               "<span style='color:#888'>●</span> Paired with ") + f"<b>{name}</b>"
                              + f"  ({self.settings.worker_server})")
        elif not self.conn.text().startswith(("Found", "Looking", "Pairing", "No coordinator", "<span style='color:#dc")):
            self.conn.setText("Not paired yet: find the coordinator (or type its address), enter its pairing code "
                              "and press Pair.")
        self.state.setText(STATE_TEXT.get(st["state"], st["state"]))
        self.note.setText(st.get("note") or "")

        cur = st["current"] or {}
        L = st["live"] or {}
        if L.get("download_of"):
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * L.get("download", 0) / max(1, L["download_of"])))
            self.bar.setFormat(f"{fmt_bytes(L.get('download', 0))} / {fmt_bytes(L['download_of'])}")
        elif L.get("frame") is not None and L.get("samples"):
            self.bar.setRange(0, int(L["samples"]))
            self.bar.setValue(int(L.get("sample") or 0))
            self.bar.setFormat(f"frame {L['frame']} · sample %v/%m")
        elif rendering:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
            self.bar.setFormat("")
        det = []
        if cur:
            det.append(f"Job: <b>{cur.get('job')}</b>")
        if L.get("frame") is not None:
            det.append(f"frame {L['frame']}")
            if L.get("since"):
                det.append(f"{fmt_duration(time.time() - L['since'])} so far")
            if L.get("remaining_s") is not None:
                det.append(f"{fmt_duration(L['remaining_s'])} left (Blender's estimate)")
        if L.get("mem_mb"):
            det.append(f"memory {fmt_bytes(L['mem_mb'] * 1048576)}")
        if L.get("device"):
            det.append(str(L["device"]))
        if st.get("uploads_waiting"):
            det.append(f"{st['uploads_waiting']} frame(s) being sent")
        self.l_detail.setText(" · ".join(det))
        self.l_detail.setTextFormat(Qt.TextFormat.RichText)

        s, t = st.get("session") or {}, st.get("total") or {}

        def line(d, title):
            fr = d.get("frames", 0)
            avg = (d.get("render_s", 0) / fr) if fr else None
            return (f"<b>{title}:</b> {fr} frames · {fmt_duration(d.get('render_s', 0))} rendering"
                    + (f" · avg {fmt_duration(avg)}" if avg else "")
                    + (f" · {d.get('failed')} failed" if d.get("failed") else "")
                    + (f" · {fmt_bytes(d.get('bytes'))} sent" if d.get("bytes") else ""))
        self.stats.setText(line(s, "This session") + "<br>" + line(t, "All time"))
        per = (t.get("per_job") or {})
        self.jobs.setRowCount(len(per))
        for r, (name, d) in enumerate(sorted(per.items(), key=lambda kv: -kv[1].get("frames", 0))):
            fr = d.get("frames", 0)
            for c, v in enumerate((name, str(fr), fmt_duration(d.get("render_s", 0)),
                                   fmt_duration(d.get("render_s", 0) / fr) if fr else "–")):
                self.jobs.setItem(r, c, QTableWidgetItem(v))

        inst = st.get("installs") or []
        self.installs.setRowCount(len(inst))
        for r, i in enumerate(inst):
            ads = sorted(f"{a.get('name') or k} {a.get('version') or ''}".strip()
                         for k, a in (i.get("addons") or {}).items() if not a.get("bundled"))
            txt = ", ".join(ads) or "–"
            if i.get("addons_error"):
                txt += f"   (could not check: {i['addons_error']})"
            self.installs.setItem(r, 0, QTableWidgetItem(i.get("label") or i.get("version")))
            self.installs.setItem(r, 1, QTableWidgetItem(txt))
        if not inst and not self.settings.installs:
            self.installs.setRowCount(1)
            self.installs.setItem(0, 0, QTableWidgetItem("none"))
            self.installs.setItem(0, 1, QTableWidgetItem("Add Blender under Settings → Blender"))
        if w and time.time() - self._last_cache > 30:
            self._last_cache = time.time()
            self._cache_text = f"Downloaded jobs: {fmt_bytes(w.cache_usage())}"
        self.cache.setText(self._cache_text)
