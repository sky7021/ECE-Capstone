"""Application entry point, CLI parser, QThread lifecycle and signal wiring (spec §4.A.1)."""

from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QCoreApplication, QObject, QTimer
from PyQt6.QtWidgets import QApplication

from software.fsm.host_controller import (
    HostConfig,
    ReaderFactory,
    default_reader_factory,
)
from software.gui.main_window import MainWindow
from software.pipeline.csv_logger import DEFAULT_LOG_DIR, CSVLogger
from software.pipeline.zoh_fusion import ZOHFusionEngine
from software.readers.aux_reader import AuxReader
from software.readers.qcm_reader import QCMReader
from software.readers.serial_worker import DEFAULT_BAUD


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse ``--mock``, ``--qcm-port``, ``--aux-port``, ``--baud``, and ``--log-dir``."""
    ap = argparse.ArgumentParser(description="ECE 490 Host PC Master Orchestrator & GUI")
    ap.add_argument(
        "--mock",
        action="store_true",
        help="enable in-process synthetic QThread generators",
    )
    ap.add_argument("--qcm-port", help="Basys 3 FPGA COM port (Link A)")
    ap.add_argument("--aux-port", help="ESP32 COM port (Link B)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    return ap.parse_args(argv)


class ApplicationController(QObject):
    """Top-level orchestrator wiring readers, ZOH fusion, CSV logger and MainWindow."""

    def __init__(
        self,
        config: Optional[HostConfig] = None,
        main_window: Optional[MainWindow] = None,
        reader_factory: Optional[ReaderFactory] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or HostConfig(mock=True)
        self._reader_factory = reader_factory or default_reader_factory
        self.qcm_reader: Optional[QCMReader] = None
        self.aux_reader: Optional[AuxReader] = None
        if main_window is not None:
            self.main_window = main_window
            self.zoh_fusion = main_window.zoh_fusion
            self.zoh_fusion.flow_timeout_s = self.config.flow_timeout_s
            self.zoh_fusion.sht_timeout_s = self.config.sht_timeout_s
            self.csv_logger = main_window.csv_logger
            self.csv_logger.log_dir = Path(self.config.log_dir)
        else:
            self.zoh_fusion = ZOHFusionEngine(
                flow_timeout_s=self.config.flow_timeout_s,
                sht_timeout_s=self.config.sht_timeout_s,
                parent=self,
            )
            self.csv_logger = CSVLogger(self.config.log_dir)
            self.main_window = MainWindow(
                zoh_fusion=self.zoh_fusion,
                csv_logger=self.csv_logger,
                modal_enabled=False,
            )
        self._signals_bound = False

    def init_workers(self) -> bool:
        """Execute ``S0 -> HS_CONNECTED (S1: IDLE)`` guard check, open ports & start workers."""
        cfg = self.config
        if not cfg.mock and not (cfg.qcm_port and cfg.aux_port):
            return False

        qcm, aux = self._reader_factory(cfg)
        if not qcm.open_port() or not aux.open_port():
            qcm.stop_and_close()
            aux.stop_and_close()
            return False

        self.qcm_reader = qcm
        self.aux_reader = aux
        self.main_window.qcm_reader = qcm
        self.main_window.aux_reader = aux
        self.bind_signals()
        qcm.start()
        aux.start()
        self.main_window.on_ports_connected()
        return True

    def bind_signals(self) -> None:
        """Wire reader, fusion, logger, and MainWindow signals and fault slots."""
        if not self._signals_bound:
            self.zoh_fusion.fused_row_ready.connect(self.csv_logger.log_fused_row)
            self.zoh_fusion.fused_row_ready.connect(self.main_window.update_plots)
            self.zoh_fusion.staleness_fault.connect(self.main_window.on_fault_triggered)
            self._signals_bound = True

        if self.qcm_reader is not None:
            self.qcm_reader.qcm_packet_received.connect(self.zoh_fusion.ingest_qcm)
            self.qcm_reader.serial_fault.connect(self.main_window.on_fault_triggered)
        if self.aux_reader is not None:
            self.aux_reader.aux_packet_received.connect(self.zoh_fusion.ingest_aux)
            self.aux_reader.serial_fault.connect(self.main_window.on_fault_triggered)

    def shutdown(self) -> None:
        """Execute ``HS_CONNECTED -> S0: DISCONNECTED`` direct termination."""
        self.main_window.fsm.shutdown()
        self.csv_logger.flush_and_close()
        if self.qcm_reader is not None:
            self.qcm_reader.stop_and_close()
        if self.aux_reader is not None:
            self.aux_reader.stop_and_close()


def configure_signal_handling(
    app: QCoreApplication, controller: ApplicationController
) -> QTimer:
    """Trap ``SIGINT`` and ``SIGTERM`` to invoke ``controller.shutdown()``."""

    def _handle_sig(_signum, _frame) -> None:
        controller.shutdown()
        app.quit()

    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sig)

    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(200)
    return timer


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_arguments(argv)
    app = QApplication.instance() or QApplication(sys.argv)
    config = HostConfig(
        mock=args.mock,
        qcm_port=args.qcm_port,
        aux_port=args.aux_port,
        baud=args.baud,
        log_dir=args.log_dir,
    )
    window = MainWindow(modal_enabled=True)
    controller = ApplicationController(config=config, main_window=window)
    _keepalive = configure_signal_handling(app, controller)
    controller.init_workers()
    window.show()
    app.aboutToQuit.connect(controller.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
