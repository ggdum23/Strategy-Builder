"""Interactive equity, drawdown, and exposure charts.

PyQtGraph draws the series it is given. It does not read a run directory and
it does not calculate a performance statistic. The drawdown series, when
requested, is passed in already labeled as a picture of the equity column.
"""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

pg.setConfigOptions(antialias=True, useOpenGL=False)


class _DateAxis(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.labels: list[str] = []
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        out = []
        last = len(self.labels) - 1
        previous = None
        for value in values:
            index = int(round(float(value)))
            label = self.labels[index] if 0 <= index <= last else ""
            if label and label == previous:
                out.append("")
            else:
                out.append(label)
                previous = label or previous
        return out


class _NumberAxis(pg.AxisItem):
    """Display scale for a series that is already in dollars or a fraction."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.percent = False
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        step = abs(float(spacing) * float(scale)) or 1.0
        numbers = [float(value) * float(scale) for value in values]
        use_millions = any(abs(number) >= 1_000_000 for number in numbers)
        out = []
        for number in numbers:
            if self.percent:
                digits = 1 if step >= 0.1 else 2
                out.append(f"{number:.{digits}f}%")
            elif use_millions:
                digits = 2 if step >= 10_000 else 3
                out.append(f"{number / 1_000_000:.{digits}f}M")
            elif abs(number) >= 10_000 or step >= 1_000:
                digits = 0 if step >= 1_000 else 1
                out.append(f"{number / 1_000:.{digits}f}k")
            else:
                out.append(f"{number:,.0f}")
        return out


class ChartPanel(QWidget):
    def __init__(self):
        super().__init__()
        self._theme = None
        self._axis = _DateAxis(orientation="bottom")
        self._yaxis = _NumberAxis(orientation="left")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        tools = QHBoxLayout()
        self.caption = QLabel("")
        self.caption.setObjectName("muted")
        self.caption.setWordWrap(True)
        reset = QPushButton("Reset view")
        reset.clicked.connect(self._reset)
        tools.addWidget(self.caption, 1)
        tools.addWidget(reset)
        self.plot = pg.PlotWidget(axisItems={"bottom": self._axis, "left": self._yaxis})
        self.plot.showGrid(x=True, y=True, alpha=0.28)
        self.plot.setMenuEnabled(False)
        self.plot.setClipToView(True)
        self.plot.setMinimumHeight(248)
        self.setMinimumHeight(292)
        self.plot.setLabel("bottom", "session")
        self.plot.getAxis("left").setWidth(68)
        self._legend = self.plot.addLegend(offset=(8, 8))
        self._cursor = QLabel(" ")
        self._cursor.setObjectName("muted")
        layout.addLayout(tools)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self._cursor)
        self._points: list[tuple[str, list[float], list[float]]] = []
        self.plot.scene().sigMouseMoved.connect(self._moved)

    def apply_theme(self, theme) -> None:
        self._theme = theme
        self.plot.setBackground(theme.chart_bg)
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen(pg.mkPen(theme.chart_grid))
            axis.setTextPen(pg.mkPen(theme.chart_fg))
        if self._legend is not None:
            fill = QColor(theme.chart_bg)
            fill.setAlpha(232)
            self._legend.setBrush(pg.mkBrush(fill))
            self._legend.setPen(pg.mkPen(theme.border))
            if hasattr(self._legend, "setLabelTextColor"):
                self._legend.setLabelTextColor(theme.chart_fg)

    def set_lines(self, lines, ylabel: str, sessions: list[str] | None, note: str, *, percent: bool = False) -> None:
        """``lines`` is (name, x, y, color). Empty input clears the picture."""
        self.plot.clear()
        self._legend = self.plot.addLegend(offset=(8, 8))
        if self._theme is not None:
            self.apply_theme(self._theme)
        self._axis.labels = list(sessions or [])
        self._yaxis.percent = percent
        self._points = []
        self.caption.setText(note)
        self.plot.setLabel("left", ylabel)
        if not lines:
            self._cursor.setText("No series in this report.")
            return
        for name, xs, ys, color in lines:
            x_values = [float(item) for item in xs]
            y_values = []
            for item in ys:
                try:
                    number = float(item)
                except (TypeError, ValueError):
                    number = float("nan")
                y_values.append(number * 100.0 if percent else number)
            item = self.plot.plot(
                x_values,
                y_values,
                pen=pg.mkPen(color, width=1.7),
                name=name,
            )
            item.setClipToView(True)
            item.setDownsampling(auto=True, method="peak")
            self._points.append((name, x_values, y_values))
        self.plot.enableAutoRange()
        self._cursor.setText("Drag to pan. Wheel to zoom.")

    def _reset(self) -> None:
        self.plot.enableAutoRange()

    def _moved(self, position) -> None:
        try:
            self._moved_inner(position)
        except Exception:
            return

    def _moved_inner(self, position) -> None:
        if not self._points:
            return
        view = self.plot.plotItem.vb
        if view is None or not self.plot.sceneBoundingRect().contains(position):
            return
        point = view.mapSceneToView(position)
        index = int(round(point.x()))
        bits = []
        for name, xs, ys in self._points:
            if not xs:
                continue
            nearest = min(range(len(xs)), key=lambda i: abs(xs[i] - index))
            if abs(xs[nearest] - index) > 1.5:
                continue
            label = ""
            if 0 <= index < len(self._axis.labels):
                label = self._axis.labels[index]
            bits.append(f"{name} {ys[nearest]:.4g}" + (f"  {label}" if label else ""))
        if bits:
            self._cursor.setText("   ".join(bits))
