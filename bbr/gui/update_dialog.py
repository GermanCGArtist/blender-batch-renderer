"""Help → Check for updates…"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QRadioButton,
                               QButtonGroup, QMessageBox)

from .. import updater


class _Bridge(QObject):
    done = Signal(str, object)


class UpdateDialog(QDialog):
    def __init__(self, settings, busy, parent=None):
        """busy() -> reason text if something is rendering (then nothing is installed)."""
        super().__init__(parent)
        self.setWindowTitle("Updates")
        self.resize(560, 300)
        self.settings = settings
        self.busy = busy
        self.info = None
        self.installed = False
        self.bridge = _Bridge()
        self.bridge.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        lay = QVBoxLayout(self)
        cur = updater.current()
        self.cur = QLabel()
        self.cur.setTextFormat(Qt.TextFormat.RichText)
        txt = f"Installed: <b>{updater.label(cur)}</b>" + ("  (built into the app)" if cur.get("builtin") else "")
        if cur.get("note"):
            txt += f"<br><span style='color:#e67e22'>{cur['note']}</span>"
        self.cur.setText(txt)
        lay.addWidget(self.cur)
        row = QHBoxLayout()
        row.addWidget(QLabel("Channel:"))
        self.r_stable = QRadioButton("Stable")
        self.r_stable.setToolTip("Versions that were tried and approved (the 'release' branch)")
        self.r_alpha = QRadioButton("Alpha")
        self.r_alpha.setToolTip("Every change as soon as it is made – experimental, may have bugs "
                                "(the 'main' branch)")
        g = QButtonGroup(self)
        g.addButton(self.r_stable)
        g.addButton(self.r_alpha)
        (self.r_alpha if settings.update_channel == "alpha" else self.r_stable).setChecked(True)
        self.r_stable.toggled.connect(self._channel_changed)
        row.addWidget(self.r_stable)
        row.addWidget(self.r_alpha)
        row.addStretch(1)
        lay.addLayout(row)
        self.status = QLabel("Press Check to look for a newer version on GitHub.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.status, 1)
        btns = QHBoxLayout()
        self.b_check = QPushButton("Check")
        self.b_check.setToolTip("Ask GitHub for the newest version of the chosen channel")
        self.b_check.clicked.connect(self._check)
        self.b_install = QPushButton("Install")
        self.b_install.setToolTip("Download only the app code (1-2 MB), check it and install it next to the "
                                  "current version")
        self.b_install.setEnabled(False)
        self.b_install.clicked.connect(self._install)
        self.b_prev = QPushButton("Use previous version")
        prev = updater.previous()
        self.b_prev.setEnabled(bool(prev))
        self.b_prev.setToolTip(f"Go back to {prev}" if prev else "No previous version installed")
        self.b_prev.clicked.connect(self._previous)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        for b in (self.b_check, self.b_install, self.b_prev):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(close)
        lay.addLayout(btns)

    def _channel(self):
        return "alpha" if self.r_alpha.isChecked() else "stable"

    def _channel_changed(self):
        self.settings.update_channel = self._channel()
        self.settings.save()
        self.info = None
        self.b_install.setEnabled(False)
        self.status.setText("Press Check to look for a newer version on GitHub.")

    def _run(self, kind, fn):
        def work():
            try:
                self.bridge.done.emit(kind, fn())
            except updater.UpdateError as e:
                self.bridge.done.emit("error", str(e))
            except Exception as e:  # never crash the window
                self.bridge.done.emit("error", f"{type(e).__name__}: {e}")
        threading.Thread(target=work, daemon=True).start()

    def _check(self):
        self.b_check.setEnabled(False)
        self.b_install.setEnabled(False)
        self.status.setText("Checking GitHub …")
        ch = self._channel()
        self._run("checked", lambda: updater.check(ch))

    def _install(self):
        if not self.info:
            return
        why = self.busy()
        if why:
            QMessageBox.information(self, "Updates", f"{why} – install the update when it is finished or stopped.")
            return
        self.b_install.setEnabled(False)
        self.b_check.setEnabled(False)
        self.status.setText(f"Installing {updater.label(self.info)} …")
        info = self.info
        self._run("installed", lambda: updater.install(info))

    def _previous(self):
        why = self.busy()
        if why:
            QMessageBox.information(self, "Updates", f"{why} – switch versions when it is finished or stopped.")
            return
        try:
            name = updater.use_previous()
        except updater.UpdateError as e:
            QMessageBox.warning(self, "Updates", str(e))
            return
        self.installed = True
        self.status.setText(f"Next start uses {'the built-in version' if not name else name}.")
        self._offer_restart()

    def _done(self, kind, result):
        self.b_check.setEnabled(True)
        if kind == "error":
            self.status.setText(f"<span style='color:#dc3545'>{result}</span>")
            return
        if kind == "checked":
            self.info = result
            if result["is_current"]:
                self.status.setText(f"You have the newest {result['channel']} version ({updater.label(result)}).")
            elif not result["fits"]:
                self.status.setText(f"<b>{updater.label(result)}</b> is available, but it needs a newer app: "
                                    "download the project again and run the build script once.")
            elif not result["newer"]:
                self.status.setText(f"The newest {result['channel']} version is {updater.label(result)} – "
                                    "not newer than yours.")
                self.b_install.setEnabled(True)
                self.b_install.setText("Install anyway")
            else:
                warn = "<br><span style='color:#e67e22'>Alpha: experimental, may have bugs.</span>" \
                    if result["channel"] == "alpha" else ""
                self.status.setText(f"Available: <b>{updater.label(result)}</b> from {result['date'][:10]}<br>"
                                    f"{result['message']}{warn}")
                self.b_install.setEnabled(True)
                self.b_install.setText("Install")
        elif kind == "installed":
            self.installed = True
            self.status.setText(f"Installed {result}. It is used from the next start on.")
            self.b_prev.setEnabled(True)
            self._offer_restart()

    def _offer_restart(self):
        r = QMessageBox.question(self, "Updates", "Restart the app now to use it?")
        if r == QMessageBox.StandardButton.Yes:
            self.done(2)        # the window restarts the app
