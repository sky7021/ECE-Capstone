"""60 FPS pyqtgraph waveforms (f_qcm, delta_f, dm/dt, Temp).

Guardrail 4: Uses ``pyqtgraph`` exclusively for real-time time-series plotting
and never imports ``matplotlib``. If ``pyqtgraph`` is not installed in a
headless environment, falls back to lightweight ``QWidget`` placeholders while
retaining full time-series buffers.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Optional

from PyQt6.QtWidgets import QVBoxLayout, QWidget

try:
    import pyqtgraph as pg  # type: ignore[import-untyped]
except ModuleNotFoundError:  # pragma: no cover - headless fallback when pyqtgraph is absent
    pg = None


class TimeSeriesView(QWidget):
    """Manages 4 synchronized subplots (f_qcm, delta_f_particle, dm/dt, T_qcm vs T_sht)."""

    def __init__(self, max_points: int = 600, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.max_points = max_points
        self.t_buf: Deque[float] = deque(maxlen=max_points)
        self.f_qcm_buf: Deque[float] = deque(maxlen=max_points)
        self.delta_f_buf: Deque[float] = deque(maxlen=max_points)
        self.dm_dt_buf: Deque[float] = deque(maxlen=max_points)
        self.t_qcm_buf: Deque[float] = deque(maxlen=max_points)
        self.t_sht_buf: Deque[float] = deque(maxlen=max_points)

        layout = QVBoxLayout(self)
        self.plots: list[Any] = []
        self._curves: dict[str, Any] = {}

        titles = (
            "Raw QCM Frequency f_qcm (Hz)",
            "Tared Frequency Shift delta_f_particle (Hz)",
            "Sauerbrey Mass Deposition Rate dm/dt (ng/s)",
            "Crystal Temp T_qcm vs Chamber Temp T_sht (C)",
        )
        if pg is not None:
            for idx, title in enumerate(titles):
                pw = pg.PlotWidget(title=title)
                if idx > 0:
                    pw.setXLink(self.plots[0])
                self.plots.append(pw)
                layout.addWidget(pw)
            self._curves["f_qcm"] = self.plots[0].plot(pen="y")
            self._curves["delta_f"] = self.plots[1].plot(pen="c")
            self._curves["dm_dt"] = self.plots[2].plot(pen="m")
            self._curves["t_qcm"] = self.plots[3].plot(pen="r", name="T_qcm")
            self._curves["t_sht"] = self.plots[3].plot(pen="g", name="T_sht")
        else:
            for _ in titles:
                placeholder = QWidget(self)
                self.plots.append(placeholder)
                layout.addWidget(placeholder)

    def clear_series(self) -> None:
        self.t_buf.clear()
        self.f_qcm_buf.clear()
        self.delta_f_buf.clear()
        self.dm_dt_buf.clear()
        self.t_qcm_buf.clear()
        self.t_sht_buf.clear()

    def append_fused_row(self, row: dict) -> None:
        t = float(row["pc_timestamp_s"])
        self.t_buf.append(t)
        self.f_qcm_buf.append(float(row["qcm_freq_hz"]))
        self.delta_f_buf.append(float(row["delta_f_hz"]))
        self.dm_dt_buf.append(float(row["dm_dt_ng_s"]))
        self.t_qcm_buf.append(float(row["qcm_temp_c"]))
        self.t_sht_buf.append(float(row["sht_temp_c"]))

        if self._curves:
            xs = list(self.t_buf)
            self._curves["f_qcm"].setData(xs, list(self.f_qcm_buf))
            self._curves["delta_f"].setData(xs, list(self.delta_f_buf))
            self._curves["dm_dt"].setData(xs, list(self.dm_dt_buf))
            self._curves["t_qcm"].setData(xs, list(self.t_qcm_buf))
            self._curves["t_sht"].setData(xs, list(self.t_sht_buf))

