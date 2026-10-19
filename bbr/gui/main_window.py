"""The main window."""
from __future__ import annotations

import collections
import os
import time
from typing import Dict, Optional

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFontDatabase, QKeySequence, QTextCursor
from PySide6.QtWidgets import (QMainWindow, QWidget, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QLabel, QProgressBar,
                               QPlainTextEdit, QToolBar, QFileDialog, QMessageBox, QHeaderView,
                               QAbstractItemView, QPushButton, QCheckBox, QMenu, QListWidget,
                               QListWidgetItem, QStatusBar, QSizePolicy)

from .. import APP_NAME, __version__
from ..blendfile import version_str
from ..installs import BlenderInstall, scan
from ..jobqueue import JobQueue
from ..models import (AppSettings, Job, JobSettings, QUEUED, RENDERING, STOPPED, DONE, DONE_ERRORS,
                      FAILED, CANCELLED, F_DONE, F_FAILED, RUNNABLE)
from ..parse import fmt_duration, fmt_bytes
from ..runner import Runner
from .dialogs import SettingsDialog, AddJobsDialog
from .widgets import JobSettingsForm, Thumbnail, LineChart

STATUS_COLORS = {RENDERING: "#3d8bfd", DONE: "#2fa84f", DONE_ERRORS: "#d39e00", FAILED: "#dc3545",
                 CANCELLED: "#888888", STOPPED: "#e67e22"}


class Bridge(QObject):
    """Carries engine events from the render thread into the GUI thread."""
    event = Signal(str, object)


def clock(ts: float) -> str:
    t = time.localtime(ts)
    same_day = time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M" if same_day else "%a %H:%M", t)


class MainWindow(QMainWindow):
    COLS = ["Job", "Status", "Progress", "Frames", "Blender", "ETA"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1400, 900)
        self.setAcceptDrops(True)

        self.settings = AppSettings.load()
        self.queue = JobQueue()
        load_warnings = self.queue.load()
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event, Qt.ConnectionType.QueuedConnection)
        self.runner = Runner(self.queue, self.settings, lambda k, p: self.bridge.event.emit(k, p))

        self.console_buf: Dict[str, collections.deque] = {}
        self.pending_console: list = []
        self.selected_id: Optional[str] = None
        self._quit_when_idle = False
        self._row_ids: list = []

        self._build_actions()
        self._build_ui()
        self._refresh_table(full=True)
        self._update_actions()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self.console_timer = QTimer(self)
        self.console_timer.timeout.connect(self._flush_console)
        self.console_timer.start(200)

        for w in load_warnings:
            self._add_message("warn", w)
        QTimer.singleShot(300, self._first_run)

    # ======================================================================
    # UI construction
    # ======================================================================
    def _build_actions(self):
        def act(text, slot, shortcut=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
                a.setStatusTip(tip)
            return a

        self.a_add = act("Add .blend…", self.add_files, "Ctrl+O", "Add .blend files to the queue (or drop them on the window)")
        self.a_remove = act("Remove", self.remove_job, None, "Remove the selected job from the queue (files stay on disk)")
        self.a_up = act("▲", lambda: self.move_job(-1), None, "Move job up")
        self.a_down = act("▼", lambda: self.move_job(1), None, "Move job down")
        self.a_start = act("▶  Start", self.start_batch, "Ctrl+R", "Render the queue (resumes where it stopped)")
        self.a_stop_after = act("Stop after frame", self.stop_after_frame, None,
                                "Let the current frame finish, then stop")
        self.a_stop_now = act("■  Stop now", self.stop_now, "Ctrl+.",
                              "Kill Blender now - the current frame is rendered again next time")
        self.a_cancel = act("Cancel job", self.cancel_job, None,
                            "Stop the current job, mark it cancelled and continue with the next one")
        self.a_retry = act("Retry failed frames", self.retry_failed, None,
                           "Queue the selected job again, rendering only frames that failed")
        self.a_reset = act("Start job over…", self.reset_job, None, "Forget progress and render every frame again")
        self.a_preview = act("Create preview", self.create_preview, None,
                             "Encode the frames finished so far into preview/<job>.mp4")
        self.a_folder = act("Open folder", self.open_folder, None, "Open the job's render folder")
        self.a_settings = act("Settings…", self.open_settings, "Ctrl+,", "Blender installs and defaults")

        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for a in (self.a_add, self.a_remove, self.a_up, self.a_down, None, self.a_start, self.a_stop_after,
                  self.a_stop_now, self.a_cancel, None, self.a_retry, self.a_reset, self.a_preview,
                  self.a_folder, None, self.a_settings):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.addToolBar(tb)

    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)

        # ---- batch summary --------------------------------------------------
        top = QHBoxLayout()
        self.batch_label = QLabel()
        self.batch_label.setTextFormat(Qt.TextFormat.RichText)
        self.batch_bar = QProgressBar()
        self.batch_bar.setMaximumWidth(380)
        self.batch_bar.setFormat("batch %p%")
        top.addWidget(self.batch_label, 1)
        top.addWidget(self.batch_bar)
        outer.addLayout(top)

        vsplit = QSplitter(Qt.Orientation.Vertical)
        hsplit = QSplitter(Qt.Orientation.Horizontal)

        # ---- queue table ----------------------------------------------------
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(self.COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(lambda *_: self.open_folder())
        hsplit.addWidget(self.table)

        # ---- right side tabs -------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_live_tab(), "Live")
        self.tabs.addTab(self._build_settings_tab(), "Job settings")
        self.issues = QPlainTextEdit()
        self.issues.setReadOnly(True)
        self.tabs.addTab(self.issues, "Issues && history")
        hsplit.addWidget(self.tabs)
        hsplit.setStretchFactor(0, 2)
        hsplit.setStretchFactor(1, 3)
        vsplit.addWidget(hsplit)

        # ---- bottom: console + messages -------------------------------------
        bottom = QTabWidget()
        cw = QWidget()
        cl = QVBoxLayout(cw)
        cl.setContentsMargins(0, 0, 0, 0)
        crow = QHBoxLayout()
        self.follow = QCheckBox("Follow the rendering job")
        self.follow.setChecked(True)
        self.console_title = QLabel()
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_console)
        open_log = QPushButton("Open logs folder")
        open_log.clicked.connect(lambda: self._open_path(self._sel() and self._sel().logs_dir))
        crow.addWidget(self.console_title, 1)
        crow.addWidget(self.follow)
        crow.addWidget(open_log)
        crow.addWidget(clear)
        cl.addLayout(crow)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.console.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.console.setMaximumBlockCount(self.settings.console_lines)
        cl.addWidget(self.console)
        bottom.addTab(cw, "Blender console")
        self.messages = QListWidget()
        bottom.addTab(self.messages, "Messages")
        self.bottom_tabs = bottom
        vsplit.addWidget(bottom)
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        outer.addWidget(vsplit, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

    def _build_live_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)

        row = QHBoxLayout()
        g1 = QGroupBox("Current frame")
        f1 = QGridLayout(g1)
        self.l_frame = QLabel("–")
        self.l_frame.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.l_phase = QLabel("")
        self.frame_bar = QProgressBar()
        self.frame_bar.setFormat("%p%")
        self.l_frame_time = QLabel("–")
        self.l_frame_remaining = QLabel("–")
        self.l_mem = QLabel("–")
        f1.addWidget(self.l_frame, 0, 0, 1, 2)
        f1.addWidget(self.l_phase, 1, 0, 1, 2)
        f1.addWidget(self.frame_bar, 2, 0, 1, 2)
        for r, (k, v) in enumerate((("Elapsed", self.l_frame_time), ("Blender says remaining", self.l_frame_remaining),
                                    ("Memory (now / Blender peak)", self.l_mem)), start=3):
            f1.addWidget(QLabel(k), r, 0)
            f1.addWidget(v, r, 1)
        row.addWidget(g1, 1)

        g2 = QGroupBox("Job")
        f2 = QGridLayout(g2)
        self.job_bar = QProgressBar()
        f2.addWidget(self.job_bar, 0, 0, 1, 2)
        self.job_labels = {}
        keys = ["Frames", "Average frame", "Time remaining", "Finishes at", "Disk used / projected",
                "Render time so far", "Blender runs", "Blender", "Engine / device"]
        for r, k in enumerate(keys, start=1):
            f2.addWidget(QLabel(k), r, 0)
            v = QLabel("–")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setWordWrap(True)
            f2.addWidget(v, r, 1)
            self.job_labels[k] = v
        f2.setColumnStretch(1, 1)
        row.addWidget(g2, 1)

        self.thumb = Thumbnail()
        g3 = QGroupBox("Last finished frame")
        g3l = QVBoxLayout(g3)
        g3l.addWidget(self.thumb)
        row.addWidget(g3, 1)
        lay.addLayout(row)

        charts = QHBoxLayout()
        self.chart_time = LineChart("Render time per frame", "seconds", ["render time"])
        self.chart_mem = LineChart("Memory per frame", "MB", ["Blender peak", "process peak"])
        charts.addWidget(self.chart_time)
        charts.addWidget(self.chart_mem)
        lay.addLayout(charts, 1)
        return w

    def _build_settings_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.job_form = JobSettingsForm(self._installs)
        lay.addWidget(self.job_form)
        row = QHBoxLayout()
        self.l_form_note = QLabel()
        self.l_form_note.setWordWrap(True)
        self.b_apply = QPushButton("Apply to this job")
        self.b_apply.clicked.connect(self._apply_job_settings)
        row.addWidget(self.l_form_note, 1)
        row.addWidget(self.b_apply)
        lay.addLayout(row)
        self.l_file_info = QLabel()
        self.l_file_info.setWordWrap(True)
        self.l_file_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.l_file_info)
        lay.addStretch(1)
        return w

    # ======================================================================
    # helpers
    # ======================================================================
    def _installs(self):
        return [BlenderInstall.from_dict(d) for d in self.settings.installs]

    def _sel(self) -> Optional[Job]:
        return self.queue.find(self.selected_id) if self.selected_id else None

    def _is_current(self, job: Optional[Job]) -> bool:
        return bool(job and self.runner.running and self.runner.current is job)

    def _open_path(self, p):
        if p:
            os.makedirs(p, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _add_message(self, level: str, text: str):
        item = QListWidgetItem(f"{time.strftime('%H:%M:%S')}  {level.upper():5s}  {text}")
        if level == "error":
            item.setForeground(QColor("#dc3545"))
        elif level == "warn":
            item.setForeground(QColor("#d39e00"))
        self.messages.addItem(item)
        self.messages.scrollToBottom()
        if self.messages.count() > 5000:
            self.messages.takeItem(0)
        self.statusBar().showMessage(text, 8000)

    def _first_run(self):
        if not self.settings.installs:
            found = scan()
            self.settings.installs = [i.to_dict() for i in found]
            self.settings.save()
            if found:
                self._add_message("info", "Found Blender installs: " + ", ".join(i.label for i in found))
            else:
                QMessageBox.information(self, APP_NAME, "No Blender install found automatically.\n"
                                        "Add one under Settings.")
            self.job_form.reload_installs()
        if self.settings.auto_resume and any(j.status == STOPPED and "interrupted" in (j.status_note or "")
                                             for j in self.queue.jobs):
            self._add_message("info", "Resuming the interrupted batch")
            self.start_batch()

    # ======================================================================
    # table
    # ======================================================================
    def _refresh_table(self, full: bool = False):
        ids = [j.id for j in self.queue.jobs]
        if full or ids != self._row_ids:
            sel = self.selected_id
            self.table.blockSignals(True)
            self.table.setRowCount(len(ids))
            for r in range(len(ids)):
                for c in range(len(self.COLS)):
                    if c == 2:
                        if not isinstance(self.table.cellWidget(r, c), QProgressBar):
                            bar = QProgressBar()
                            bar.setMaximumHeight(18)
                            self.table.setCellWidget(r, c, bar)
                    elif self.table.item(r, c) is None:
                        self.table.setItem(r, c, QTableWidgetItem())
            self._row_ids = ids
            self.table.blockSignals(False)
            if sel in ids:
                self.table.selectRow(ids.index(sel))
            elif ids:
                self.table.selectRow(0)
            else:
                self.selected_id = None
        for r, j in enumerate(self.queue.jobs):
            self._fill_row(r, j)

    def _fill_row(self, r: int, j: Job):
        total = j.total_frames
        done = j.count(F_DONE)
        failed = j.count(F_FAILED)
        status = j.status
        if self._is_current(j):
            extra = {"loading": "loading", "preflight": "checking file", "preview": "making preview",
                     "waiting": "waiting to retry"}.get(self.runner.live.phase)
            if extra:
                status = f"{j.status} – {extra}"
        items = [
            (0, j.name, f"{j.blend_path}\n→ {j.folder}"),
            (1, status, j.status_note),
            (3, f"{done}{'+' + str(failed) + ' failed' if failed else ''} / {total if total is not None else '?'}", None),
            (4, (j.blender_used or {}).get("label", f"auto (file {version_str(j.file_version)})"),
             (j.blender_used or {}).get("reason")),
            (5, self._eta_text(j), None),
        ]
        for c, text, tip in items:
            it = self.table.item(r, c)
            if it is None:
                continue
            it.setText(str(text))
            it.setToolTip(tip or "")
            if c == 1:
                col = STATUS_COLORS.get(j.status)
                it.setForeground(QColor(col) if col else self.palette().text())
        bar = self.table.cellWidget(r, 2)
        if isinstance(bar, QProgressBar):
            if total:
                frac = (done + failed) / total
                if self._is_current(j):
                    ff = self.runner.live.frame_fraction()
                    if ff and self.runner.live.frame is not None:
                        frac += ff / total
                bar.setRange(0, 1000)
                bar.setValue(int(min(1.0, frac) * 1000))
                bar.setFormat(f"{frac * 100:.1f}%")
            else:
                bar.setRange(0, 1000)
                bar.setValue(0)
                bar.setFormat("–")

    def _eta_text(self, j: Job) -> str:
        if j.status in (DONE, DONE_ERRORS):
            return "done"
        if j.status in (FAILED, CANCELLED):
            return ""
        eta = self._job_eta(j)
        return fmt_duration(eta) if eta is not None else "?"

    def _job_eta(self, j: Job) -> Optional[float]:
        if self._is_current(j):
            L = self.runner.live
            rem = None
            if L.frame is not None:
                if L.remaining_s is not None:
                    rem = L.remaining_s
                elif L.frame_started and j.avg_frame_seconds():
                    rem = max(0.0, j.avg_frame_seconds() - (time.time() - L.frame_started))
            return j.eta_seconds(rem)
        return j.eta_seconds()

    def _on_select(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if not rows:
            return
        r = min(rows)
        if r < len(self._row_ids):
            new = self._row_ids[r]
            if new != self.selected_id:
                self.selected_id = new
                self._load_selected()
        self._update_actions()

    def _load_selected(self):
        j = self._sel()
        if not j:
            return
        self.job_form.reload_installs()
        self.job_form.set_settings(j.settings)
        self._reload_console(j)
        self._refresh_live()
        self._refresh_charts()
        self._refresh_issues()
        last = j.done_records()
        last.sort(key=lambda r: r.get("finished_at") or 0)
        self.thumb.show_file(last[-1]["file"] if last else None)

    def _context_menu(self, pos):
        m = QMenu(self)
        for a in (self.a_start, self.a_stop_after, self.a_stop_now, self.a_cancel, None, self.a_retry, self.a_reset,
                  self.a_preview, self.a_folder, None, self.a_up, self.a_down, self.a_remove):
            m.addSeparator() if a is None else m.addAction(a)
        j = self._sel()
        if j and j.preview_path and os.path.exists(j.preview_path):
            m.addSeparator()
            m.addAction("Play preview").triggered.connect(lambda: self._open_path(j.preview_path))
        if j:
            m.addAction("Open report.xlsx").triggered.connect(lambda: self._open_path(j.data_dir / "report.xlsx")
                                                             if (j.data_dir / "report.xlsx").exists() else None)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _update_actions(self):
        j = self._sel()
        running = self.runner.running
        cur = self._is_current(j)
        self.a_start.setEnabled(not running and any(x.status in RUNNABLE for x in self.queue.jobs))
        self.a_stop_after.setEnabled(running and not self.runner.stopping)
        self.a_stop_now.setEnabled(running)
        self.a_cancel.setEnabled(running and self.runner.current is not None)
        self.a_remove.setEnabled(bool(j) and not cur)
        self.a_up.setEnabled(bool(j))
        self.a_down.setEnabled(bool(j))
        self.a_retry.setEnabled(bool(j) and not cur and j.count(F_FAILED) > 0
                                or bool(j) and not cur and j.status in (FAILED, CANCELLED))
        self.a_reset.setEnabled(bool(j) and not cur)
        self.a_preview.setEnabled(bool(j) and j.count(F_DONE) > 0)
        self.a_folder.setEnabled(bool(j))
        self.b_apply.setEnabled(bool(j) and not cur)
        self.l_form_note.setText("This job is rendering – stop it to change settings." if cur else
                                 "Changes apply the next time the job renders. Finished frames are kept.")

    # ======================================================================
    # live panel
    # ======================================================================
    def _tick(self):
        self._refresh_table()
        self._refresh_live()
        self._refresh_batch()

    def _refresh_batch(self):
        jobs = self.queue.jobs
        known = [j for j in jobs if j.total_frames and j.status not in (CANCELLED, FAILED)]
        tot = sum(j.total_frames for j in known)
        done = sum(j.count(F_DONE) + j.count(F_FAILED) for j in known)
        self.batch_bar.setRange(0, max(1, tot))
        self.batch_bar.setValue(done)
        pending = [j for j in jobs if j.status in RUNNABLE]
        etas = [self._job_eta(j) for j in pending]
        known_eta = sum(e for e in etas if e is not None)
        unknown = sum(1 for e in etas if e is None)
        n_done = sum(1 for j in jobs if j.status in (DONE, DONE_ERRORS))
        parts = [f"<b>{n_done}/{len(jobs)}</b> jobs done"]
        if self.runner.running:
            state = "stopping…" if self.runner.stopping else "rendering"
            parts.insert(0, f"<b style='color:#3d8bfd'>● {state}</b>")
        else:
            parts.insert(0, "<b>idle</b>")
        if pending:
            t = f"remaining ≈ <b>{fmt_duration(known_eta)}</b>"
            if known_eta:
                t += f" (done ≈ {clock(time.time() + known_eta)})"
            if unknown:
                t += f" + {unknown} job(s) not started yet (no estimate)"
            parts.append(t)
        if self.runner._keep.active:
            parts.append("keeping the computer awake")
        if self._quit_when_idle:
            parts.append("<b>will quit after the current frame</b>")
        self.batch_label.setText(" · ".join(parts))

    def _refresh_live(self):
        j = self._sel()
        L = self.runner.live
        cur = self._is_current(j)
        if not j:
            return
        if cur and L.frame is not None:
            self.l_frame.setText(f"Frame {L.frame}")
            ff = L.frame_fraction()
            self.frame_bar.setRange(0, 1000)
            self.frame_bar.setValue(int((ff or 0) * 1000))
            self.frame_bar.setFormat(f"sample {L.sample}/{L.samples}" if L.samples else "%p%")
            el = time.time() - L.frame_started if L.frame_started else None
            self.l_frame_time.setText(fmt_duration(el))
            self.l_frame_remaining.setText(fmt_duration(L.remaining_s) if L.remaining_s is not None else "–")
            self.l_phase.setText("rendering")
        else:
            phase = {"loading": "loading file / preparing frame", "preflight": "checking file",
                     "preview": "encoding preview MP4", "waiting": "waiting before retry"}.get(L.phase, "")
            if cur and L.phase == "waiting" and L.wait_until:
                phase += f" ({fmt_duration(max(0, L.wait_until - time.time()))})"
            self.l_frame.setText("–" if not cur else "…")
            self.l_phase.setText(phase if cur else j.status)
            self.frame_bar.setRange(0, 0 if cur and L.phase in ("loading", "preflight", "preview") else 1000)
            self.frame_bar.setValue(0)
            self.frame_bar.setFormat("")
            self.l_frame_time.setText("–")
            self.l_frame_remaining.setText("–")
        if cur:
            self.l_mem.setText(f"{fmt_bytes((L.rss_mb or 0) * 1048576) if L.rss_mb else '–'} / "
                               f"{fmt_bytes(L.peak_mb * 1048576) if L.peak_mb else '–'}")
        else:
            self.l_mem.setText("–")

        total = j.total_frames
        done, failed = j.count(F_DONE), j.count(F_FAILED)
        if total:
            self.job_bar.setRange(0, total)
            self.job_bar.setValue(done + failed)
            self.job_bar.setFormat(f"{done}/{total} frames  (%p%)")
        else:
            self.job_bar.setRange(0, 1)
            self.job_bar.setValue(0)
            self.job_bar.setFormat("frame range known after Blender has loaded the file")
        recs = j.done_records()
        times = [r["render_s"] for r in recs if r.get("render_s")]
        eta = self._job_eta(j)
        si = j.scene_info or {}
        bu = j.blender_used or {}
        vals = {
            "Frames": f"{done} done, {failed} failed, {len(j.remaining_frames() or []) if total else '?'} to go"
                      + (f"  (range {si.get('frame_start')}–{si.get('frame_end')})" if si else ""),
            "Average frame": fmt_duration(j.avg_frame_seconds()) + (
                f"  (min {fmt_duration(min(times))}, max {fmt_duration(max(times))})" if times else ""),
            "Time remaining": fmt_duration(eta) if j.status in RUNNABLE else "–",
            "Finishes at": clock(time.time() + eta) if eta and j.status in RUNNABLE else "–",
            "Disk used / projected": f"{fmt_bytes(j.used_bytes())} / {fmt_bytes(j.projected_bytes())}",
            "Render time so far": fmt_duration(sum(times)) if times else "–",
            "Blender runs": f"{len(j.attempts)}" + (f", {j.load_failures} failed to start" if j.load_failures else ""),
            "Blender": f"{bu.get('label', '–')}  ({bu.get('reason', 'not started yet')})",
            "Engine / device": f"{si.get('engine', '–')} / {si.get('device') or '–'}"
                               + (f", {si.get('samples')} samples" if si.get("samples") else "")
                               + (f", {si.get('res_x')}×{si.get('res_y')} @{si.get('res_pct')}%" if si else ""),
        }
        for k, v in vals.items():
            self.job_labels[k].setText(v)

    def _refresh_charts(self):
        j = self._sel()
        if not j:
            return
        recs = j.done_records()
        xs = [r["frame"] for r in recs]
        self.chart_time.set_data(xs, [[r.get("render_s") for r in recs]])
        self.chart_mem.set_data(xs, [[r.get("blender_peak_mb") for r in recs],
                                     [r.get("process_peak_mb") for r in recs]])

    def _refresh_issues(self):
        j = self._sel()
        if not j:
            self.issues.setPlainText("")
            return
        lines = [f"{j.name}", f"  file:   {j.blend_path}  (saved with Blender {version_str(j.file_version)})",
                 f"  folder: {j.folder}", f"  status: {j.status} {j.status_note}", ""]
        si = j.scene_info or {}
        if si.get("format_note"):
            lines += ["NOTE: " + si["format_note"], ""]
        if si.get("overrides_applied"):
            lines += ["Overrides: " + ", ".join(si["overrides_applied"]), ""]
        pf = j.preflight
        if pf:
            lines.append("Preflight check:")
            lines += [f"  ⚠ {p}" for p in pf.get("problems", [])] or ["  no problems found"]
            lines += [f"  ℹ {n}" for n in pf.get("notes", [])]
            lines.append("")
        bad = [(int(k), r) for k, r in list(j.frames.items()) if r.get("status") != F_DONE]
        if bad:
            lines.append("Frames with problems:")
            for f, r in sorted(bad):
                lines.append(f"  frame {f}: {r.get('status')} after {r.get('attempts')} attempt(s) – {r.get('last_error', '')}")
            lines.append("")
        if j.attempts:
            lines.append("Blender runs:")
            for a in j.attempts:
                dur = fmt_duration(a["ended"] - a["started"]) if a.get("ended") else "running"
                lines.append(f"  #{a['n']:>3}  {time.strftime('%d.%m %H:%M:%S', time.localtime(a['started']))}  "
                             f"{dur:>10}  {a.get('frames_rendered', 0):>4} frames  {a.get('reason', '')}")
        self.issues.setPlainText("\n".join(lines))

    # ======================================================================
    # console
    # ======================================================================
    def _console_target(self) -> Optional[str]:
        if self.follow.isChecked() and self.runner.current is not None:
            return self.runner.current.id
        return self.selected_id

    def _reload_console(self, j: Optional[Job] = None):
        target = self._console_target()
        tj = self.queue.find(target) if target else None
        self.console.setPlainText("\n".join(self.console_buf.get(target, [])))
        self.console.moveCursor(QTextCursor.MoveOperation.End)
        self.console_title.setText(f"Console: {tj.name}" if tj else "Console")
        self._console_shown = target

    def _flush_console(self):
        if getattr(self, "_console_shown", None) != self._console_target():
            self._reload_console()
            self.pending_console.clear()
            return
        if not self.pending_console:
            return
        target = self._console_target()
        lines = [t for jid, t in self.pending_console if jid == target]
        self.pending_console.clear()
        if lines:
            sb = self.console.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 4
            self.console.appendPlainText("\n".join(lines))
            if at_bottom:
                sb.setValue(sb.maximum())

    def _clear_console(self):
        t = self._console_target()
        if t in self.console_buf:
            self.console_buf[t].clear()
        self.console.clear()

    # ======================================================================
    # engine events
    # ======================================================================
    def _on_event(self, kind: str, p: dict):
        jid = p.get("job_id")
        if kind == "console":
            buf = self.console_buf.setdefault(jid, collections.deque(maxlen=self.settings.console_lines))
            buf.append(p["text"])
            self.pending_console.append((jid, p["text"]))
        elif kind == "message":
            self._add_message(p["level"], p["text"])
            if jid == self.selected_id:
                self._refresh_issues()
        elif kind == "job":
            self._refresh_table()
            if jid == self.selected_id:
                self._refresh_live()
                self._refresh_issues()
            self._update_actions()
        elif kind == "frame_done":
            self._refresh_table()
            if jid == self.selected_id:
                j = self._sel()
                rec = j.frames.get(str(p["frame"])) if j else None
                if rec:
                    self.thumb.show_file(rec.get("file"))
                self._refresh_charts()
                self._refresh_live()
        elif kind == "preview":
            self._update_actions()
        elif kind == "batch":
            self._refresh_table()
            self._update_actions()
            if not p["running"] and self._quit_when_idle:
                self.close()

    # ======================================================================
    # actions
    # ======================================================================
    def add_files(self, files=None):
        if not files:
            start = self.settings.last_render_root or os.path.expanduser("~")
            files, _ = QFileDialog.getOpenFileNames(self, "Add .blend files", start, "Blender files (*.blend)")
        files = [f for f in files if f.lower().endswith(".blend")]
        if not files:
            return
        dlg = AddJobsDialog(files, self.settings, self._installs(), self)
        if dlg.exec() != AddJobsDialog.DialogCode.Accepted:
            return
        s = dlg.form.get_settings()
        if s.render_root:
            self.settings.last_render_root = s.render_root
            self.settings.save()
        for f in files:
            js = JobSettings.from_dict(vars(s).copy())
            js.render_root = s.render_root
            j = self.queue.add(f, js)
            self._add_message("info", f"Added {j.name} → {j.folder}"
                              + (" (re-attached to existing render folder, finished frames kept)" if j.frames else ""))
        self._refresh_table(full=True)
        self._update_actions()

    def remove_job(self):
        j = self._sel()
        if not j or self._is_current(j):
            return
        if QMessageBox.question(self, "Remove job", f"Remove '{j.name}' from the queue?\n"
                                "Rendered frames and data stay in its folder; adding the same file "
                                "to the same render folder again picks up where it left off.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.queue.remove(j)
        self.selected_id = None
        self._refresh_table(full=True)
        self._update_actions()

    def move_job(self, d):
        j = self._sel()
        if j:
            self.queue.move(j, d)
            self._refresh_table(full=True)

    def start_batch(self):
        if not self.settings.installs:
            QMessageBox.warning(self, APP_NAME, "Add a Blender install under Settings first.")
            return
        self._quit_when_idle = False
        self.runner.start()
        self._update_actions()

    def stop_after_frame(self):
        self.runner.stop_after_frame()
        self._add_message("info", "Stopping after the current frame")
        self._update_actions()

    def stop_now(self):
        self.runner.stop_now()
        self._add_message("info", "Stopping now – the current frame will be rendered again next time")
        self._update_actions()

    def cancel_job(self):
        cur = self.runner.current
        if cur and QMessageBox.question(self, "Cancel job", f"Cancel '{cur.name}' and continue with the next job?"
                                        "\nFinished frames are kept.") == QMessageBox.StandardButton.Yes:
            self.runner.cancel_current_job()

    def retry_failed(self):
        j = self._sel()
        if j and not self._is_current(j):
            self.queue.reset_job(j, keep_frames=True)
            self._add_message("info", f"{j.name}: queued again, failed frames will be retried")
            self._refresh_table()
            self._update_actions()

    def reset_job(self):
        j = self._sel()
        if not j or self._is_current(j):
            return
        if QMessageBox.question(self, "Start over", f"Render every frame of '{j.name}' again?\n"
                                "Existing frame files will be overwritten.") != QMessageBox.StandardButton.Yes:
            return
        self.queue.reset_job(j, keep_frames=False)
        self._load_selected()
        self._refresh_table()
        self._update_actions()

    def create_preview(self):
        j = self._sel()
        if j:
            self._add_message("info", f"{j.name}: creating preview from {j.count(F_DONE)} frames…")
            self.runner.make_preview(j)

    def open_folder(self):
        j = self._sel()
        if j:
            self._open_path(j.folder)

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            dlg.apply()
            self.console.setMaximumBlockCount(self.settings.console_lines)
            self.job_form.reload_installs()
            self._update_actions()

    def _apply_job_settings(self):
        j = self._sel()
        if not j or self._is_current(j):
            return
        new = self.job_form.get_settings()
        old = j.settings
        if not new.render_root:
            new.render_root = old.render_root
        if os.path.abspath(new.render_root) != os.path.abspath(old.render_root):
            QMessageBox.information(self, "Render folder", "The render folder of an existing job can't be moved here. "
                                    "Remove the job and add the file again with the new folder.")
            new.render_root = old.render_root
        range_changed = (new.frame_start, new.frame_end) != (old.frame_start, old.frame_end)
        j.settings = new
        if j.status in (DONE, DONE_ERRORS, CANCELLED, FAILED) and range_changed:
            j.status, j.status_note = QUEUED, "settings changed"
        if range_changed and j.scene_info:
            # the new range is known for sure only after Blender reloads the file
            if new.frame_start is not None:
                j.scene_info["frame_start"] = new.frame_start
            if new.frame_end is not None:
                j.scene_info["frame_end"] = new.frame_end
        self.queue.save_job(j)
        self._add_message("info", f"{j.name}: settings saved")
        self._refresh_table()
        self._update_actions()

    # ======================================================================
    # drag & drop, closing
    # ======================================================================
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith(".blend") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_files([u.toLocalFile() for u in e.mimeData().urls()])

    def closeEvent(self, e):
        if not self.runner.running:
            e.accept()
            return
        if self._quit_when_idle:
            e.ignore()
            return
        box = QMessageBox(self)
        box.setWindowTitle("Rendering")
        box.setText("A render is running.")
        box.setInformativeText("The batch continues from where it stopped the next time you press Start.")
        now = box.addButton("Stop now and quit", QMessageBox.ButtonRole.DestructiveRole)
        after = box.addButton("Finish this frame, then quit", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Keep rendering", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is now:
            self.runner.stop_now()
            self.runner.wait(15)
            e.accept()
        elif clicked is after:
            self._quit_when_idle = True
            self.runner.stop_after_frame()
            self._add_message("info", "Will quit after the current frame")
            e.ignore()
        else:
            e.ignore()
