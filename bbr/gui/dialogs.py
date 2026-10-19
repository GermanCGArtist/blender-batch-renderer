"""Settings dialog and the 'add files' dialog."""
from __future__ import annotations

import sys
from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QCursor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
                               QPushButton, QDialogButtonBox, QGroupBox, QCheckBox, QSpinBox, QLabel,
                               QFileDialog, QMessageBox, QHeaderView, QAbstractItemView, QFormLayout,
                               QListWidget, QApplication)

from ..installs import BlenderInstall, probe, scan
from ..models import AppSettings, JobSettings
from .widgets import JobSettingsForm


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(820, 640)
        self.settings = settings
        self.installs: List[BlenderInstall] = [BlenderInstall.from_dict(d) for d in settings.installs]
        lay = QVBoxLayout(self)

        g = QGroupBox("Blender installs")
        gl = QVBoxLayout(g)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Version", "Label", "Path"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        gl.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (("Add…", self._add), ("Remove", self._remove), ("Scan for installs", self._scan)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        gl.addLayout(row)
        gl.addWidget(QLabel("“Auto” picks the install matching the version a file was saved with, "
                            "otherwise the next newer one."))
        lay.addWidget(g)

        g2 = QGroupBox("Defaults for new jobs")
        g2l = QVBoxLayout(g2)
        self.defaults = JobSettingsForm(lambda: self.installs, show_root=True, show_overrides=False)
        self.defaults.set_settings(JobSettings.from_dict(settings.defaults))
        g2l.addWidget(self.defaults)
        lay.addWidget(g2)

        g3 = QGroupBox("App")
        f3 = QFormLayout(g3)
        self.keep_awake = QCheckBox("Keep the computer awake while rendering")
        self.keep_awake.setChecked(settings.keep_awake)
        self.auto_resume = QCheckBox("Resume an interrupted batch automatically when the app starts")
        self.auto_resume.setChecked(settings.auto_resume)
        self.console_lines = QSpinBox()
        self.console_lines.setRange(500, 200_000)
        self.console_lines.setValue(settings.console_lines)
        f3.addRow(self.keep_awake)
        f3.addRow(self.auto_resume)
        f3.addRow("Console lines kept per job", self.console_lines)
        lay.addWidget(g3)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill()

    def _fill(self):
        self.installs.sort(key=lambda i: i.version, reverse=True)
        self.table.setRowCount(len(self.installs))
        for r, i in enumerate(self.installs):
            for c, v in enumerate((".".join(map(str, i.version)), i.label, i.path)):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.resizeColumnsToContents()
        self.defaults.reload_installs()

    def _probe_with_cursor(self, fn):
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            return fn()
        finally:
            QApplication.restoreOverrideCursor()

    def _add(self):
        if sys.platform == "darwin":
            path, _ = QFileDialog.getOpenFileName(self, "Choose Blender.app", "/Applications",
                                                  "Applications (*.app);;All files (*)")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose the Blender executable")
        if not path:
            return
        inst = self._probe_with_cursor(lambda: probe(path))
        if not inst:
            QMessageBox.warning(self, "Not Blender", f"Could not read a Blender version from\n{path}")
            return
        self.installs = [i for i in self.installs if i.executable != inst.executable] + [inst]
        self._fill()

    def _remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.installs[r]
        self._fill()

    def _scan(self):
        found = self._probe_with_cursor(scan)
        known = {i.executable for i in self.installs}
        new = [i for i in found if i.executable not in known]
        self.installs += new
        self._fill()
        QMessageBox.information(self, "Scan", f"Found {len(found)} install(s), {len(new)} new.")

    def apply(self):
        s = self.settings
        s.installs = [i.to_dict() for i in self.installs]
        d = self.defaults.get_settings()
        s.defaults.update({k: getattr(d, k) for k in ("render_root", "blender", "max_attempts", "retry_delay_s",
                                                       "frame_timeout_min", "make_preview", "preflight")})
        s.keep_awake = self.keep_awake.isChecked()
        s.auto_resume = self.auto_resume.isChecked()
        s.console_lines = self.console_lines.value()
        s.save()


class AddJobsDialog(QDialog):
    def __init__(self, files: List[str], settings: AppSettings, installs, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Add {len(files)} file(s)")
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        lst = QListWidget()
        lst.addItems(files)
        lst.setMaximumHeight(140)
        lay.addWidget(lst)
        self.form = JobSettingsForm(lambda: installs)
        s = JobSettings.from_dict(settings.defaults)
        if settings.last_render_root and not s.render_root:
            s.render_root = settings.last_render_root
        self.form.set_settings(s)
        lay.addWidget(self.form)
        lay.addWidget(QLabel("Each file gets its own folder inside the render folder. "
                             "Overrides apply to all files added here; leave them off to use each file's own settings."))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Add to queue")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
