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
    HostController,
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
    """Backwards-compatible adapter delegating to unified HostController and MainWindow."""

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
        if main_window is not None:
            self.main_window = main_window
            self.controller = HostController(
                self.config, reader_factory=self._reader_factory, parent=self
            )
            self.main_window.set_controller(self.controller)
        else:
            self.controller = HostController(
                self.config, reader_factory=self._reader_factory, parent=self
            )
            self.main_window = MainWindow(controller=self.controller, modal_enabled=False)

    @property
    def qcm_reader(self) -> Optional[QCMReader]:
        return self.controller.qcm_reader

    @property
    def aux_reader(self) -> Optional[AuxReader]:
        return self.controller.aux_reader

    @property
    def zoh_fusion(self) -> ZOHFusionEngine:
        return self.controller.engine

    @property
    def csv_logger(self) -> CSVLogger:
        return self.controller.csv_logger

    def init_workers(self) -> bool:
        """``S0 -> S1`` boot ports and readers."""
        return self.controller.connect_ports()

    def shutdown(self) -> None:
        """Execute graceful shutdown."""
        self.controller.shutdown()


def configure_signal_handling(
    app: QCoreApplication, controller: HostController
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

    controller = HostController(config)
    window = MainWindow(controller=controller, modal_enabled=True)
    _keepalive = configure_signal_handling(app, controller)
    controller.connect_ports()
    window.show()

    app.aboutToQuit.connect(controller.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
