"""Settings dialog and the 'add files' dialog."""
from __future__ import annotations

import sys
from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QCursor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
                               QPushButton, QDialogButtonBox, QGroupBox, QCheckBox, QSpinBox, QDoubleSpinBox, QLabel,
                               QFileDialog, QMessageBox, QHeaderView, QAbstractItemView, QFormLayout,
                               QListWidget, QApplication, QLineEdit, QComboBox, QRadioButton, QTabWidget,
                               QWidget, QButtonGroup)

from ..installs import BlenderInstall, probe, scan
from ..models import AppSettings, JobSettings
from .widgets import JobSettingsForm


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None, api=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(820, 600)
        self.settings = settings
        self.api = api
        self.api_changed = False
        self.installs: List[BlenderInstall] = [BlenderInstall.from_dict(d) for d in settings.installs]
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs)

        def page(title):
            w = QWidget()
            tabs.addTab(w, title)
            return QVBoxLayout(w)

        lay = page("Blender")

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

        lay = page("Defaults for new jobs")
        g2 = QGroupBox("Defaults for new jobs")
        g2l = QVBoxLayout(g2)
        self.defaults = JobSettingsForm(lambda: self.installs, show_root=True, show_overrides=False)
        self.defaults.set_settings(JobSettings.from_dict(settings.defaults))
        g2l.addWidget(self.defaults)
        lay.addWidget(g2)
        lay.addStretch(1)

        lay = page("App")
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
        self.min_free = QDoubleSpinBox()
        self.min_free.setRange(0, 100000)
        self.min_free.setDecimals(1)
        self.min_free.setSuffix(" GB")
        self.min_free.setValue(float(settings.min_free_gb))
        self.min_free.setToolTip("The batch stops (keeping all finished frames) when the render drive has less "
                                 "free space than this, or less than 3 frames' worth.")
        f3.addRow("Stop when free disk space is below", self.min_free)
        lay.addWidget(g3)
        lay.addStretch(1)

        # ---- status page & API ------------------------------------------------
        lay = page("Status page && API")
        g4 = QGroupBox("Status page & API")
        f4 = QFormLayout(g4)
        self.api_enabled = QCheckBox("Run the status page and API")
        self.api_enabled.setChecked(settings.api_enabled)
        self.api_port = QSpinBox()
        self.api_port.setRange(1024, 65535)
        self.api_port.setValue(int(settings.api_port))
        self.api_lan = QCheckBox("Allow other devices on my home network (phone, other computers)")
        self.api_lan.setChecked(settings.api_allow_lan)
        self.api_key = QLineEdit(settings.api_key)
        self.api_key.setReadOnly(True)
        new_key = QPushButton("New key")
        new_key.clicked.connect(self._new_key)
        krow = QHBoxLayout()
        krow.addWidget(self.api_key, 1)
        krow.addWidget(new_key)
        f4.addRow(self.api_enabled)
        f4.addRow("Port", self.api_port)
        f4.addRow(self.api_lan)
        f4.addRow("Key for other devices", krow)
        self.api_info = QLabel()
        self.api_info.setWordWrap(True)
        self.api_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.api_info.setOpenExternalLinks(True)
        f4.addRow(self.api_info)
        copy = QPushButton("Copy link for other devices")
        copy.clicked.connect(self._copy_link)
        f4.addRow(copy)
        lay.addWidget(g4)
        lay.addWidget(QLabel(
            "The status page shows the queue and live progress in any web browser. The API lets other tools – "
            "e.g. the “Send to Batch Renderer” add-on for Blender – add jobs and control the batch.\n\n"
            "From outside your home network the app is not reachable. For that, use a VPN such as Tailscale "
            "instead of opening a port on your router."))
        lay.addStretch(1)
        for w in (self.api_enabled, self.api_lan):
            w.toggled.connect(self._api_info)
        self.api_port.valueChanged.connect(self._api_info)
        self._api_info()

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)
        self._fill()

    def _new_key(self):
        import secrets
        self.api_key.setText(secrets.token_urlsafe(18))
        self._api_info()

    def _lan_links(self):
        if not self.api:
            return []
        old = (self.settings.api_port, self.settings.api_key)
        self.settings.api_port, self.settings.api_key = self.api_port.value(), self.api_key.text()
        try:
            return self.api.lan_urls()
        finally:
            self.settings.api_port, self.settings.api_key = old

    def _api_info(self):
        port = self.api_port.value()
        if not self.api_enabled.isChecked():
            self.api_info.setText("Off.")
            return
        lines = [f"On this computer: <a href='http://127.0.0.1:{port}/'>http://127.0.0.1:{port}/</a>"]
        if self.api_lan.isChecked():
            links = self._lan_links()
            if links:
                lines.append("Other devices: " + "<br>".join(f"<a href='{u}'>{u}</a>" for u in links))
        self.api_info.setText("<br>".join(lines))

    def _copy_link(self):
        links = self._lan_links() if self.api_lan.isChecked() else []
        text = links[0] if links else f"http://127.0.0.1:{self.api_port.value()}/"
        QApplication.clipboard().setText(text)

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
                                                       "frame_timeout_min", "make_preview", "preflight",
                                                       "restart_every", "ignore_addons", "transfer",
                                                       "consistency_check")})
        s.keep_awake = self.keep_awake.isChecked()
        s.auto_resume = self.auto_resume.isChecked()
        s.console_lines = self.console_lines.value()
        s.min_free_gb = self.min_free.value()
        new_api = (self.api_enabled.isChecked(), self.api_port.value(), self.api_lan.isChecked(), self.api_key.text())
        self.api_changed = new_api != (s.api_enabled, s.api_port, s.api_allow_lan, s.api_key)
        s.api_enabled, s.api_port, s.api_allow_lan, s.api_key = new_api
        s.save()


class FolderDialog(QDialog):
    """Create / edit a queue folder."""

    def __init__(self, name: str = "", render_root: str = "", parent=None, title="New folder"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 160)
        form = QFormLayout(self)
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("e.g. Project X – shots")
        form.addRow("Name", self.name)
        self.root = QLineEdit(render_root)
        self.root.setPlaceholderText("optional: default render folder for files added to this folder")
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.root, 1)
        row.addWidget(b)
        form.addRow("Render folder", row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Render folder", self.root.text())
        if d:
            self.root.setText(d)


class AddJobsDialog(QDialog):
    def __init__(self, files: List[str], settings: AppSettings, installs, parent=None,
                 groups=None, group_id: str = ""):
        super().__init__(parent)
        self.settings = settings
        self.files = files
        self.setWindowTitle(f"Add {len(files)} file(s)")
        self.resize(720, 620)
        lay = QVBoxLayout(self)
        lst = QListWidget()
        lst.addItems(files)
        lst.setMaximumHeight(120)
        lay.addWidget(lst)

        self.groups = groups or []
        self.folder = QComboBox()
        self.folder.addItem("— top level —", "")
        for g in self.groups:
            self.folder.addItem(g["name"], g["id"])
        i = self.folder.findData(group_id)
        self.folder.setCurrentIndex(max(0, i))
        frow = QFormLayout()
        frow.addRow("Queue folder", self.folder)
        lay.addLayout(frow)

        # where the output goes
        gr = QGroupBox("Render folder")
        gl = QVBoxLayout(gr)
        self.r_next = QRadioButton("Next to each .blend file  (a folder called “render” beside the file)")
        self.r_custom = QRadioButton("Custom folder:")
        bg = QButtonGroup(self)
        bg.addButton(self.r_next)
        bg.addButton(self.r_custom)
        self.custom_root = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        crow = QHBoxLayout()
        crow.addWidget(self.r_custom)
        crow.addWidget(self.custom_root, 1)
        crow.addWidget(browse)
        gl.addWidget(self.r_next)
        gl.addLayout(crow)
        self.example = QLabel()
        self.example.setWordWrap(True)
        self.example.setStyleSheet("color: gray;")
        gl.addWidget(self.example)
        lay.addWidget(gr)
        self.custom_root.textEdited.connect(lambda _: self.r_custom.setChecked(True))
        for w in (self.r_next, self.r_custom):
            w.toggled.connect(self._update_example)
        self.custom_root.textChanged.connect(self._update_example)

        self.form = JobSettingsForm(lambda: installs, show_root=False)
        self._defaults = JobSettings.from_dict(settings.defaults)
        self.form.set_settings(self._defaults)
        self.folder.currentIndexChanged.connect(self._folder_changed)
        self._folder_changed()
        lay.addWidget(self.form)
        lay.addWidget(QLabel("Overrides apply to all files added here; leave them off to use each file's own settings."))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Add to queue")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self):
        import os
        start = self.custom_root.text() or self.settings.last_render_root or os.path.dirname(self.files[0])
        d = QFileDialog.getExistingDirectory(self, "Render folder", start)
        if d:
            self.custom_root.setText(d)
            self.r_custom.setChecked(True)

    def _folder_changed(self):
        gid = self.folder.currentData()
        g = next((x for x in self.groups if x["id"] == gid), None)
        preset = (g or {}).get("render_root") or self._defaults.render_root
        if preset:
            self.custom_root.setText(preset)
            self.r_custom.setChecked(True)
        else:
            self.r_next.setChecked(True)
        self._update_example()

    def _update_example(self):
        import os
        f = self.files[0]
        stem = os.path.splitext(os.path.basename(f))[0]
        if self.r_next.isChecked():
            out = os.path.join(os.path.dirname(f), "render", stem)
        else:
            out = os.path.join(self.custom_root.text() or "…", stem)
        self.example.setText(f"e.g. {os.path.basename(f)} → {out}")

    def _accept(self):
        if self.r_custom.isChecked() and not self.custom_root.text().strip():
            QMessageBox.information(self, "Render folder", "Choose a custom folder or use “next to each .blend file”.")
            return
        self.accept()

    def render_root(self) -> str:
        """"" means: a render folder next to each .blend file."""
        return self.custom_root.text().strip() if self.r_custom.isChecked() else ""

    @property
    def group_id(self) -> str:
        return self.folder.currentData() or ""
