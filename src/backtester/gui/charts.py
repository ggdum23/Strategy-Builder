"""Equity and drawdown charts.

PyQtGraph draws the series it is given. It does not read a run directory and
it does not calculate a performance statistic. The drawdown series is passed
in already labeled as a picture of the equity column.

Ranges are taken from the finite min and max of every series on that plot,
with padding, and then locked. ``setClipToView`` and downsampling are off:
both drop points the autorange then fails to see, which clips a buy-and-hold
line or a drawdown trough that sits outside the first guess.
"""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .format import format_number

pg.setConfigOptions(antialias=True, useOpenGL=False)


class _DateAxis(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.labels: list[str] = []
        self.enableAutoSIPrefix(False)

    def tickValues(self, minVal, maxVal, size):
        """One level of integer session indexes. A tick at 1.5 is not a session."""
        lo, hi = sorted((float(minVal), float(maxVal)))
        if not math.isfinite(lo) or not math.isfinite(hi):
            return []
        if hi <= lo:
            return [(1, [float(int(round(lo)))])]
        count = max(1, int(float(size) / 72) if size else 1)
        step = max(1, int(math.ceil((hi - lo) / count)))
        step = _nice_int_step(step)
        start = math.ceil(lo - 1e-9)
        end = math.floor(hi + 1e-9)
        values = []
        index = int(start)
        while index <= end:
            values.append(float(index))
            index += step
        return [(step, values)]

    def tickStrings(self, values, scale, spacing):
        out = []
        last = len(self.labels) - 1
        previous = None
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError):
                out.append("")
                continue
            if abs(number - round(number)) > 1e-6:
                out.append("")
                continue
            index = int(round(number))
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

    def tickValues(self, minVal, maxVal, size):
        """Several dollar ticks when a log span sits inside one power of ten."""
        if not self.logMode or self.percent:
            return super().tickValues(minVal, maxVal, size)
        lo, hi = sorted((float(minVal), float(maxVal)))
        if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
            return []
        low = 10 ** lo
        high = 10 ** hi
        step, dollars = _dollar_ticks(low, high)
        if not dollars:
            return [(None, [(lo + hi) / 2])]
        return [(step, [math.log10(value) for value in dollars])]

    def tickStrings(self, values, scale, spacing):
        numbers = []
        for value in values:
            try:
                number = float(value) * float(scale)
            except (TypeError, ValueError):
                numbers.append(float("nan"))
                continue
            if self.logMode:
                if number > 300 or number < -300:
                    numbers.append(float("nan"))
                else:
                    numbers.append(10 ** number)
            else:
                numbers.append(number)
        if self.percent:
            return ["" if not math.isfinite(number) else format_number("fraction", number) for number in numbers]
        finite = [number for number in numbers if math.isfinite(number)]
        if self.logMode and len(finite) >= 2:
            step = abs(finite[-1] - finite[0]) / (len(finite) - 1) or 1.0
        elif spacing is None:
            step = 1.0
        else:
            step = abs(float(spacing) * float(scale)) or 1.0
        use_millions = any(abs(number) >= 1_000_000 for number in numbers)
        out = []
        for number in numbers:
            if not math.isfinite(number):
                out.append("")
                continue
            if use_millions:
                digits = 2 if step >= 10_000 else 3
                out.append(f"{number / 1_000_000:.{digits}f}M")
            elif abs(number) >= 10_000 or step >= 1_000:
                digits = 0 if step >= 1_000 else 1
                out.append(f"{number / 1_000:.{digits}f}k")
            else:
                out.append(f"{number:,.0f}")
        return out


def _nice_int_step(step: int) -> int:
    if step <= 1:
        return 1
    magnitude = 10 ** int(math.floor(math.log10(step)))
    for factor in (1, 2, 5, 10):
        if factor * magnitude >= step:
            return factor * magnitude
    return step


def _nice_dollar_step(span: float, count: int) -> float:
    raw = span / max(count, 1)
    if not math.isfinite(raw) or raw <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if factor * magnitude >= raw - 1e-9:
            return float(factor * magnitude)
    return float(10 * magnitude)


def _dollar_ticks(low: float, high: float) -> tuple[float, list[float]]:
    """1-2-5 steps across a dollar span. Tight log ranges still get several."""
    span = high - low
    chosen_step = 1.0
    chosen: list[float] = []
    for count in (6, 8, 10, 14):
        step = _nice_dollar_step(span, count)
        start = math.floor(low / step) * step
        ticks = []
        cursor = start
        for _ in range(48):
            if cursor > high + step * 0.01:
                break
            if low <= cursor <= high and cursor > 0:
                ticks.append(cursor)
            cursor += step
        chosen_step = step
        chosen = ticks
        if len(ticks) >= 4:
            return step, ticks[:8]
    return chosen_step, chosen


def _finite_pairs(xs, ys) -> list[tuple[float, float]]:
    pairs = []
    for x_value, y_value in zip(xs, ys):
        if isinstance(x_value, (int, float)) and isinstance(y_value, (int, float)):
            if math.isfinite(x_value) and math.isfinite(y_value):
                pairs.append((float(x_value), float(y_value)))
    return pairs


def _padded(low: float, high: float, *, log: bool) -> tuple[float, float]:
    """View limits. In log mode these are log10 coordinates, padded in log space."""
    if log:
        low = max(float(low), 1e-300)
        high = max(float(high), low)
        log_low = math.log10(low)
        log_high = math.log10(high)
        span = log_high - log_low
        pad = span * 0.08 if span else 0.05
        return log_low - pad, log_high + pad
    if low == high:
        delta = abs(low) * 0.08 or 1.0
        low, high = low - delta, high + delta
    else:
        span = high - low
        low, high = low - span * 0.08, high + span * 0.08
    if high <= low:
        high = low + 1.0
    return low, high


class ChartPanel(QWidget):
    def __init__(self):
        super().__init__()
        self._theme = None
        self._study = None
        self._equity_items = []
        self._drawdown_items = []
        self._x_axis = _DateAxis(orientation="bottom")
        self._equity_axis = _NumberAxis(orientation="left")
        self._dd_axis = _NumberAxis(orientation="left")
        self._dd_axis.percent = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        tools = QHBoxLayout()
        self.caption = QLabel("")
        self.caption.setObjectName("muted")
        self.caption.setWordWrap(True)
        self.log_box = QCheckBox("Log scale")
        self.log_box.setObjectName("logScale")
        self.log_box.toggled.connect(self._log_toggled)
        self.exposure_box = QCheckBox("Exposure")
        self.exposure_box.setObjectName("exposureToggle")
        self.exposure_box.setEnabled(False)
        self.exposure_box.toggled.connect(self._paint)
        reset = QPushButton("Reset view")
        reset.clicked.connect(self._paint)
        tools.addWidget(self.caption, 1)
        tools.addWidget(self.log_box)
        tools.addWidget(self.exposure_box)
        tools.addWidget(reset)
        self.canvas = pg.GraphicsLayoutWidget()
        self.canvas.setMinimumHeight(420)
        self.equity_plot = self.canvas.addPlot(
            row=0,
            col=0,
            axisItems={"bottom": _DateAxis(orientation="bottom"), "left": self._equity_axis},
        )
        self.canvas.nextRow()
        self.dd_plot = self.canvas.addPlot(
            row=1,
            col=0,
            axisItems={"bottom": self._x_axis, "left": self._dd_axis},
        )
        self.canvas.ci.layout.setRowStretchFactor(0, 3)
        self.canvas.ci.layout.setRowStretchFactor(1, 1)
        self.dd_plot.setXLink(self.equity_plot)
        for plot in (self.equity_plot, self.dd_plot):
            plot.showGrid(x=True, y=True, alpha=0.28)
            plot.setMenuEnabled(False)
            plot.setClipToView(False)
            plot.disableAutoRange()
            plot.getViewBox().setDefaultPadding(0)
            plot.setClipToView(False)
        self.equity_plot.setLabel("left", "USD")
        self.dd_plot.setLabel("left", "drawdown %")
        self.dd_plot.setLabel("bottom", "session")
        self.equity_plot.getAxis("left").setWidth(72)
        self.dd_plot.getAxis("left").setWidth(72)
        self._legend = self.equity_plot.addLegend(offset=(8, 8))
        self.dd_caption = QLabel("")
        self.dd_caption.setObjectName("muted")
        self.dd_caption.setWordWrap(True)
        self.readout = QLabel("Drag to pan. Wheel to zoom. The readout is the session and each series.")
        self.readout.setObjectName("crosshair")
        self.readout.setWordWrap(True)
        layout.addLayout(tools)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.dd_caption)
        layout.addWidget(self.readout)
        self.setMinimumHeight(480)
        self._vline_equity = pg.InfiniteLine(angle=90, movable=False)
        self._vline_drawdown = pg.InfiniteLine(angle=90, movable=False)
        self.canvas.scene().sigMouseMoved.connect(self._moved)

    def apply_theme(self, theme) -> None:
        self._theme = theme
        self.canvas.setBackground(theme.chart_bg)
        for plot in (self.equity_plot, self.dd_plot):
            for name in ("left", "bottom"):
                axis = plot.getAxis(name)
                axis.setPen(pg.mkPen(theme.chart_grid))
                axis.setTextPen(pg.mkPen(theme.chart_fg))
        pen = pg.mkPen(theme.chart_fg)
        self._vline_equity.setPen(pen)
        self._vline_drawdown.setPen(pen)
        if self._legend is not None:
            fill = QColor(theme.chart_bg)
            fill.setAlpha(232)
            self._legend.setBrush(pg.mkBrush(fill))
            self._legend.setPen(pg.mkPen(theme.border))
            if hasattr(self._legend, "setLabelTextColor"):
                self._legend.setLabelTextColor(theme.chart_fg)

    def set_lines(self, lines, ylabel: str, sessions: list[str] | None, note: str, *, percent: bool = False) -> None:
        """Older single-plot call. Draws on the equity panel and leaves drawdown empty."""
        equity = []
        drawdown = []
        for name, xs, ys, color in lines:
            target = drawdown if percent else equity
            target.append((name, xs, ys, color))
        self.set_study(
            equity=equity,
            drawdown=drawdown,
            sessions=list(sessions or []),
            equity_note=note,
            drawdown_note=note if percent else "",
            exposure=None,
            equity_percent=percent,
        )
        if ylabel:
            self.equity_plot.setLabel("left", ylabel)

    def set_study(
        self,
        *,
        equity,
        drawdown,
        sessions,
        equity_note: str,
        drawdown_note: str,
        exposure=None,
        equity_percent: bool = False,
    ) -> None:
        """``equity`` and ``drawdown`` are ``(name, x, y, color)``."""
        self._study = {
            "equity": list(equity or []),
            "drawdown": list(drawdown or []),
            "sessions": list(sessions or []),
            "equity_note": equity_note,
            "drawdown_note": drawdown_note,
            "exposure": exposure,
            "equity_percent": equity_percent,
        }
        self.exposure_box.setEnabled(exposure is not None)
        if exposure is None and self.exposure_box.isChecked():
            self.exposure_box.blockSignals(True)
            self.exposure_box.setChecked(False)
            self.exposure_box.blockSignals(False)
        self._paint()

    def _series_for_top(self):
        study = self._study or {}
        if self.exposure_box.isChecked() and study.get("exposure"):
            name, xs, ys, color = study["exposure"]
            return [(name, xs, ys, color)], "exposure", False
        return list(study.get("equity") or []), "USD", bool(study.get("equity_percent"))

    def _paint(self) -> None:
        study = self._study
        if study is None:
            return
        self.equity_plot.clear()
        self.dd_plot.clear()
        self._legend = self.equity_plot.addLegend(offset=(8, 8))
        if self._theme is not None:
            self.apply_theme(self._theme)
        self._x_axis.labels = list(study["sessions"])
        top_axis = self.equity_plot.getAxis("bottom")
        if isinstance(top_axis, _DateAxis):
            top_axis.labels = list(study["sessions"])
        self.caption.setText(study["equity_note"])
        self.dd_caption.setText(study["drawdown_note"])
        self.readout.setText("Drag to pan. Wheel to zoom. The readout is the session and each series.")
        top, ylabel, percent = self._series_for_top()
        self._equity_axis.percent = percent
        self.equity_plot.setLabel("left", ylabel)
        self._equity_items = []
        self._drawdown_items = []
        log = self.log_box.isChecked() and not self.exposure_box.isChecked()
        if log and not self._can_log(top):
            self.log_box.blockSignals(True)
            self.log_box.setChecked(False)
            self.log_box.blockSignals(False)
            log = False
            self.readout.setText("Log scale stays off: an equity point is not above zero.")
        self.equity_plot.setLogMode(x=False, y=log)
        self.dd_plot.setLogMode(x=False, y=False)
        for name, xs, ys, color in top:
            item = self._plot(self.equity_plot, name, xs, ys, color, legend=True)
            self._equity_items.append((name, item))
        for name, xs, ys, color in study["drawdown"]:
            item = self._plot(self.dd_plot, name, xs, ys, color, legend=False)
            self._drawdown_items.append((name, item))
        self._vline_equity = pg.InfiniteLine(angle=90, movable=False)
        self._vline_drawdown = pg.InfiniteLine(angle=90, movable=False)
        if self._theme is not None:
            pen = pg.mkPen(self._theme.chart_fg)
            self._vline_equity.setPen(pen)
            self._vline_drawdown.setPen(pen)
        self.equity_plot.addItem(self._vline_equity, ignoreBounds=True)
        self.dd_plot.addItem(self._vline_drawdown, ignoreBounds=True)
        self._fit()
        if not top and not study["drawdown"]:
            self.readout.setText("No series in this report.")

    def _plot(self, plot, name, xs, ys, color, *, legend: bool):
        x_values = []
        y_values = []
        for x_item, y_item in zip(xs, ys):
            try:
                x_values.append(float(x_item))
            except (TypeError, ValueError):
                x_values.append(float("nan"))
            try:
                y_values.append(float(y_item))
            except (TypeError, ValueError):
                y_values.append(float("nan"))
        kwargs = {"pen": pg.mkPen(color, width=1.8)}
        if legend:
            kwargs["name"] = name
        item = plot.plot(x_values, y_values, **kwargs)
        item.setClipToView(False)
        item.setDownsampling(auto=False)
        item.opts["clipToView"] = False
        return item

    def _can_log(self, series) -> bool:
        saw = False
        for _name, xs, ys, _color in series:
            for _x_value, y_value in _finite_pairs(xs, ys):
                saw = True
                if y_value <= 0:
                    return False
        return saw

    def _log_toggled(self, _checked: bool) -> None:
        if self._study is None:
            return
        top, _ylabel, _percent = self._series_for_top()
        if self.log_box.isChecked() and not self._can_log(top):
            self.log_box.blockSignals(True)
            self.log_box.setChecked(False)
            self.log_box.blockSignals(False)
            self.readout.setText("Log scale stays off: an equity point is not above zero.")
            return
        self._paint()

    def _fit(self) -> None:
        top, _ylabel, _percent = self._series_for_top()
        drawdown = (self._study or {}).get("drawdown") or []
        log = self.log_box.isChecked() and not self.exposure_box.isChecked()
        self._fit_plot(self.equity_plot, top, log=log)
        self._fit_plot(self.dd_plot, drawdown, log=False)
        # The drawdown x axis is linked. Fit x from every series so a longer
        # benchmark cannot sit outside the linked range.
        xs = []
        for series in (top, drawdown):
            for _name, x_values, y_values, _color in series:
                xs.extend(x for x, _y in _finite_pairs(x_values, y_values))
        if xs:
            low, high = _padded(min(xs), max(xs), log=False)
            self.equity_plot.setXRange(low, high, padding=0)
            self.equity_plot.disableAutoRange()
            self.dd_plot.disableAutoRange()

    def _fit_plot(self, plot, series, *, log: bool) -> None:
        pairs = []
        for _name, xs, ys, _color in series:
            pairs.extend(_finite_pairs(xs, ys))
        plot.disableAutoRange()
        plot.setClipToView(False)
        if not pairs:
            return
        xs = [item[0] for item in pairs]
        ys = [item[1] for item in pairs]
        x0, x1 = _padded(min(xs), max(xs), log=False)
        y0, y1 = _padded(min(ys), max(ys), log=log)
        plot.setXRange(x0, x1, padding=0)
        plot.setYRange(y0, y1, padding=0)
        plot.disableAutoRange()

    def covered(self) -> list[str]:
        """Human-readable misses. Empty means every finite extremum is inside the view."""
        problems = []
        top, _ylabel, _percent = self._series_for_top()
        problems.extend(self._coverage(self.equity_plot, top, "equity"))
        drawdown = (self._study or {}).get("drawdown") or []
        problems.extend(self._coverage(self.dd_plot, drawdown, "drawdown"))
        return problems

    def _coverage(self, plot, series, label: str) -> list[str]:
        """Compare extrema in the ViewBox coordinate system.

        With a log axis that system is log10. Comparing raw dollars to a log
        range reports a false pass while the line sits outside the plot.
        """
        problems = []
        log_y = bool(plot.getViewBox().state.get("logMode", [False, False])[1])
        x_range, y_range = plot.viewRange()

        def y_coord(value: float) -> float | None:
            if not log_y:
                return value
            if value <= 0:
                return None
            return math.log10(value)

        for name, xs, ys, _color in series:
            pairs = _finite_pairs(xs, ys)
            if not pairs:
                continue
            x_values = [item[0] for item in pairs]
            y_values = [item[1] for item in pairs]
            x0, x1 = min(x_values), max(x_values)
            y0, y1 = y_coord(min(y_values)), y_coord(max(y_values))
            outside = x0 < x_range[0] - 1e-6 or x1 > x_range[1] + 1e-6
            outside = outside or y0 is None or y1 is None
            outside = outside or y0 < y_range[0] - 1e-6 or y1 > y_range[1] + 1e-6
            if outside:
                problems.append(
                    f"{label}:{name} data [{x0}, {x1}] × [{y0}, {y1}] "
                    f"view [{x_range[0]}, {x_range[1]}] × [{y_range[0]}, {y_range[1]}]"
                )
        return problems

    def show_readout_at(self, index: int) -> None:
        """Place the crosshair on one session. The screenshot uses this."""
        self._vline_equity.setPos(float(index))
        self._vline_drawdown.setPos(float(index))
        self.readout.setText(self._readout(index))

    def _readout(self, index: int) -> str:
        study = self._study or {}
        labels = study.get("sessions") or []
        bits = []
        if 0 <= index < len(labels) and labels[index]:
            bits.append(str(labels[index]))
        top, ylabel, percent = self._series_for_top()
        if ylabel == "exposure":
            top_kind = "ratio"
        elif percent:
            top_kind = "fraction"
        else:
            top_kind = "money"
        series = [(name, xs, ys, top_kind) for name, xs, ys, _color in top]
        series.extend((name, xs, ys, "fraction") for name, xs, ys, _color in study.get("drawdown") or [])
        for name, xs, ys, kind in series:
            pairs = _finite_pairs(xs, ys)
            if not pairs:
                continue
            nearest = min(pairs, key=lambda item: abs(item[0] - index))
            if abs(nearest[0] - index) > 1.5:
                continue
            bits.append(f"{name} {format_number(kind, nearest[1])}")
        return "   ".join(bits) if bits else "Drag to pan. Wheel to zoom."

    def _moved(self, position) -> None:
        try:
            self._moved_inner(position)
        except Exception:
            return

    def _moved_inner(self, position) -> None:
        if self._study is None:
            return
        plot = None
        if self.equity_plot.sceneBoundingRect().contains(position):
            plot = self.equity_plot
        elif self.dd_plot.sceneBoundingRect().contains(position):
            plot = self.dd_plot
        if plot is None:
            return
        point = plot.vb.mapSceneToView(position)
        index = int(round(point.x()))
        self._vline_equity.setPos(point.x())
        self._vline_drawdown.setPos(point.x())
        text = self._readout(index)
        if text:
            self.readout.setText(text)
