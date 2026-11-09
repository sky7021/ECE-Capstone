"""Qt controller wiring the Host FSM to readers, ZOH fusion engine and CSV logger.

:class:`HostController` is the single object a GUI (``main_window.py``) or a
headless runner talks to. It owns:

* :class:`~software.fsm.host_fsm.HostStateMachine` - control plane (S0-S5)
* :class:`~software.readers.qcm_reader.QCMReader` /
  :class:`~software.readers.aux_reader.AuxReader` - QThread serial workers
* :class:`~software.pipeline.zoh_fusion.ZOHFusionEngine` - S2 latch / S3 fusion
* :class:`~software.pipeline.csv_logger.CSVLogger` - session storage

Mapping to the spec's GUI handlers (``main_window.py`` simply delegates):

============================================  ===============================
Spec handler                                  Controller slot
============================================  ===============================
``connect_ports``                             :meth:`connect_ports`
``MainWindow.on_start_run_clicked``           :meth:`start_run`
``MainWindow.on_fan_slider_changed``          :meth:`set_fan_pwm`
``MainWindow.on_fault_triggered``             :meth:`on_fault_triggered`
``MainWindow.on_fault_modal_confirmed``       :meth:`confirm_fault`
``shutdown``                                  :meth:`shutdown`
============================================  ===============================

All slots must be called from the GUI (main) thread; reader signals arrive
there through queued connections.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal, pyqtSlot
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
    log_dir: Path | str = DEFAULT_LOG_DIR
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


def configure_signal_handling(app: QCoreApplication, controller: HostController) -> QTimer:
    """Trap ``SIGINT`` and ``SIGTERM`` to invoke ``controller.shutdown()`` and quit ``app``."""

    def _handle_sig(_signum: int, _frame: Any) -> None:
        controller.shutdown()
        app.quit()

    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sig)

    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(200)
    return timer


class HostController(QObject):
    # --- control-plane notifications -------------------------------------
    state_changed = pyqtSignal(object)  # HostState
    s2_progress = pyqtSignal(bool, bool)  # (in_s2_a2, in_s2_b2) for lock badges
    connection_failed = pyqtSignal(str)
    session_started = pyqtSignal(str)  # CSV path
    fault_raised = pyqtSignal(str)  # GUI: show modal, then call confirm_fault()
    fault_cleared = pyqtSignal()
    # --- data-plane passthrough ------------------------------------------
    fused_row_ready = pyqtSignal(dict)  # S3 fused rows (plots)

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
        self._last_emitted_state: HostState = HostState.S0_DISCONNECTED

        self.engine = ZOHFusionEngine(
            flow_timeout_s=self.config.flow_timeout_s,
            sht_timeout_s=self.config.sht_timeout_s,
            parent=self,
        )
        self.csv_logger = CSVLogger(self.config.log_dir)
        self.fsm = HostStateMachine(actions=self)
        self.fsm.add_listener(self)
        self._bind_engine()

    # ================================================================ queries
    @property
    def state(self) -> HostState:
        return self.fsm.state

    # ======================================================== dispatch helper
    def _dispatch(
        self,
        event_fn: Callable[..., Any],
        *args: Any,
        on_error: Optional[Callable[[TransitionNotAllowed], None]] = None,
    ) -> bool:
        try:
            event_fn(*args)
            return True
        except TransitionNotAllowed as exc:
            if on_error is not None:
                on_error(exc)
            return False

    # ========================================================== public slots
    @pyqtSlot(result=bool)
    def connect_ports(self) -> bool:
        """``S0 -> S1``: guard ports, open them and spawn the reader QThreads."""
        self.last_error = None
        return self._dispatch(
            self.fsm.boot,
            on_error=lambda exc: self.connection_failed.emit(self.last_error or str(exc)),
        )

    @pyqtSlot(result=bool)
    def start_run(self) -> bool:
        """``S1 -> S2``: purge buffers, open session CSV, arm S2 priming."""
        return self._dispatch(self.fsm.start_run)

    @pyqtSlot(int, result=bool)
    def set_fan_pwm(self, duty: int) -> bool:
        """``HS_CONNECTED -> S5 -> H``: transmit ``SET_PWM:<duty>``."""
        return self._dispatch(self.fsm.fan_pwm_changed, duty)

    @pyqtSlot(str)
    def on_fault_triggered(self, error_msg: str) -> None:
        """``HS_CONNECTED -> S4`` group preemptive exit."""
        self._dispatch(self.fsm.fault, error_msg)

    @pyqtSlot(result=bool)
    def confirm_fault(self) -> bool:
        """``S4 -> S0`` after the user acknowledges the fault modal."""
        return self._dispatch(self.fsm.fault_confirmed)

    @pyqtSlot()
    def shutdown(self) -> None:
        """Direct termination (app exit / SIGINT / SIGTERM)."""
        self._dispatch(self.fsm.shutdown)

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
        self.engine.ingest_qcm(ts, freq_hz, temp_c)

    @pyqtSlot(float, dict)
    def _on_aux_packet(self, ts: float, aux: dict) -> None:
        if not self._is_current_worker(self.sender()):
            return
        self.engine.ingest_aux(ts, aux)

    @pyqtSlot(str)
    def _on_worker_fault(self, msg: str) -> None:
        if self._is_current_worker(self.sender()):
            self.on_fault_triggered(msg)

    # ---------------------------------------------------------- engine slots
    @pyqtSlot(int, float)
    def _on_qcm_tare_ready(self, f_hz: int, t_c: float) -> None:
        self._dispatch(self.fsm.qcm_tare_ready)

    @pyqtSlot(dict)
    def _on_aux_zoh_primed(self, aux: dict) -> None:
        self._dispatch(self.fsm.aux_zoh_primed)

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
        state = self.fsm.state
        if state is not self._last_emitted_state:
            self._last_emitted_state = state
            self.state_changed.emit(state)

    # ------------------------------------------------------ action helpers
    def _teardown_workers(self) -> None:
        workers = [w for w in (self.qcm_reader, self.aux_reader) if w is not None]
        self.qcm_reader = None
        self.aux_reader = None
        for w in workers:
            self._unbind_worker(w)
            w.stop_and_close()
            w.deleteLater()

    # --- HostActions guards -----------------------------------------------
    def ports_available(self) -> bool:
        cfg = self.config
        if cfg.mock:
            return True
        if not (cfg.qcm_port and cfg.aux_port):
            self.last_error = "both --qcm-port and --aux-port are required (or use --mock)"
            return False
        return True

    def ports_healthy(self) -> bool:
        q, a = self.qcm_reader, self.aux_reader
        return q is not None and a is not None and q.is_healthy and a.is_healthy

    # --- HostActions methods ----------------------------------------------
    def open_hardware(self) -> bool:
        self._teardown_workers()  # defensive: never leak a previous session's threads
        qcm, aux = self._reader_factory(self.config)
        if not qcm.open_port() or not aux.open_port():
            self.last_error = qcm.last_error or aux.last_error or "failed to open serial ports"
            for w in (qcm, aux):
                w.stop_and_close()
                w.deleteLater()
            return False
        self.qcm_reader, self.aux_reader = qcm, aux
        self._bind_workers(qcm, aux)
        qcm.start()
        aux.start()
        return True

    def begin_session(self) -> None:
        assert self.qcm_reader is not None and self.aux_reader is not None
        armed_at = time.monotonic()
        self.qcm_reader.purge_input_buffer()  # guardrail 5: only on S1 -> S2
        self.aux_reader.purge_input_buffer()
        path = self.csv_logger.open_session_csv()
        self.engine.reset_for_tare(armed_at)
        self.session_started.emit(str(path))

    def begin_streaming(self, f0_hz: int, t0_c: float) -> None:
        # The engine already froze the baseline and entered STREAMING; CSV
        # logging is driven by engine.fused_row_ready.
        pass

    def transmit_pwm(self, duty: int) -> None:
        if self.aux_reader is None:
            raise RuntimeError("AUX reader not running")
        self.aux_reader.send_pwm_command(duty)

    def park_hardware(self, reason: str) -> None:
        self._release_all()

    def show_fault(self, reason: str) -> None:
        self.fault_raised.emit(reason)

    def clear_fault(self) -> None:
        self.fault_cleared.emit()

    def terminate(self) -> None:
        self._release_all()

    def _release_all(self) -> None:
        self.engine.invalidate()  # invalidate ZOH cache, halt derivative math
        try:
            self.csv_logger.flush_and_close()
        finally:
            self._teardown_workers()  # stop QThreads, close ports
