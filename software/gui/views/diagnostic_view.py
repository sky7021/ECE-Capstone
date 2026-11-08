"""COM port health, ZOH indicators, Start Run button and S5 Fan PWM slider (0-100%)."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from software.gui.widgets.status_indicator import StatusIndicator


class DiagnosticView(QWidget):
    """Renders Start Run button, 0-100% Fan PWM slider, and telemetry readouts."""

    start_run_requested = pyqtSignal()
    fan_pwm_changed = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.status_indicator = StatusIndicator(self)
        layout.addWidget(self.status_indicator)

        btn_row = QHBoxLayout()
        self.start_button = QPushButton("Start Run", self)
        self.start_button.clicked.connect(self.start_run_requested.emit)
        btn_row.addWidget(self.start_button)

        self.pwm_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.pwm_slider.setRange(0, 100)
        self.pwm_slider.setValue(0)
        self.pwm_slider.valueChanged.connect(self.fan_pwm_changed.emit)
        btn_row.addWidget(self.pwm_slider)
        layout.addLayout(btn_row)

        readout_row = QHBoxLayout()
        self.flow_label = QLabel("Flow: -- L/min", self)
        self.rh_label = QLabel("RH: -- %", self)
        self.age_label = QLabel("ZOH Age: flow=--s, sht=--s", self)
        for lbl in (self.flow_label, self.rh_label, self.age_label):
            readout_row.addWidget(lbl)
        layout.addLayout(readout_row)

    def update_readouts(self, row: dict) -> None:
        flow = row.get("flow_lpm", 0.0)
        rh = row.get("sht_rh_pct", 0.0)
        flow_age = row.get("zoh_flow_age_s", 0.0)
        sht_age = row.get("zoh_sht_age_s", 0.0)
        self.flow_label.setText(f"Flow: {flow:.3f} L/min")
        self.rh_label.setText(f"RH: {rh:.1f} %")
        self.age_label.setText(f"ZOH Age: flow={flow_age:.2f}s, sht={sht_age:.2f}s")
        self.status_indicator.set_zoh_health(flow_age <= 3.0 and sht_age <= 6.0)

