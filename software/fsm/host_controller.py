"""Qt controller wiring the Host FSM to readers, ZOH fusion engine and CSV logger.

:class:`HostController` is the single object a GUI (``main_window.py``) or a
headless runner talks to. It owns:

* :class:`~software.fsm.host_fsm.HostStateMachine` - control plane (S0-S5)
* :class:`~software.readers.qcm_reader.QCMReader` /
  :class:`~software.readers.aux_reader.AuxReader` - QThread serial workers
* :class:`~software.pipeline.zoh_fusion.ZOHFusionEngine` - S2 latch / S3 fusion
* :class:`~software.pipeline.csv_logger.CSVLogger` - session storage

Mapping to the spec's GUI handlers (``main_window.py`` should simply delegate):

============================================  ===============================
Spec handler                                  Controller slot
============================================  ===============================
``ApplicationController.init_workers``        :meth:`connect_ports`
``MainWindow.on_start_run_clicked``           :meth:`start_run`
``MainWindow.on_fan_slider_changed``          :meth:`set_fan_pwm`
``MainWindow.on_fault_triggered``             :meth:`on_fault_triggered`
``MainWindow.on_fault_modal_confirmed``       :meth:`confirm_fault`
``ApplicationController.shutdown``            :meth:`shutdown`
============================================  ===============================

All slots must be called from the GUI (main) thread; reader signals arrive
there through queued connections.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from statemachine.exceptions import TransitionNotAllowed

from software.fsm.host_fsm import HostState, HostStateMachine
from software.pipeline.csv_logger import DEFAULT_LOG_DIR, CSVLogger
from software.pipeline.quality import FLOW_TIMEOUT_S, QCM_TIMEOUT_S, SHT_TIMEOUT_S
from software.pipeline.zoh_fusion import ZOHFusionEngine
from software.readers.aux_reader import AuxReader
from software.readers.qcm_reader import QCMReader
from software.readers.serial_worker import DEFAULT_BAUD

log = logging.getLogger(__name__)


@dataclass
class HostConfig:
    mock: bool = False
    qcm_port: Optional[str] = None
    aux_port: Optional[str] = None
    baud: int = DEFAULT_BAUD
    log_dir: str = DEFAULT_LOG_DIR
    qcm_timeout_s: float = QCM_TIMEOUT_S
    flow_timeout_s: float = FLOW_TIMEOUT_S
    sht_timeout_s: float = SHT_TIMEOUT_S
    mock_period_s: float = 1.0


ReaderFactory = Callable[[HostConfig], "tuple[QCMReader, AuxReader]"]


def default_reader_factory(config: HostConfig) -> tuple[QCMReader, AuxReader]:
    if config.mock:
        from software.tests.serial_mock import MockAuxReader, MockQCMReader

        return (
            MockQCMReader(period_s=config.mock_period_s, timeout_s=config.qcm_timeout_s),
            MockAuxReader(
                period_s=config.mock_period_s,
                flow_timeout_s=config.flow_timeout_s,
                sht_timeout_s=config.sht_timeout_s,
            ),
        )
    return (
        QCMReader(config.qcm_port, config.baud, timeout_s=config.qcm_timeout_s),
        AuxReader(
            config.aux_port,
            config.baud,
            flow_timeout_s=config.flow_timeout_s,
            sht_timeout_s=config.sht_timeout_s,
        ),
    )


class HostController(QObject):
    # --- control-plane notifications -------------------------------------
    state_changed = pyqtSignal(object)  # Transition (use .target for the new HostState)
    s2_progress = pyqtSignal(bool, bool)  # (in_s2_a2, in_s2_b2) for lock badges
    connection_failed = pyqtSignal(str)
    session_started = pyqtSignal(str)  # CSV path
    streaming_started = pyqtSignal(int, float)  # frozen f0 (Hz), T0 (C)
    fault_raised = pyqtSignal(str)  # GUI: show modal, then call confirm_fault()
    fault_cleared = pyqtSignal()
    # --- data-plane passthrough ------------------------------------------
    qcm_packet = pyqtSignal(float, int, float)  # raw QCM (any connected state)
    aux_packet = pyqtSignal(float, dict)  # raw AUX (any connected state)
    fused_row_ready = pyqtSignal(dict)  # S3 fused rows (plots)
    pwm_command_sent = pyqtSignal(int)

    def __init__(
        self,
        config: Optional[HostConfig] = None,
        reader_factory: Optional[ReaderFactory] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or HostConfig()
        self._reader_factory = reader_factory or default_reader_factory
        self.qcm_reader: Optional[QCMReader] = None
        self.aux_reader: Optional[AuxReader] = None
        self.last_error: Optional[str] = None

        self.engine = ZOHFusionEngine(
            flow_timeout_s=self.config.flow_timeout_s,
            sht_timeout_s=self.config.sht_timeout_s,
            parent=self,
        )
        self.csv_logger = CSVLogger(self.config.log_dir)
        self.fsm = HostStateMachine(actions=_ControllerActions(self))
        self.fsm.add_listener(self)
        self._bind_engine()

    # ================================================================ queries
    @property
    def state(self) -> HostState:
        return self.fsm.state

    # ========================================================== public slots
    @pyqtSlot(result=bool)
    def connect_ports(self) -> bool:
        """``S0 -> S1``: guard ports, open them and spawn the reader QThreads."""
        self.last_error = None
        try:
            self.fsm.boot()
            return True
        except TransitionNotAllowed as exc:
            msg = self.last_error or str(exc)
            self.connection_failed.emit(msg)
            return False

    @pyqtSlot(result=bool)
    def start_run(self) -> bool:
        """``S1 -> S2``: purge buffers, open session CSV, arm S2 priming."""
        try:
            self.fsm.start_run()
            return True
        except TransitionNotAllowed:
            return False

    @pyqtSlot(int, result=bool)
    def set_fan_pwm(self, duty: int) -> bool:
        """``HS_CONNECTED -> S5 -> H``: transmit ``SET_PWM:<duty>``."""
        try:
            self.fsm.fan_pwm_changed(duty)
            return True
        except TransitionNotAllowed:
            return False

    @pyqtSlot(str)
    def on_fault_triggered(self, error_msg: str) -> None:
        """``HS_CONNECTED -> S4`` group preemptive exit."""
        try:
            self.fsm.fault(error_msg)
        except TransitionNotAllowed:
            pass

    @pyqtSlot(result=bool)
    def confirm_fault(self) -> bool:
        """``S4 -> S0`` after the user acknowledges the fault modal."""
        try:
            self.fsm.fault_confirmed()
            return True
        except TransitionNotAllowed:
            return False

    @pyqtSlot()
    def shutdown(self) -> None:
        """Direct termination (app exit / SIGINT / SIGTERM)."""
        try:
            self.fsm.shutdown()
        except TransitionNotAllowed:
            pass

    # ======================================================== signal wiring
    def _bind_engine(self) -> None:
        e = self.engine
        e.qcm_tare_ready.connect(self._on_qcm_tare_ready)
        e.aux_zoh_primed.connect(self._on_aux_zoh_primed)
        e.s2_join_complete.connect(self._on_join_complete)
        e.fused_row_ready.connect(self.csv_logger.log_fused_row)
        e.fused_row_ready.connect(self.fused_row_ready)
        e.staleness_fault.connect(self.on_fault_triggered)

    def _bind_workers(self, qcm: QCMReader, aux: AuxReader) -> None:
        qcm.qcm_packet_received.connect(self._on_qcm_packet)
        aux.aux_packet_received.connect(self._on_aux_packet)
        qcm.serial_fault.connect(self._on_worker_fault)
        aux.serial_fault.connect(self._on_worker_fault)

    @staticmethod
    def _unbind_worker(worker: QObject) -> None:
        for sig_name in ("qcm_packet_received", "aux_packet_received", "serial_fault"):
            sig = getattr(worker, sig_name, None)
            if sig is None:
                continue
            try:
                sig.disconnect()
            except TypeError:
                pass  # nothing connected

    def _is_current_worker(self, obj: Optional[QObject]) -> bool:
        # Queued signals from a torn-down worker may still be in the event
        # queue after a fault/reconnect; only the live workers are trusted.
        return obj is not None and (obj is self.qcm_reader or obj is self.aux_reader)

    # ---------------------------------------------------------- worker slots
    @pyqtSlot(float, int, float)
    def _on_qcm_packet(self, ts: float, freq_hz: int, temp_c: float) -> None:
        if not self._is_current_worker(self.sender()):
            return
        self.qcm_packet.emit(ts, freq_hz, temp_c)
        self.engine.ingest_qcm(ts, freq_hz, temp_c)

    @pyqtSlot(float, dict)
    def _on_aux_packet(self, ts: float, aux: dict) -> None:
        if not self._is_current_worker(self.sender()):
            return
        self.aux_packet.emit(ts, aux)
        self.engine.ingest_aux(ts, aux)

    @pyqtSlot(str)
    def _on_worker_fault(self, msg: str) -> None:
        if not self._is_current_worker(self.sender()):
            return
        try:
            self.fsm.fault(msg)
        except TransitionNotAllowed:
            pass

    # ---------------------------------------------------------- engine slots
    @pyqtSlot(int, float)
    def _on_qcm_tare_ready(self, f_hz: int, t_c: float) -> None:
        try:
            self.fsm.qcm_tare_ready()
        except TransitionNotAllowed:
            pass

    @pyqtSlot(dict)
    def _on_aux_zoh_primed(self, aux: dict) -> None:
        try:
            self.fsm.aux_zoh_primed()
        except TransitionNotAllowed:
            pass

    @pyqtSlot(int, float)
    def _on_join_complete(self, f0_hz: int, t0_c: float) -> None:
        try:
            self.fsm.join_completed(f0_hz, t0_c)
        except TransitionNotAllowed as exc:
            # Data plane and control plane disagree - never stream un-tared data.
            self.fsm.fault(f"internal: S2 join rejected ({exc})")

    # -------------------------------------------------------- FSM listener
    def after_transition(self, event: Any, source: Any, target: Any, **kwargs: Any) -> None:
        self.s2_progress.emit(self.fsm.in_s2_a2, self.fsm.in_s2_b2)
        self.state_changed.emit(self.fsm.state)

    # ------------------------------------------------------ action helpers
    def _teardown_workers(self) -> None:
        workers = [w for w in (self.qcm_reader, self.aux_reader) if w is not None]
        self.qcm_reader = None
        self.aux_reader = None
        for w in workers:
            self._unbind_worker(w)
            w.stop_and_close()
            w.deleteLater()


class _ControllerActions:
    """:class:`~software.fsm.host_fsm.HostActions` implementation backed by the controller."""

    def __init__(self, c: HostController) -> None:
        self._c = c

    # --- guards -----------------------------------------------------------
    def ports_available(self) -> bool:
        cfg = self._c.config
        if cfg.mock:
            return True
        if not (cfg.qcm_port and cfg.aux_port):
            self._c.last_error = "both --qcm-port and --aux-port are required (or use --mock)"
            return False
        return True

    def ports_healthy(self) -> bool:
        q, a = self._c.qcm_reader, self._c.aux_reader
        return q is not None and a is not None and q.is_healthy and a.is_healthy

    # --- actions ----------------------------------------------------------
    def open_hardware(self) -> bool:
        c = self._c
        c._teardown_workers()  # defensive: never leak a previous session's threads
        qcm, aux = c._reader_factory(c.config)
        if not qcm.open_port() or not aux.open_port():
            c.last_error = qcm.last_error or aux.last_error or "failed to open serial ports"
            for w in (qcm, aux):
                w.stop_and_close()
                w.deleteLater()
            return False
        c.qcm_reader, c.aux_reader = qcm, aux
        c._bind_workers(qcm, aux)
        qcm.start()
        aux.start()
        return True

    def begin_session(self) -> None:
        c = self._c
        assert c.qcm_reader is not None and c.aux_reader is not None
        armed_at = time.monotonic()
        c.qcm_reader.purge_input_buffer()  # guardrail 5: only on S1 -> S2
        c.aux_reader.purge_input_buffer()
        path = c.csv_logger.open_session_csv()
        c.engine.reset_for_tare(armed_at)
        c.session_started.emit(str(path))

    def begin_streaming(self, f0_hz: int, t0_c: float) -> None:
        # The engine already froze the baseline and entered STREAMING; CSV
        # logging is driven by engine.fused_row_ready.
        self._c.streaming_started.emit(f0_hz, t0_c)

    def transmit_pwm(self, duty: int) -> None:
        c = self._c
        if c.aux_reader is None:
            raise RuntimeError("AUX reader not running")
        c.aux_reader.send_pwm_command(duty)
        c.pwm_command_sent.emit(duty)

    def park_hardware(self, reason: str) -> None:
        self._release_all()

    def show_fault(self, reason: str) -> None:
        self._c.fault_raised.emit(reason)

    def clear_fault(self) -> None:
        self._c.fault_cleared.emit()

    def terminate(self) -> None:
        self._release_all()

    def _release_all(self) -> None:
        c = self._c
        c.engine.invalidate()  # invalidate ZOH cache, halt derivative math
        try:
            c.csv_logger.flush_and_close()
        finally:
            c._teardown_workers()  # stop QThreads, close ports

