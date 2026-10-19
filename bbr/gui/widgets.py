"""Reusable widgets: job settings form, charts, thumbnail."""
from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import Qt, QPointF, QMargins, Signal
from PySide6.QtGui import QPixmap, QPainter, QPalette
from PySide6.QtWidgets import (QWidget, QFormLayout, QLineEdit, QPushButton, QHBoxLayout, QComboBox,
                               QCheckBox, QSpinBox, QDoubleSpinBox, QLabel, QFileDialog, QSizePolicy,
                               QVBoxLayout)

from ..models import JobSettings

try:
    from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
    HAVE_CHARTS = True
except ImportError:  # QtCharts is part of the normal PySide6 wheel, but be safe
    HAVE_CHARTS = False


def is_dark(widget: QWidget) -> bool:
    return widget.palette().color(QPalette.ColorRole.Window).lightness() < 128


class OptionalSpin(QWidget):
    """Checkbox + spinbox: unchecked means 'use the value saved in the .blend'."""

    def __init__(self, lo: int, hi: int, default: int, suffix: str = ""):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.check = QCheckBox("override")
        self.spin = QSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setValue(default)
        if suffix:
            self.spin.setSuffix(suffix)
        self.spin.setEnabled(False)
        self.check.toggled.connect(self.spin.setEnabled)
        lay.addWidget(self.check)
        lay.addWidget(self.spin, 1)

    def value(self) -> Optional[int]:
        return self.spin.value() if self.check.isChecked() else None

    def set_value(self, v: Optional[int]):
        self.check.setChecked(v is not None)
        if v is not None:
            self.spin.setValue(int(v))


class JobSettingsForm(QWidget):
    changed = Signal()

    def __init__(self, installs_provider, show_root: bool = True, show_overrides: bool = True):
        super().__init__()
        self.installs_provider = installs_provider
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.root = QLineEdit()
        self.root.setPlaceholderText("empty = a 'render' folder next to each .blend file")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.root, 1)
        row.addWidget(browse)
        self._root_row = QWidget()
        self._root_row.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        if show_root:
            form.addRow("Render folder", self._root_row)

        self.blender = QComboBox()
        form.addRow("Blender", self.blender)

        self.frame_start = OptionalSpin(-1_000_000, 1_000_000, 1)
        self.frame_end = OptionalSpin(-1_000_000, 1_000_000, 250)
        self.samples = OptionalSpin(1, 1_000_000, 128)
        self.res_pct = OptionalSpin(1, 1000, 100, " %")
        if show_overrides:
            form.addRow("Start frame", self.frame_start)
            form.addRow("End frame", self.frame_end)
            form.addRow("Samples", self.samples)
            form.addRow("Resolution", self.res_pct)

        self.max_attempts = QSpinBox()
        self.max_attempts.setRange(1, 100)
        form.addRow("Attempts per frame", self.max_attempts)
        self.retry_delay = QSpinBox()
        self.retry_delay.setRange(0, 24 * 3600)
        self.retry_delay.setSuffix(" s")
        form.addRow("Wait before retry", self.retry_delay)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0, 7 * 24 * 60)
        self.timeout.setDecimals(1)
        self.timeout.setSuffix(" min")
        self.timeout.setSpecialValueText("no limit")
        form.addRow("Frame timeout", self.timeout)
        self.restart_every = QSpinBox()
        self.restart_every.setRange(0, 100000)
        self.restart_every.setSuffix(" frames")
        self.restart_every.setSpecialValueText("never")
        self.restart_every.setToolTip("Start a fresh Blender process every N frames. Helps when Blender's memory "
                                      "grows over a long sequence; costs one file load each time.")
        form.addRow("Restart Blender every", self.restart_every)
        self.preview = QCheckBox("Make an MP4 preview when the job finishes")
        form.addRow("", self.preview)
        self.preflight = QCheckBox("Check the file first (missing textures, unbaked sims …)")
        form.addRow("", self.preflight)
        self.reload_installs()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Render folder", self.root.text() or os.path.expanduser("~"))
        if d:
            self.root.setText(d)

    def reload_installs(self):
        cur = self.blender.currentData()
        self.blender.clear()
        self.blender.addItem("Auto (match the version the file was saved with)", "auto")
        for inst in self.installs_provider():
            self.blender.addItem(f"{inst.label}  –  {inst.path}", inst.path)
        if cur:
            i = self.blender.findData(cur)
            if i >= 0:
                self.blender.setCurrentIndex(i)

    def set_settings(self, s: JobSettings):
        self.root.setText(s.render_root or "")
        i = self.blender.findData(s.blender or "auto")
        if i < 0:
            self.blender.addItem(f"(missing) {s.blender}", s.blender)
            i = self.blender.count() - 1
        self.blender.setCurrentIndex(i)
        self.frame_start.set_value(s.frame_start)
        self.frame_end.set_value(s.frame_end)
        self.samples.set_value(s.samples)
        self.res_pct.set_value(s.resolution_pct)
        self.max_attempts.setValue(int(s.max_attempts))
        self.retry_delay.setValue(int(s.retry_delay_s))
        self.timeout.setValue(float(s.frame_timeout_min or 0))
        self.preview.setChecked(bool(s.make_preview))
        self.preflight.setChecked(bool(s.preflight))
        self.restart_every.setValue(int(s.restart_every or 0))

    def get_settings(self) -> JobSettings:
        return JobSettings(
            render_root=self.root.text().strip(),
            blender=self.blender.currentData() or "auto",
            frame_start=self.frame_start.value(), frame_end=self.frame_end.value(),
            samples=self.samples.value(), resolution_pct=self.res_pct.value(),
            max_attempts=self.max_attempts.value(), retry_delay_s=self.retry_delay.value(),
            frame_timeout_min=self.timeout.value(), make_preview=self.preview.isChecked(),
            preflight=self.preflight.isChecked(), restart_every=self.restart_every.value())


class Thumbnail(QLabel):
    def __init__(self):
        super().__init__("No frame yet")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pix: Optional[QPixmap] = None
        self._path = None

    def show_file(self, path: Optional[str]):
        if path == self._path:
            return
        self._path = path
        if not path or not os.path.exists(path):
            self._pix = None
            self.setText("No frame yet")
            return
        pix = QPixmap(path)
        if pix.isNull():
            self._pix = None
            self.setText(f"No thumbnail for {os.path.splitext(path)[1]} files\n{os.path.basename(path)}")
            return
        self._pix = pix
        self._rescale()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()

    def _rescale(self):
        if self._pix:
            self.setPixmap(self._pix.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))


class LineChart(QWidget):
    """A small line chart with one or more series (falls back to a label)."""

    def __init__(self, title: str, y_title: str, names: List[str]):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.names = names
        if not HAVE_CHARTS:
            lay.addWidget(QLabel(f"{title}: charts unavailable (QtCharts missing)"))
            self.chart = None
            return
        self.chart = QChart()
        self.chart.setTitle(title)
        self.chart.setMargins(QMargins(4, 4, 4, 4))
        self.series = []
        self.ax = QValueAxis()
        self.ax.setTitleText("frame")
        self.ax.setLabelFormat("%d")
        self.ay = QValueAxis()
        self.ay.setTitleText(y_title)
        self.chart.addAxis(self.ax, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(self.ay, Qt.AlignmentFlag.AlignLeft)
        for n in names:
            s = QLineSeries()
            s.setName(n)
            self.chart.addSeries(s)
            s.attachAxis(self.ax)
            s.attachAxis(self.ay)
            self.series.append(s)
        self.chart.legend().setVisible(len(names) > 1)
        view = QChartView(self.chart)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(170)
        lay.addWidget(view)
        self.apply_theme()

    def apply_theme(self):
        if self.chart is not None:
            self.chart.setTheme(QChart.ChartTheme.ChartThemeDark if is_dark(self)
                                else QChart.ChartTheme.ChartThemeLight)

    def set_data(self, xs: List[float], ys_per_series: List[List[Optional[float]]]):
        if self.chart is None:
            return
        allx, ally = [], []
        for s, ys in zip(self.series, ys_per_series):
            pts = [QPointF(float(x), float(y)) for x, y in zip(xs, ys) if y is not None]
            s.replace(pts)
            allx += [p.x() for p in pts]
            ally += [p.y() for p in pts]
        if allx:
            lo, hi = min(allx), max(allx)
            self.ax.setRange(lo, hi if hi > lo else lo + 1)
            top = max(ally) if ally else 1
            self.ay.setRange(0, top * 1.1 if top > 0 else 1)
