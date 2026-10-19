"""The main window."""
from __future__ import annotations

import collections
import os
import time
from typing import Dict, Optional

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFontDatabase, QKeySequence, QTextCursor, QShortcut
from PySide6.QtWidgets import (QMainWindow, QWidget, QSplitter, QTabWidget, QToolButton,
                               QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QLabel, QProgressBar,
                               QPlainTextEdit, QToolBar, QFileDialog, QMessageBox, QHeaderView,
                               QAbstractItemView, QPushButton, QCheckBox, QMenu, QListWidget,
                               QListWidgetItem, QStatusBar, QSizePolicy, QStackedWidget, QComboBox)

from .. import APP_NAME, __version__
from ..blendfile import version_str
from ..installs import BlenderInstall, scan
from ..api import ApiServer
from ..control import Controller
from ..jobqueue import JobQueue, QUEUE_EXT
from ..models import (AppSettings, Job, JobSettings, QUEUED, RENDERING, STOPPED, DONE, DONE_ERRORS,
                      FAILED, CANCELLED, ON_HOLD, F_DONE, F_FAILED, RUNNABLE)
from ..parse import fmt_duration, fmt_bytes
from ..runner import Runner
from .api_bridge import QtExecutor
from .dialogs import SettingsDialog, AddJobsDialog, FolderDialog
from .queue_tree import QueueTree
from .widgets import JobSettingsForm, Thumbnail, LineChart
from .machines_view import MachinesPanel, job_machines_text
from .worker_view import WorkerView

STATUS_COLORS = {RENDERING: "#3d8bfd", DONE: "#2fa84f", DONE_ERRORS: "#d39e00", FAILED: "#dc3545",
                 CANCELLED: "#888888", STOPPED: "#e67e22", ON_HOLD: "#c678dd"}


class Bridge(QObject):
    """Carries engine events from the render thread into the GUI thread."""
    event = Signal(str, object)


def clock(ts: float) -> str:
    t = time.localtime(ts)
    same_day = time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M" if same_day else "%a %H:%M", t)


class MainWindow(QMainWindow):

    def __init__(self, first_run: bool = True):
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

        # queue file (manual save): compare the queue with what the file holds
        self._saved_snapshot = None
        qf = self.settings.current_queue_file
        if qf and os.path.isfile(qf):
            self._saved_snapshot = self._file_snapshot(qf)
        elif qf:
            self.settings.current_queue_file = ""
        self.queue.on_change = self._update_title

        # web API + status page
        self.controller = Controller(self.queue, self.runner, self.settings)
        self.controller.on_changed = self._after_api_change
        self.controller.open_queue_file = lambda path: QTimer.singleShot(0, lambda: self.open_queue_file(path))
        self.controller.queue_file_info = lambda: {"path": self.settings.current_queue_file or None,
                                                   "modified": self._is_modified()}
        self.api = ApiServer(self.controller, QtExecutor(), self.settings)

        self.console_buf: Dict[str, collections.deque] = {}
        self.pending_console: list = []
        self.selected_id: Optional[str] = None     # selected job
        self.selected_gid: Optional[str] = None    # selected queue folder
        self._quit_when_idle = False
        self._tree_sig = None
        self._worker_console: list = []
        self._worker_last = 0.0
        self._machines_last = 0.0

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
        self._start_api()
        self._update_title()
        self._apply_mode(initial=True)
        if first_run:
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
                a.setToolTip(tip + (f"  ({QKeySequence(shortcut).toString(QKeySequence.SequenceFormat.NativeText)})"
                                    if shortcut else ""))
                a.setStatusTip(tip)
            return a

        # queue files
        self.a_new_queue = act("New Queue", self.new_queue, "Ctrl+N", "Start an empty queue")
        self.a_open_queue = act("Open Queue…", self.open_queue_dialog, "Ctrl+O", "Open a saved queue file")
        self.a_save = act("Save Queue", self.save_queue, "Ctrl+S", "Save the queue to its file")
        self.a_save_as = act("Save Queue As…", self.save_queue_as, "Ctrl+Shift+S", "Save the queue to a new file")
        self.recent_menu = QMenu("Open Recent", self)
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        # jobs & folders
        self.a_add = act("Add .blend…", self.add_files, "Ctrl+I", "Add .blend files to the queue (or drop them on the list)")
        self.a_add_existing = act("Add existing job folder…", self.add_existing, None,
                                  "Put a job folder back into the queue, e.g. after moving the render folder")
        self.a_new_folder = act("New folder…", self.new_folder, "Ctrl+Shift+N", "Create a folder to organise jobs")
        self.a_edit_folder = act("Folder settings…", self.edit_folder, None, "Rename the folder / set its render folder")
        self.a_remove = act("Remove…", self.remove_selected, None,
                            "Remove the selected job (files stay on disk) or folder (its jobs move to the top)  (⌫)")
        self.a_up = act("▲", lambda: self.move_selected(-1), "Ctrl+Up", "Move up")
        self.a_down = act("▼", lambda: self.move_selected(1), "Ctrl+Down", "Move down")
        # rendering
        self.a_start = act("▶  Start", self.start_batch, "Ctrl+R", "Render the queue (resumes where it stopped)")
        self.a_stop_after = act("Stop after frame", self.stop_after_frame, "Ctrl+Shift+.",
                                "Let the current frame finish, then stop")
        self.a_stop_now = act("■  Stop now", self.stop_now, "Ctrl+.",
                              "Kill Blender now - the current frame is rendered again next time")
        self.a_cancel = act("Cancel job", self.cancel_job, None,
                            "Stop the current job, mark it cancelled and continue with the next one")
        self.a_retry = act("Retry failed frames", self.retry_failed, None,
                           "Queue the selected job again, rendering only frames that failed")
        self.a_accept = act("Continue with changed file", self.accept_changed, None,
                            "The .blend was saved again after frames were rendered: keep the finished frames "
                            "and render the rest from the changed file")
        self.a_reset = act("Start job over…", self.reset_job, None, "Forget progress and render every frame again")
        self.a_preview = act("Create preview", self.create_preview, "Ctrl+P",
                             "Encode the frames finished so far into preview/<job>.mp4")
        self.a_play = act("Play preview", self.play_preview, None, "Open the preview video  (Space)")
        self.a_folder = act("Open render folder", self.open_folder, None, "Open the job's render folder  (↩)")
        self.a_report = act("Open report.xlsx", self.open_report, None, "Open the job's Excel report")
        self.a_settings = act("Settings…", self.open_settings, "Ctrl+,", "Blender installs, defaults, API")
        self.a_settings.setMenuRole(QAction.MenuRole.PreferencesRole)
        self.a_status_page = act("Open status page in browser", self.open_status_page, None,
                                 "The status page shows progress in any browser – also on your phone")
        self.a_shortcuts = act("Keyboard shortcuts", self.show_shortcuts, "Ctrl+/", "List of keyboard shortcuts")
        self.a_docs = act("Documentation", self.open_docs, None, "Open the user guide (PDF)")

        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for a in (self.a_add, self.a_new_folder, self.a_remove, self.a_up, self.a_down, None, self.a_start,
                  self.a_stop_after, self.a_stop_now, self.a_cancel, None):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.job_menu = QMenu("Job", self)
        for a in (self.a_retry, self.a_accept, self.a_reset, None, self.a_preview, self.a_play, None, self.a_folder,
                  self.a_report):
            self.job_menu.addSeparator() if a is None else self.job_menu.addAction(a)
        jb = QToolButton()
        jb.setText("Job ▾")
        jb.setMenu(self.job_menu)
        jb.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(jb)
        tb.addSeparator()
        tb.addAction(self.a_settings)
        self.addToolBar(tb)
        self.tb_queue = tb
        mt = QToolBar("Mode")
        mt.setMovable(False)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        mt.addWidget(spacer)
        mt.addWidget(QLabel("Mode: "))
        self.mode_box = QComboBox()
        self.mode_box.addItem("Coordinator – keeps the queue", "coordinator")
        self.mode_box.addItem("Worker – renders for another computer", "worker")
        self.mode_box.setToolTip("Coordinator: this computer has the queue and can share frames with workers.\n"
                                 "Worker: this computer renders frames for a coordinator on the home network.\n"
                                 "Locked while something is rendering.")
        self.mode_box.setCurrentIndex(1 if self.settings.mode == "worker" else 0)
        self.mode_box.currentIndexChanged.connect(self._mode_changed)
        mt.addWidget(self.mode_box)
        mt.addSeparator()
        self.tb_settings = mt.addAction("Settings…", self.open_settings)
        self.tb_settings.setVisible(False)
        self.addToolBar(mt)
        self.tb_mode = mt

        mb = self.menuBar()
        fm = mb.addMenu("File")
        for a in (self.a_new_queue, self.a_open_queue, self.recent_menu, None, self.a_save, self.a_save_as, None,
                  self.a_add, self.a_add_existing, None, self.a_new_folder, self.a_edit_folder, None, self.a_settings):
            if a is None:
                fm.addSeparator()
            elif isinstance(a, QMenu):
                fm.addMenu(a)
            else:
                fm.addAction(a)
        qm = mb.addMenu("Queue")
        for a in (self.a_start, self.a_stop_after, self.a_stop_now, self.a_cancel, None, self.a_up, self.a_down,
                  self.a_remove):
            qm.addSeparator() if a is None else qm.addAction(a)
        self.queue_menu = qm
        mb.addMenu(self.job_menu)
        hm = mb.addMenu("Help")
        for a in (self.a_status_page, None, self.a_shortcuts, self.a_docs):
            hm.addSeparator() if a is None else hm.addAction(a)

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

        # ---- queue tree -----------------------------------------------------
        self.tree = QueueTree()
        self.tree.itemSelectionChanged.connect(self._on_select)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemDoubleClicked.connect(lambda *_: self.open_folder() if self._sel() else self.edit_folder())
        self.tree.order_changed.connect(self._on_tree_order)
        self.tree.files_dropped.connect(lambda files, gid: self.add_files(files, gid))
        self.tree.check_changed.connect(self._on_check)
        self.tree.expanded_changed.connect(self._on_expanded)
        hsplit.addWidget(self.tree)
        # keys that act on the selected row (only while the queue list has the focus)
        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for key, slot in (("Backspace", self.remove_selected), ("Delete", self.remove_selected),
                          ("Ctrl+Backspace", lambda: self.remove_selected(confirm=False)),
                          ("Ctrl+Delete", lambda: self.remove_selected(confirm=False)),
                          ("Return", self._enter_pressed), ("Enter", self._enter_pressed),
                          ("Space", self.play_preview)):
            sc = QShortcut(QKeySequence(key), self.tree)
            sc.setContext(ctx)
            sc.activated.connect(slot)

        # ---- right side tabs -------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_live_tab(), "Live")
        self.tabs.addTab(self._build_settings_tab(), "Job settings")
        self.issues = QPlainTextEdit()
        self.issues.setReadOnly(True)
        self.tabs.addTab(self.issues, "Issues && history")
        self.machines_panel = MachinesPanel(self.runner, self.settings, self.api)
        self.machines_panel.settings_changed.connect(self._start_api)
        self.tabs.addTab(self.machines_panel, "Machines")
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
        self.stack = QStackedWidget()
        self.stack.addWidget(central)
        self.worker_view = WorkerView(self.settings, lambda k, p: self.bridge.event.emit(k, p))
        self.stack.addWidget(self.worker_view)
        self.setCentralWidget(self.stack)
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
                "Render time so far", "Blender runs", "Blender", "Engine / device", "Machines"]
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
        self.job_form = JobSettingsForm(self._installs, machines_provider=self._machine_names)
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
    def _machine_names(self):
        """Computers that can render jobs: this one, plus known workers when sharing is on."""
        sched = self.runner.scheduler
        out = [(sched.local.name, "this computer")]
        if self.settings.share_with_workers:
            for m in sorted(sched.machines.values(), key=lambda m: m.name.lower()):
                if m.kind == "remote":
                    out.append((m.name, f"{m.os} · {m.gpu or 'device not known yet'}"))
        return out

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
        sig = QueueTree.signature(self.queue)
        if full or sig != self._tree_sig:
            sel = ("j", self.selected_id) if self.selected_id else ("g", self.selected_gid) if self.selected_gid else None
            self.tree.blockSignals(True)
            self.tree.rebuild(self.queue)
            self.tree.blockSignals(False)
            self._tree_sig = sig
            if sel and (sel[1] in self.tree.job_items or sel[1] in self.tree.group_items):
                self.tree.select(*sel)
            elif self.queue.ordered_jobs():
                self.tree.select("j", self.queue.ordered_jobs()[0].id)
            else:
                self.selected_id = self.selected_gid = None
            self._on_select()
        for jid, it in self.tree.job_items.items():
            j = self.queue.find(jid)
            if j:
                self._fill_row(it, j)
        for g in self.queue.groups:
            it = self.tree.group_items.get(g["id"])
            if it:
                self._fill_group_row(it, g)

    def _fill_row(self, it, j: Job):
        total = j.total_frames
        done = j.count(F_DONE)
        failed = j.count(F_FAILED)
        status = j.status
        if self._is_current(j):
            extra = {"loading": "loading", "preflight": "checking file", "preview": "making preview",
                     "waiting": "waiting to retry"}.get(self.runner.live.phase)
            if extra:
                status = f"{j.status} – {extra}"
        if not self.queue.is_active(j) and j.status in RUNNABLE:
            status += " (skipped)"
        cols = {
            0: (j.name, f"{j.blend_path}\n→ {j.folder}"),
            1: (status, j.status_note),
            3: (f"{done}{' +' + str(failed) + ' failed' if failed else ''} / {total if total is not None else '?'}", None),
            4: ((j.blender_used or {}).get("label", f"auto (file {version_str(j.file_version)})"),
                (j.blender_used or {}).get("reason")),
            5: (self._eta_text(j), None),
        }
        for c, (text, tip) in cols.items():
            if it.text(c) != str(text):
                it.setText(c, str(text))
            it.setToolTip(c, tip or "")
        col = STATUS_COLORS.get(j.status)
        it.setForeground(1, QColor(col) if col else self.palette().text())
        bar = self.tree.itemWidget(it, 2)
        if isinstance(bar, QProgressBar):
            if total:
                frac = (done + failed) / total
                if self._is_current(j):
                    ff = self.runner.live.frame_fraction()
                    if ff and self.runner.live.frame is not None:
                        frac += ff / total
                bar.setValue(int(min(1.0, frac) * 1000))
                bar.setFormat(f"{frac * 100:.1f}%")
            else:
                bar.setValue(0)
                bar.setFormat("–")

    def _fill_group_row(self, it, g: dict):
        jobs = self.queue.jobs_in(g["id"])
        n_done = sum(1 for j in jobs if j.status in (DONE, DONE_ERRORS))
        rendering = any(self._is_current(j) for j in jobs)
        known = [j for j in jobs if j.total_frames]
        tot = sum(j.total_frames for j in known)
        fin = sum(j.count(F_DONE) + j.count(F_FAILED) for j in known)
        etas = [self._job_eta(j) for j in jobs if j.status in RUNNABLE and self.queue.is_active(j)]
        eta = sum(e for e in etas if e is not None)
        status = f"{n_done}/{len(jobs)} jobs done" + (" · rendering" if rendering else "")
        if not g.get("enabled", True):
            status += " · skipped"
        it.setText(0, f"{g['name']}  ({len(jobs)})")
        it.setToolTip(0, f"Render folder: {g.get('render_root') or '(per job)'}")
        it.setText(1, status)
        it.setText(3, f"{fin} / {tot}" if known else "")
        it.setText(5, fmt_duration(eta) if etas and eta else ("done" if jobs and n_done == len(jobs) else ""))
        bar = self.tree.itemWidget(it, 2)
        if isinstance(bar, QProgressBar):
            bar.setValue(int(fin / tot * 1000) if tot else 0)
            bar.setFormat(f"{fin / tot * 100:.1f}%" if tot else "–")

    def _eta_text(self, j: Job) -> str:
        if j.status in (DONE, DONE_ERRORS):
            return "done"
        if j.status in (FAILED, CANCELLED):
            return ""
        eta = self._job_eta(j)
        return fmt_duration(eta) if eta is not None else "?"

    def _job_eta(self, j: Job) -> Optional[float]:
        return self.runner.job_eta(j)

    def _on_select(self):
        sel = self.tree.selected()
        jid = sel[1] if sel and sel[0] == "j" else None
        gid = sel[1] if sel and sel[0] == "g" else None
        changed = (jid != self.selected_id) or (gid != self.selected_gid)
        self.selected_id, self.selected_gid = jid, gid
        if changed:
            self._load_selected()
        self._update_actions()

    def _load_selected(self):
        j = self._sel()
        self.tabs.setEnabled(j is not None)
        if not j:
            g = self.queue.group(self.selected_gid) if self.selected_gid else None
            self.l_frame.setText(g["name"] if g else "–")
            self.l_phase.setText("Folder – select a job to see its details" if g else "")
            self.thumb.show_file(None)
            self.issues.setPlainText("")
            self.chart_time.set_data([], [[]])
            self.chart_mem.set_data([], [[], []])
            self._reload_console()
            return
        self.job_form.reload_installs()
        self.job_form.reload_machines(keep_choices=False)
        self.job_form.set_settings(j.settings)
        self._reload_console(j)
        self._refresh_live()
        self._refresh_charts()
        self._refresh_issues()
        last = j.done_records()
        last.sort(key=lambda r: r.get("finished_at") or 0)
        self.thumb.show_file(last[-1]["path"] if last else None)

    def _context_menu(self, pos):
        m = QMenu(self)
        j = self._sel()
        g = self.queue.group(self.selected_gid) if self.selected_gid else None
        if j:
            for a in (self.a_retry, self.a_accept, self.a_reset, None, self.a_preview, self.a_folder):
                m.addSeparator() if a is None else m.addAction(a)
            m.addAction(self.a_play)
            m.addAction(self.a_report)
            m.addSeparator()
            mv = m.addMenu("Move to folder")
            mv.addAction("— top level —").triggered.connect(lambda: self._move_to_group(j, ""))
            for grp in self.queue.groups:
                a = mv.addAction(grp["name"])
                a.setEnabled(grp["id"] != j.group)
                a.triggered.connect(lambda _=False, gid=grp["id"]: self._move_to_group(j, gid))
            mv.addSeparator()
            mv.addAction("New folder…").triggered.connect(lambda: self.new_folder(move_job=j))
        elif g:
            m.addAction(self.a_edit_folder)
            m.addAction("Add .blend files to this folder…").triggered.connect(lambda: self.add_files(None, g["id"]))
        else:
            m.addAction(self.a_add)
            m.addAction(self.a_new_folder)
        m.addSeparator()
        for a in (self.a_up, self.a_down, self.a_remove):
            m.addAction(a)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _update_actions(self):
        j = self._sel()
        g = self.selected_gid
        running = self.runner.running
        cur = self._is_current(j)
        self.a_start.setEnabled(not running and self.queue.next_runnable() is not None)
        self.a_stop_after.setEnabled(running and not self.runner.stopping)
        self.a_stop_now.setEnabled(running)
        self.a_cancel.setEnabled(running and self.runner.current is not None)
        self.a_remove.setEnabled((bool(j) and not cur) or bool(g))
        self.a_up.setEnabled(bool(j) or bool(g))
        self.a_down.setEnabled(bool(j) or bool(g))
        self.a_edit_folder.setEnabled(bool(g))
        self.a_retry.setEnabled(bool(j) and not cur and (j.count(F_FAILED) > 0
                                                         or j.status in (FAILED, CANCELLED, ON_HOLD)))
        self.a_accept.setEnabled(bool(j) and not cur and j.status == ON_HOLD and j.blend_changed())
        self.a_reset.setEnabled(bool(j) and not cur)
        self.a_preview.setEnabled(bool(j) and j.count(F_DONE) > 0)
        self.a_folder.setEnabled(bool(j))
        self.a_play.setEnabled(bool(j) and bool(j.preview_path) and os.path.exists(j.preview_path or ""))
        self.a_report.setEnabled(bool(j) and (j.data_dir / "report.xlsx").exists())
        self._update_mode_lock()
        self.a_new_queue.setEnabled(not running)
        self.a_open_queue.setEnabled(not running)
        self.recent_menu.setEnabled(not running)
        self.b_apply.setEnabled(bool(j) and not cur)
        self.l_form_note.setText("This job is rendering – stop it to change settings." if cur else
                                 "Changes apply the next time the job renders. Finished frames are kept.")

    # ======================================================================
    # live panel
    # ======================================================================
    def _tick(self):
        if self.settings.mode == "worker":
            self.worker_view.refresh()
            self._update_mode_lock()
            return
        if self.tabs.currentWidget() is self.machines_panel:
            self.machines_panel.refresh()
        self._update_mode_lock()
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
        if self.settings.share_with_workers:
            pub = self.runner.scheduler.machines_public()
            n_r = sum(1 for m in pub["machines"] if m["kind"] == "remote" and m["online"] and m["state"] == "rendering")
            n_on = sum(1 for m in pub["machines"] if m["kind"] == "remote" and m["online"])
            if n_on:
                parts.append(f"{n_on} worker(s) online" + (f", {n_r} rendering" if n_r else ""))
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
        vals["Machines"] = job_machines_text(self.runner.scheduler, j) or "this computer"
        pkg = j.package or {}
        if self.settings.share_with_workers:
            if pkg.get("error"):
                vals["Machines"] += f"\n(not shared: {pkg['error']})"
            elif pkg.get("hash"):
                need = pkg.get("addons_required") or []
                vals["Machines"] += (f"\nshared copy {fmt_bytes(pkg.get('size'))}, Blender {pkg.get('blender')}"
                                     + (f", needs {', '.join(need)}" if need else ""))
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
        if self._worker_console:
            lines, self._worker_console = self._worker_console, []
            self.worker_view.append_console(lines)
        if self.settings.mode == "worker":
            return
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
        if self.settings.mode == "worker" and kind == "console":
            self._worker_console.append(p["text"])
            return
        if kind in ("worker", "worker_live"):
            if kind == "worker" or time.time() - self._worker_last > 0.3:
                self._worker_last = time.time()
                self.worker_view.refresh()
                self._update_mode_lock()
            return
        if kind == "worker_found":
            self.worker_view.found(p["found"])
            return
        if kind == "worker_paired":
            self.worker_view.paired(p)
            return
        if kind == "worker_addons":
            self.worker_view.addons_done()
            return
        if kind == "machines":
            if self.tabs.currentWidget() is self.machines_panel and time.time() - self._machines_last > 0.5:
                self._machines_last = time.time()
                self.machines_panel.refresh()
            return
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
                    self.thumb.show_file(str(j.frame_file(int(p["frame"]), rec)))
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
    def add_files(self, files=None, gid=None):
        if not files:
            start = self.settings.last_blend_dir or os.path.expanduser("~")
            files, _ = QFileDialog.getOpenFileNames(self, "Add .blend files", start, "Blender files (*.blend)")
        files = [f for f in files if f.lower().endswith(".blend")]
        if not files:
            return
        self.settings.last_blend_dir = os.path.dirname(os.path.abspath(files[0]))
        if gid is None:
            j = self._sel()
            gid = self.selected_gid or (j.group if j else "")
        dlg = AddJobsDialog(files, self.settings, self._installs(), self, groups=self.queue.groups, group_id=gid or "")
        if dlg.exec() != AddJobsDialog.DialogCode.Accepted:
            self.settings.save()
            return
        s = dlg.form.get_settings()
        s.render_root = dlg.render_root()
        if s.render_root:
            self.settings.last_render_root = s.render_root
        self.settings.save()
        last = None
        for f in files:
            js = JobSettings.from_dict(vars(s).copy())
            last = self.queue.add(f, js, group=dlg.group_id)
            self._add_message("info", f"Added {last.name} → {last.folder}"
                              + (" (re-attached to existing render folder, finished frames kept)" if last.frames else ""))
        self.selected_id, self.selected_gid = (last.id if last else None), None
        self._refresh_table(full=True)
        self._update_actions()

    def add_existing(self):
        d = QFileDialog.getExistingDirectory(self, "Choose a job folder (the one containing job.json)",
                                             self.settings.last_render_root or os.path.expanduser("~"))
        if not d:
            return
        try:
            j = self.queue.add_existing(d, self.selected_gid or "")
        except ValueError as e:
            QMessageBox.warning(self, "Add job folder", str(e))
            return
        self._add_message("info", f"Re-added {j.name} from {j.folder} ({j.count(F_DONE)} frames already done)")
        self.selected_id, self.selected_gid = j.id, None
        self._refresh_table(full=True)
        self._update_actions()

    def new_folder(self, move_job: Optional[Job] = None):
        dlg = FolderDialog(parent=self)
        if dlg.exec() != FolderDialog.DialogCode.Accepted:
            return
        g = self.queue.add_group(dlg.name.text().strip() or "Folder", dlg.root.text().strip())
        if move_job:
            self.queue.set_group(move_job, g["id"])
        else:
            self.selected_id, self.selected_gid = None, g["id"]
        self._refresh_table(full=True)

    def edit_folder(self):
        g = self.queue.group(self.selected_gid) if self.selected_gid else None
        if not g:
            return
        dlg = FolderDialog(g["name"], g.get("render_root", ""), self, title="Folder settings")
        if dlg.exec() == FolderDialog.DialogCode.Accepted:
            g["name"] = dlg.name.text().strip() or g["name"]
            g["render_root"] = dlg.root.text().strip()
            self.queue.save()
            self._refresh_table(full=True)

    def _move_to_group(self, j: Job, gid: str):
        self.queue.set_group(j, gid)
        self._refresh_table(full=True)

    def _on_tree_order(self, top, groups):
        self.queue.set_order(top, groups)
        self._refresh_table(full=True)

    def _on_check(self, kind, ident, checked):
        if kind == "j":
            j = self.queue.find(ident)
            if j:
                self.queue.set_enabled(j, checked)
        else:
            self.queue.set_group_enabled(ident, checked)
        self._refresh_table()
        self._update_actions()

    def _on_expanded(self, gid, state):
        g = self.queue.group(gid)
        if g:
            g["expanded"] = state
            self.queue.save()

    def remove_selected(self, confirm: bool = True):
        if self.selected_gid:
            g = self.queue.group(self.selected_gid)
            if g and (not confirm or QMessageBox.question(self, "Delete folder", f"Delete the folder '{g['name']}'?\n"
                                          "Its jobs move to the top level of the queue.") \
                    == QMessageBox.StandardButton.Yes):
                self.queue.remove_group(g["id"])
                self.selected_gid = None
                self._refresh_table(full=True)
            return
        j = self._sel()
        if not j or self._is_current(j):
            return
        if confirm and QMessageBox.question(self, "Remove job", f"Remove '{j.name}' from the queue?\n"
                                "Rendered frames and data stay in its folder; adding the same file "
                                "to the same render folder again picks up where it left off.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.queue.remove(j)
        self.selected_id = None
        self._refresh_table(full=True)
        self._update_actions()

    def move_selected(self, d):
        j = self._sel()
        if j:
            self.queue.move(j, d)
        elif self.selected_gid:
            self.queue.move_group(self.selected_gid, d)
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

    def accept_changed(self):
        j = self._sel()
        if j and j.status == ON_HOLD:
            self.queue.accept_changed_blend(j)
            self._add_message("info", f"{j.name}: will continue with the changed .blend "
                                      f"({j.count(F_DONE)} finished frames kept)")
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
        dlg = SettingsDialog(self.settings, self, api=self.api)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            dlg.apply()
            self.console.setMaximumBlockCount(self.settings.console_lines)
            self.job_form.reload_installs()
            self._update_actions()
            if dlg.api_changed:
                self._start_api()
            self.machines_panel.refresh()
            if self.worker_view.worker:
                self.worker_view.worker.installs_info = []
                self.worker_view.worker.registered = False

    # ======================================================================
    # queue files
    # ======================================================================
    def _file_snapshot(self, path):
        snap = JobQueue.read_file_snapshot(path)
        if snap:
            snap.pop("_expanded", None)
        return snap

    def _is_modified(self) -> bool:
        if self._saved_snapshot is None:
            return bool(self.queue.jobs or self.queue.groups)
        return self.queue.snapshot() != self._saved_snapshot

    def _update_title(self):
        qf = self.settings.current_queue_file
        name = os.path.basename(qf) if qf else "Untitled queue"
        self.setWindowTitle(f"{name}[*] – {APP_NAME}")
        self.setWindowFilePath(qf or "")
        self.setWindowModified(self._is_modified())
        if hasattr(self, "a_save"):
            self.a_save.setEnabled(self._is_modified() or not qf)

    def _confirm_discard(self, what: str) -> bool:
        """Ask before replacing a queue with unsaved changes. True = go ahead."""
        if not self._is_modified():
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Save the changes to the current queue?")
        box.setInformativeText(f"Your changes will be lost if you {what} without saving. "
                               "Jobs and rendered frames always stay on disk.")
        save = box.addButton("Save…" if not self.settings.current_queue_file else "Save",
                             QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Don't Save", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save)
        box.exec()
        if box.clickedButton() is save:
            return self.save_queue()
        return box.clickedButton() is discard

    def _busy_rendering(self, what: str) -> bool:
        if self.runner.running:
            QMessageBox.information(self, APP_NAME, f"Stop the batch before you {what}.")
            return True
        return False

    def new_queue(self):
        if self._busy_rendering("start a new queue") or not self._confirm_discard("start a new queue"):
            return
        self.queue.clear()
        self.settings.current_queue_file = ""
        self.settings.save()
        self._saved_snapshot = None
        self.selected_id = self.selected_gid = None
        self._refresh_table(full=True)
        self._update_actions()
        self._update_title()

    def open_queue_dialog(self):
        start = os.path.dirname(self.settings.current_queue_file) if self.settings.current_queue_file else \
            (self.settings.last_blend_dir or os.path.expanduser("~"))
        path, _ = QFileDialog.getOpenFileName(self, "Open queue", start, f"Batch Renderer queue (*{QUEUE_EXT})")
        if path:
            self.open_queue_file(path)

    def open_queue_file(self, path: str):
        path = os.path.abspath(path)
        self.raise_()
        self.activateWindow()
        if path == self.settings.current_queue_file and not self._is_modified():
            return
        if self._busy_rendering("open another queue") or not self._confirm_discard("open another queue"):
            return
        try:
            warnings = self.queue.load_file(path)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Open queue", f"Could not open {os.path.basename(path)}:\n{e}")
            return
        self.settings.current_queue_file = path
        self.settings.add_recent(path)
        self.settings.save()
        self._saved_snapshot = self._file_snapshot(path)
        for w in warnings:
            self._add_message("warn", w)
        self._add_message("info", f"Opened queue {os.path.basename(path)} ({len(self.queue.jobs)} jobs)")
        self.selected_id = self.selected_gid = None
        self._refresh_table(full=True)
        self._update_actions()
        self._update_title()

    def save_queue(self) -> bool:
        if not self.settings.current_queue_file:
            return self.save_queue_as()
        return self._save_to(self.settings.current_queue_file)

    def save_queue_as(self) -> bool:
        start = self.settings.current_queue_file or os.path.join(
            self.settings.last_blend_dir or os.path.expanduser("~"), "queue" + QUEUE_EXT)
        path, _ = QFileDialog.getSaveFileName(self, "Save queue as", start, f"Batch Renderer queue (*{QUEUE_EXT})")
        if not path:
            return False
        if not path.lower().endswith(QUEUE_EXT):
            path += QUEUE_EXT
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        try:
            self.queue.save_file(path)
        except OSError as e:
            QMessageBox.warning(self, "Save queue", f"Could not save:\n{e}")
            return False
        self.settings.current_queue_file = os.path.abspath(path)
        self.settings.add_recent(path)
        self.settings.save()
        self._saved_snapshot = self._file_snapshot(path)
        self._add_message("info", f"Saved queue to {path}")
        self._update_title()
        return True

    def _fill_recent_menu(self):
        self.recent_menu.clear()
        items = [p for p in self.settings.recent_queue_files if os.path.isfile(p)]
        for p in items:
            a = self.recent_menu.addAction(os.path.basename(p))
            a.setToolTip(p)
            a.triggered.connect(lambda _=False, path=p: self.open_queue_file(path))
        if not items:
            a = self.recent_menu.addAction("No recent queues")
            a.setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear menu").triggered.connect(self._clear_recent)

    def _clear_recent(self):
        self.settings.recent_queue_files = []
        self.settings.save()

    # ======================================================================
    # API, status page, help
    # ======================================================================
    def _start_api(self):
        if not self.settings.api_enabled:
            self.api.stop()
            return
        if self.api.start():
            lan = self.settings.api_allow_lan or (self.settings.share_with_workers and self.settings.mode == "coordinator")
            where = "this computer and your home network" if lan else "this computer"
            self._add_message("info", f"Status page and API running on {where}: {self.api.url}")
        else:
            self._add_message("warn", f"Status page / API could not start: {self.api.error}. "
                                      "Choose another port in Settings.")

    # ======================================================================
    # coordinator / worker mode
    # ======================================================================
    def _update_mode_lock(self):
        busy = self.runner.running or self.worker_view.busy
        self.mode_box.setEnabled(not busy)
        self.mode_box.setToolTip("Locked while something is rendering." if busy else
                                 "Coordinator: keeps the queue, can share frames with workers.\n"
                                 "Worker: renders frames for a coordinator on the home network.")

    def _mode_changed(self, _i):
        mode = self.mode_box.currentData()
        if mode == self.settings.mode:
            return
        if self.runner.running or self.worker_view.busy:
            self.mode_box.blockSignals(True)
            self.mode_box.setCurrentIndex(1 if self.settings.mode == "worker" else 0)
            self.mode_box.blockSignals(False)
            return
        if mode == "worker" and not self.settings.installs:
            QMessageBox.information(self, APP_NAME, "Add a Blender install first (Settings → Blender).")
            self.mode_box.blockSignals(True)
            self.mode_box.setCurrentIndex(0)
            self.mode_box.blockSignals(False)
            return
        self.settings.mode = mode
        self.settings.save()
        self._apply_mode()
        self._start_api()       # a worker does not offer jobs on the network

    def _apply_mode(self, initial: bool = False):
        worker = self.settings.mode == "worker"
        self.stack.setCurrentIndex(1 if worker else 0)
        for a in self.tb_queue.actions():
            a.setVisible(not worker)
        self.tb_queue.setVisible(not worker)
        self.tb_settings.setVisible(worker)
        self.queue_menu.menuAction().setVisible(not worker)
        self.job_menu.menuAction().setVisible(not worker)
        for a in (self.a_start, self.a_stop_after, self.a_stop_now, self.a_cancel, self.a_add, self.a_new_queue,
                  self.a_open_queue, self.a_save, self.a_save_as, self.a_new_folder):
            if worker:
                a.setEnabled(False)
        if worker:
            self.worker_view.start(lambda k, p: self.bridge.event.emit(k, p))
            self.setWindowTitle(f"{APP_NAME} – Worker")
        else:
            if not initial:
                self.worker_view.stop(now=True)
            self._update_actions()
            self._update_title()

    def _after_api_change(self):
        self._refresh_table(full=True)
        self._update_actions()
        self._refresh_live()

    def open_status_page(self):
        if not self.api.running:
            QMessageBox.information(self, APP_NAME, "The status page is switched off or could not start – "
                                    "see Settings → Status page & API.")
            return
        QDesktopServices.openUrl(QUrl(self.api.url))

    def show_shortcuts(self):
        rows = [("⌘N", "New queue"), ("⌘O", "Open queue…"), ("⌘S / ⇧⌘S", "Save queue / Save as…"),
                ("⌘I", "Add .blend files…"), ("⇧⌘N", "New folder…"),
                ("⌫ / Delete", "Remove the selected job or folder (asks first)"),
                ("⌘⌫", "Remove without asking"), ("↩", "Open the job's render folder / folder settings"),
                ("Space", "Play the preview video"), ("⌘↑ / ⌘↓", "Move up / down"),
                ("⌘R", "Start"), ("⇧⌘.", "Stop after this frame"), ("⌘.", "Stop now"),
                ("⌘P", "Create preview"), ("⌘,", "Settings"), ("⌘/", "This list")]
        html = "<table cellspacing=6>" + "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows) + \
               "</table><p style='color:gray'>On Windows/Linux, ⌘ is Ctrl.</p>"
        QMessageBox.information(self, "Keyboard shortcuts", html)

    def open_docs(self):
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [os.path.join(getattr(sys, "_MEIPASS", ""), "docs"),
                 os.path.join(here, "..", "..", "docs")]
        for c in cands:
            p = os.path.join(c, "Blender_Batch_Renderer_Documentation.pdf")
            if os.path.isfile(p):
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(p)))
                return
        QMessageBox.information(self, APP_NAME, "The documentation PDF was not found next to the app.")

    def _enter_pressed(self):
        if self._sel():
            self.open_folder()
        elif self.selected_gid:
            self.edit_folder()

    def play_preview(self):
        j = self._sel()
        if not j:
            return
        if j.preview_path and os.path.exists(j.preview_path):
            self._open_path_file(j.preview_path)
        elif j.count(F_DONE):
            self.statusBar().showMessage("No preview yet – use Job ▸ Create preview (⌘P)", 6000)

    def open_report(self):
        j = self._sel()
        if j and (j.data_dir / "report.xlsx").exists():
            self._open_path_file(j.data_dir / "report.xlsx")

    def _quit_when_worker_idle(self):
        if self.worker_view.busy:
            QTimer.singleShot(1000, self._quit_when_worker_idle)
            return
        self._quit_when_idle = False
        self.close()

    def _open_path_file(self, p):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

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
        new.machines = list(old.machines or [])
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
        if self.settings.mode == "worker":
            if self.worker_view.busy:
                box = QMessageBox(self)
                box.setWindowTitle("Rendering")
                box.setText("This computer is rendering a frame for the coordinator.")
                box.setInformativeText("If you stop now, the coordinator gives the frame to another machine "
                                       "(or renders it when this one is back).")
                now = box.addButton("Stop now and quit", QMessageBox.ButtonRole.DestructiveRole)
                after = box.addButton("Finish this frame, then quit", QMessageBox.ButtonRole.AcceptRole)
                box.addButton("Keep rendering", QMessageBox.ButtonRole.RejectRole)
                box.exec()
                c = box.clickedButton()
                if c is now:
                    self.worker_view.stop(now=True)
                elif c is after:
                    self.worker_view.worker.set_accepting(False)
                    self._quit_when_idle = True
                    self._add_message("info", "Will quit after the current frame")
                    QTimer.singleShot(1000, self._quit_when_worker_idle)
                    e.ignore()
                    return
                else:
                    e.ignore()
                    return
            else:
                self.worker_view.stop(now=True)
            self.api.stop()
            e.accept()
            return
        if not self.runner.running:
            self.api.stop()
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
            self.api.stop()
            e.accept()
        elif clicked is after:
            self._quit_when_idle = True
            self.runner.stop_after_frame()
            self._add_message("info", "Will quit after the current frame")
            e.ignore()
        else:
            e.ignore()
