"""Reusable widgets: job settings form, charts, thumbnail."""
from __future__ import annotations

import os
from typing import List, Optional

import math

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import QPixmap, QPainter, QPalette, QPen, QColor, QFont, QFontMetrics, QPolygonF
from PySide6.QtWidgets import (QWidget, QFormLayout, QLineEdit, QPushButton, QHBoxLayout, QComboBox,
                               QCheckBox, QSpinBox, QDoubleSpinBox, QLabel, QFileDialog, QSizePolicy,
                               QVBoxLayout)

from ..models import JobSettings

SERIES_COLORS = ["#3d8bfd", "#e67e22", "#2fa84f", "#c678dd"]


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


def _nice_step(span: float, ticks: int = 4) -> float:
    if span <= 0:
        return 1.0
    raw = span / ticks
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _fmt(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}k".replace(".0k", "k")
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}"


class LineChart(QWidget):
    """A small, self-drawn line chart (no QtCharts needed, which keeps the app small)."""

    def __init__(self, title: str, y_title: str, names: List[str]):
        super().__init__()
        self.title = title
        self.y_title = y_title
        self.names = names
        self.xs: List[float] = []
        self.ys: List[List[Optional[float]]] = [[] for _ in names]
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def apply_theme(self):
        self.update()

    def set_data(self, xs: List[float], ys_per_series: List[List[Optional[float]]]):
        self.xs = list(xs)
        self.ys = [list(y) for y in ys_per_series] + [[] for _ in range(len(self.names) - len(ys_per_series))]
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text = pal.color(QPalette.ColorRole.WindowText)
        bg = pal.color(QPalette.ColorRole.Base)
        grid = QColor(text)
        grid.setAlpha(40)
        muted = QColor(text)
        muted.setAlpha(150)
        p.fillRect(self.rect(), bg)

        small = QFont(self.font())
        small.setPointSizeF(max(8.0, small.pointSizeF() * 0.85))
        bold = QFont(self.font())
        bold.setBold(True)
        fm = QFontMetrics(small)
        W, H = float(self.width()), float(self.height())

        p.setFont(bold)
        p.setPen(text)
        p.drawText(QRectF(8, 4, W - 16, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.title)
        # legend (top right)
        p.setFont(small)
        lx = W - 8
        if len(self.names) > 1:
            for i in reversed(range(len(self.names))):
                name = self.names[i]
                tw = fm.horizontalAdvance(name)
                lx -= tw
                p.setPen(muted)
                p.drawText(QRectF(lx, 4, tw + 2, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
                lx -= 18
                p.setPen(QPen(QColor(SERIES_COLORS[i % len(SERIES_COLORS)]), 3))
                p.drawLine(QPointF(lx, 14), QPointF(lx + 12, 14))
                lx -= 12

        pts = [(x, y) for ys in self.ys for x, y in zip(self.xs, ys) if y is not None]
        left, top, right, bottom = 8.0, 30.0, W - 12, H - 24
        if not pts:
            p.setPen(muted)
            p.drawText(QRectF(0, top, W, bottom - top), Qt.AlignmentFlag.AlignCenter, "no finished frames yet")
            p.end()
            return

        x_lo, x_hi = min(x for x, _ in pts), max(x for x, _ in pts)
        if x_hi == x_lo:
            x_hi = x_lo + 1
        y_hi = max(y for _, y in pts) or 1.0
        step = _nice_step(y_hi)
        y_top = math.ceil(y_hi * 1.05 / step) * step
        ticks = [i * step for i in range(int(round(y_top / step)) + 1)]
        label_w = max(fm.horizontalAdvance(_fmt(t)) for t in ticks)
        left += label_w + 6

        def X(x):
            return left + (x - x_lo) / (x_hi - x_lo) * (right - left)

        def Y(y):
            return bottom - y / y_top * (bottom - top)

        # grid + y labels
        for t in ticks:
            y = Y(t)
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(left, y), QPointF(right, y))
            p.setPen(muted)
            p.drawText(QRectF(4, y - 8, label_w + 2, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       _fmt(t))
        # x labels: first / last frame + axis titles
        p.setPen(muted)
        p.drawText(QRectF(left, bottom + 4, 80, 16), Qt.AlignmentFlag.AlignLeft, f"frame {int(x_lo)}")
        p.drawText(QRectF(right - 80, bottom + 4, 80, 16), Qt.AlignmentFlag.AlignRight, f"{int(x_hi)}")
        p.drawText(QRectF(left, bottom + 4, right - left, 16), Qt.AlignmentFlag.AlignHCenter, self.y_title)

        # series (gaps where a value is missing)
        for i, ys in enumerate(self.ys):
            color = QColor(SERIES_COLORS[i % len(SERIES_COLORS)])
            p.setPen(QPen(color, 2))
            seg: List[QPointF] = []
            pts_i = sorted(((x, y) for x, y in zip(self.xs, ys)), key=lambda t: t[0])
            for x, y in pts_i + [(None, None)]:
                if y is None:
                    if len(seg) > 1:
                        p.drawPolyline(QPolygonF(seg))
                    elif len(seg) == 1:
                        p.setBrush(color)
                        p.drawEllipse(seg[0], 2.5, 2.5)
                    seg = []
                else:
                    seg.append(QPointF(X(x), Y(y)))
            if 1 < len(pts_i) <= 60:
                p.setBrush(color)
                p.setPen(Qt.PenStyle.NoPen)
                for x, y in pts_i:
                    if y is not None:
                        p.drawEllipse(QPointF(X(x), Y(y)), 2.2, 2.2)
                p.setBrush(Qt.BrushStyle.NoBrush)
        p.end()
