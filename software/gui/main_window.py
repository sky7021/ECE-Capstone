"""Master GUI window, Host FSM state manager (S0-S5), and fault modal handler."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMainWindow, QMessageBox, QVBoxLayout, QWidget

from software.fsm.host_controller import HostConfig, HostController
from software.fsm.host_fsm import HostState, HostStateMachine, Transition
from software.gui.views.diagnostic_view import DiagnosticView
from software.gui.views.time_series_view import TimeSeriesView
from software.pipeline.csv_logger import CSVLogger
from software.pipeline.zoh_fusion import ZOHFusionEngine
from software.readers.aux_reader import AuxReader
from software.readers.qcm_reader import QCMReader

__all__ = ["HostState", "MainWindow"]


class MainWindow(QMainWindow):
    """Master GUI window & top-level Host FSM state manager (spec §4.A.2)."""

    def __init__(
        self,
        controller: Optional[HostController] = None,
        config: Optional[HostConfig] = None,
        modal_enabled: bool = False,
        modal_callback: Optional[Callable[[str], bool]] = None,
        parent: Optional[QWidget] = None,
        *,
        qcm_reader: Optional[QCMReader] = None,
        aux_reader: Optional[AuxReader] = None,
        zoh_fusion: Optional[ZOHFusionEngine] = None,
        csv_logger: Optional[CSVLogger] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("ECE 490 QCM Resonant Mass Sensing Platform")

        self.modal_enabled = modal_enabled
        self.modal_callback = modal_callback
        self.last_fault_message: Optional[str] = None
        self._controller: Optional[HostController] = None

        central = QWidget(self)
        layout = QVBoxLayout(central)
        self.diagnostic_view = DiagnosticView(central)
        self.time_series_view = TimeSeriesView(parent=central)
        layout.addWidget(self.diagnostic_view)
        layout.addWidget(self.time_series_view)
        self.setCentralWidget(central)

        self.diagnostic_view.start_run_requested.connect(self.on_start_run_clicked)
        self.diagnostic_view.fan_pwm_changed.connect(self.on_fan_slider_changed)

        if controller is not None and not isinstance(controller, HostController):
            # Positional argument passed from legacy constructor
            qcm_reader = controller
            controller = None

        ctrl = controller or HostController(config or HostConfig(mock=True))
        if qcm_reader is not None:
            ctrl.qcm_reader = qcm_reader
        if aux_reader is not None:
            ctrl.aux_reader = aux_reader
        if zoh_fusion is not None:
            ctrl.engine = zoh_fusion
        if csv_logger is not None:
            ctrl.csv_logger = csv_logger

        self.set_controller(ctrl)

    # ======================================================= controller wiring
    @property
    def controller(self) -> HostController:
        assert self._controller is not None
        return self._controller

    def set_controller(self, controller: HostController) -> None:
        if self._controller is not None:
            try:
                self._controller.state_changed.disconnect(self._sync_from_transition)
                self._controller.s2_progress.disconnect(self.diagnostic_view.status_indicator.set_s2_locks)
                self._controller.fused_row_ready.disconnect(self.update_plots)
                self._controller.fault_raised.disconnect(self._present_fault_modal)
                self._controller.fault_cleared.disconnect(self._reset_ui_on_disconnect)
            except (TypeError, RuntimeError):
                pass

        self._controller = controller
        self._controller.state_changed.connect(self._sync_from_transition)
        self._controller.s2_progress.connect(self.diagnostic_view.status_indicator.set_s2_locks)
        self._controller.fused_row_ready.connect(self.update_plots)
        self._controller.fault_raised.connect(self._present_fault_modal)
        self._controller.fault_cleared.connect(self._reset_ui_on_disconnect)

        self.diagnostic_view.status_indicator.set_state(self.current_state)

    # Backward-compatible properties mapping directly to the controller:
    @property
    def fsm(self) -> HostStateMachine:
        return self.controller.fsm

    @property
    def current_state(self) -> HostState:
        return self.controller.state

    @property
    def history_state(self) -> Optional[HostState]:
        return self.controller.fsm.history_state

    @property
    def zoh_fusion(self) -> ZOHFusionEngine:
        return self.controller.engine

    @zoh_fusion.setter
    def zoh_fusion(self, engine: ZOHFusionEngine) -> None:
        self.controller.engine = engine

    @property
    def csv_logger(self) -> CSVLogger:
        return self.controller.csv_logger

    @csv_logger.setter
    def csv_logger(self, logger: CSVLogger) -> None:
        self.controller.csv_logger = logger

    @property
    def qcm_reader(self) -> Optional[QCMReader]:
        return self.controller.qcm_reader

    @qcm_reader.setter
    def qcm_reader(self, reader: Optional[QCMReader]) -> None:
        self.controller.qcm_reader = reader

    @property
    def aux_reader(self) -> Optional[AuxReader]:
        return self.controller.aux_reader

    @aux_reader.setter
    def aux_reader(self, reader: Optional[AuxReader]) -> None:
        self.controller.aux_reader = reader

    def _sync_from_transition(self, t: Transition) -> None:
        self.diagnostic_view.status_indicator.set_state(t.target)
        if t.target is HostState.S2_TARE_AND_PRIME:
            self.diagnostic_view.status_indicator.set_s2_locks(
                self.fsm.in_s2_a2, self.fsm.in_s2_b2
            )

    # ============================================== Spec §4.A.2 FSM handlers
    @pyqtSlot(result=bool)
    def on_ports_connected(self) -> bool:
        """Transition ``S0 -> S1: IDLE / STANDBY`` via controller."""
        return self.controller.connect_ports()

    @pyqtSlot(result=bool)
    def on_start_run_clicked(self) -> bool:
        """Transition ``S1 -> S2: TARE_AND_PRIME`` via controller."""
        return self.controller.start_run()

    @pyqtSlot(int, float, result=bool)
    def on_join_completed(self, f0_hz: int, t0_c: float) -> bool:
        """Transition ``S2 -> S3: ACTIVE_STREAMING & ZOH_FUSION``."""
        return self.fsm.join_completed(f0_hz, t0_c)

    @pyqtSlot(int, result=bool)
    def on_fan_slider_changed(self, duty: int) -> bool:
        """Validate ``0 <= duty <= 100`` in ``HS_CONNECTED``, enter ``S5`` and return via ``H``."""
        return self.controller.set_fan_pwm(duty)

    @pyqtSlot(str)
    def on_fault_triggered(self, error_msg: str) -> None:
        """Group Preemptive Exit ``HS_CONNECTED -> S4: FAULT``."""
        self.controller.on_fault_triggered(error_msg)

    @pyqtSlot(result=bool)
    def on_fault_modal_confirmed(self) -> bool:
        """Transition ``S4: FAULT -> S0: DISCONNECTED`` upon modal confirmation."""
        return self.controller.confirm_fault()

    @pyqtSlot(dict)
    def update_plots(self, fused_row: dict) -> None:
        """Update 60 FPS waveforms and diagnostic digital readouts."""
        self.time_series_view.append_fused_row(fused_row)
        self.diagnostic_view.update_readouts(fused_row)

    # ============================================================== helpers
    def _present_fault_modal(self, error_msg: str) -> None:
        self.last_fault_message = error_msg
        if self.modal_callback is not None:
            if self.modal_callback(error_msg):
                self.on_fault_modal_confirmed()
            return
        if self.modal_enabled:
            QMessageBox.critical(self, "Hardware / Stream Fault (S4)", error_msg)
            self.on_fault_modal_confirmed()

    def _reset_ui_on_disconnect(self) -> None:
        self.last_fault_message = None
        self.time_series_view.clear_series()
        self.diagnostic_view.status_indicator.set_state(HostState.S0_DISCONNECTED)

    def closeEvent(self, event: Optional[QCloseEvent]) -> None:  # noqa: N802
        self.controller.shutdown()
        super().closeEvent(event)
