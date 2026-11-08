"""Master GUI window, Host FSM state manager (S0-S5), and fault modal handler."""

from __future__ import annotations

import time
from typing import Callable, Optional

from PyQt6.QtCore import pyqtSlot
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMainWindow, QMessageBox, QVBoxLayout, QWidget

from software.fsm.host_fsm import HostState, HostStateMachine, Transition
from software.gui.views.diagnostic_view import DiagnosticView
from software.gui.views.time_series_view import TimeSeriesView
from software.pipeline.csv_logger import CSVLogger
from software.pipeline.zoh_fusion import ZOHFusionEngine
from software.readers.aux_reader import AuxReader
from software.readers.qcm_reader import QCMReader

__all__ = ["HostState", "MainWindow"]


class _WindowActions:
    """Adapts a standalone :class:`MainWindow` to the :class:`HostActions` protocol."""

    def __init__(self, win: "MainWindow") -> None:
        self._w = win

    def ports_available(self) -> bool:
        return True

    def ports_healthy(self) -> bool:
        q, a = self._w.qcm_reader, self._w.aux_reader
        if q is None and a is None:
            return True
        return q is not None and a is not None and q.is_healthy and a.is_healthy

    def open_hardware(self) -> bool:
        return True

    def begin_session(self) -> None:
        w = self._w
        armed_at = time.monotonic()
        if w.qcm_reader is not None:
            w.qcm_reader.purge_input_buffer()
        if w.aux_reader is not None:
            w.aux_reader.purge_input_buffer()
        w.csv_logger.open_session_csv()
        w.zoh_fusion.reset_for_tare(armed_at)

    def begin_streaming(self, f0_hz: int, t0_c: float) -> None:
        pass

    def transmit_pwm(self, duty: int) -> None:
        if self._w.aux_reader is not None:
            self._w.aux_reader.send_pwm_command(duty)

    def park_hardware(self, reason: str) -> None:
        self._release()

    def show_fault(self, reason: str) -> None:
        self._w._present_fault_modal(reason)

    def clear_fault(self) -> None:
        self._w._reset_ui_on_disconnect()

    def terminate(self) -> None:
        self._release()

    def _release(self) -> None:
        w = self._w
        w.zoh_fusion.invalidate()
        try:
            w.csv_logger.flush_and_close()
        finally:
            if w.qcm_reader is not None:
                w.qcm_reader.stop_and_close()
            if w.aux_reader is not None:
                w.aux_reader.stop_and_close()


class MainWindow(QMainWindow):
    """Master GUI window & top-level Host FSM state manager (spec §4.A.2)."""

    def __init__(
        self,
        qcm_reader: Optional[QCMReader] = None,
        aux_reader: Optional[AuxReader] = None,
        zoh_fusion: Optional[ZOHFusionEngine] = None,
        csv_logger: Optional[CSVLogger] = None,
        modal_enabled: bool = False,
        modal_callback: Optional[Callable[[str], bool]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("ECE 490 QCM Resonant Mass Sensing Platform")

        self.qcm_reader = qcm_reader
        self.aux_reader = aux_reader
        self.zoh_fusion = zoh_fusion or ZOHFusionEngine(parent=self)
        self.csv_logger = csv_logger or CSVLogger()
        self.modal_enabled = modal_enabled
        self.modal_callback = modal_callback
        self.last_fault_message: Optional[str] = None

        self.fsm = HostStateMachine(actions=_WindowActions(self))
        self.current_state: HostState = self.fsm.state
        self.history_state: Optional[HostState] = self.fsm.history_state
        self.fsm.add_listener(self._sync_from_transition)

        self.zoh_fusion.qcm_tare_ready.connect(lambda _f, _t: self.fsm.qcm_tare_ready())
        self.zoh_fusion.aux_zoh_primed.connect(lambda _d: self.fsm.aux_zoh_primed())
        self.zoh_fusion.s2_join_complete.connect(self.on_join_completed)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        self.diagnostic_view = DiagnosticView(central)
        self.time_series_view = TimeSeriesView(parent=central)
        layout.addWidget(self.diagnostic_view)
        layout.addWidget(self.time_series_view)
        self.setCentralWidget(central)

        self.diagnostic_view.start_run_requested.connect(self.on_start_run_clicked)
        self.diagnostic_view.fan_pwm_changed.connect(self.on_fan_slider_changed)

    def _sync_from_transition(self, t: Transition) -> None:
        self.current_state = self.fsm.state
        self.history_state = self.fsm.history_state
        self.diagnostic_view.status_indicator.set_state(self.current_state)
        if self.current_state is HostState.S2_TARE_AND_PRIME:
            self.diagnostic_view.status_indicator.set_s2_locks(
                self.fsm.in_s2_a2, self.fsm.in_s2_b2
            )

    # ============================================== Spec §4.A.2 FSM handlers
    @pyqtSlot()
    def on_ports_connected(self) -> None:
        """Transition ``S0 -> S1: IDLE / STANDBY``."""
        self.fsm.boot()

    @pyqtSlot()
    def on_start_run_clicked(self) -> None:
        """Transition ``S1 -> S2: TARE_AND_PRIME``; purge buffers, open CSV, arm tare."""
        self.fsm.start_run()

    @pyqtSlot(int, float)
    def on_join_completed(self, f0_hz: int, t0_c: float) -> None:
        """Transition ``S2 -> S3: ACTIVE_STREAMING & ZOH_FUSION``."""
        if self.fsm.state is HostState.S2_TARE_AND_PRIME:
            if not self.fsm.in_s2_a2:
                self.fsm.qcm_tare_ready()
            if not self.fsm.in_s2_b2:
                self.fsm.aux_zoh_primed()
            self.fsm.join_completed(f0_hz, t0_c)

    @pyqtSlot(int)
    def on_fan_slider_changed(self, duty: int) -> None:
        """Validate ``0 <= duty <= 100`` in ``HS_CONNECTED``, enter ``S5`` and return via ``H``."""
        self.fsm.fan_pwm_changed(duty)

    @pyqtSlot(str)
    def on_fault_triggered(self, error_msg: str) -> None:
        """Group Preemptive Exit ``HS_CONNECTED -> S4: FAULT``."""
        self.fsm.fault(error_msg)

    @pyqtSlot()
    def on_fault_modal_confirmed(self) -> None:
        """Transition ``S4: FAULT -> S0: DISCONNECTED`` upon modal confirmation."""
        self.fsm.fault_confirmed()

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
        self.fsm.shutdown()
        super().closeEvent(event)

