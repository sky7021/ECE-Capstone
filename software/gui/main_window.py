"""Master GUI window, Host FSM state manager (S0-S5), and fault modal handler."""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMainWindow, QMessageBox, QVBoxLayout, QWidget

from software.fsm.host_controller import HostConfig, HostController
from software.fsm.host_fsm import HostState, HostStateMachine
from software.gui.views.diagnostic_view import DiagnosticView
from software.gui.views.time_series_view import TimeSeriesView

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

        ctrl = controller or HostController(config or HostConfig(mock=True))
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

    @property
    def fsm(self) -> HostStateMachine:
        return self.controller.fsm

    @property
    def current_state(self) -> HostState:
        return self.controller.state

    @property
    def history_state(self) -> Optional[HostState]:
        return self.controller.fsm.history_state

    def _sync_from_transition(self, state: HostState) -> None:
        self.diagnostic_view.status_indicator.set_state(state)
        if state is HostState.S2_TARE_AND_PRIME:
            self.diagnostic_view.status_indicator.set_s2_locks(
                self.fsm.in_s2_a2, self.fsm.in_s2_b2
            )

    # ============================================== Spec §4.A.2 FSM handlers
    @pyqtSlot(result=bool)
    def on_start_run_clicked(self) -> bool:
        """Transition ``S1 -> S2: TARE_AND_PRIME`` via controller."""
        return self.controller.start_run()

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
