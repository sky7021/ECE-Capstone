"""Application entry point, CLI parser, QThread lifecycle and signal wiring (spec §4.A.1)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication

from software.fsm.host_controller import (
    HostConfig,
    HostController,
    configure_signal_handling,
)
from software.gui.main_window import MainWindow
from software.pipeline.csv_logger import DEFAULT_LOG_DIR
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
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    return ap.parse_args(argv)


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
