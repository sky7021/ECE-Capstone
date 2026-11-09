"""Headless runner for the Host PC backend (no GUI required).

Examples::

    python -m software.fsm.headless --mock --duration 30 --pwm 75@12
    python -m software.fsm.headless --qcm-port COM11 --aux-port COM13

Connects (S0 -> S1), starts a run (S1 -> S2), streams fused rows in S3,
optionally applies fan PWM steps (S5 -> H), and shuts down cleanly on
``--duration`` expiry, Ctrl+C, or after a fault (S4 is auto-confirmed -> S0).
"""

from __future__ import annotations

import argparse
import logging
import math
from pathlib import Path
import sys
from typing import Any, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QCoreApplication, QTimer

from software.fsm.host_controller import (
    HostConfig,
    HostController,
    configure_signal_handling,
)
from software.pipeline.csv_logger import DEFAULT_LOG_DIR
from software.readers.serial_worker import DEFAULT_BAUD


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="ECE 490 Host PC backend (headless)")
    ap.add_argument("--mock", action="store_true", help="use in-process synthetic QThread generators")
    ap.add_argument("--qcm-port", help="Basys 3 FPGA COM port (Link A)")
    ap.add_argument("--aux-port", help="ESP32 COM port (Link B)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--duration", type=float, default=0.0, help="seconds to run (0 = until Ctrl+C)")
    ap.add_argument(
        "--pwm",
        action="append",
        default=[],
        metavar="DUTY@SECONDS",
        help="schedule a fan PWM change, e.g. 75@12 (repeatable)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> HostConfig:
    return HostConfig(
        mock=args.mock,
        qcm_port=args.qcm_port,
        aux_port=args.aux_port,
        baud=args.baud,
        log_dir=args.log_dir,
    )


def _fmt(v: float, spec: str) -> str:
    return "nan" if isinstance(v, float) and math.isnan(v) else format(v, spec)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_arguments(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    controller = HostController(config_from_args(args))
    _keepalive = configure_signal_handling(app, controller)

    def on_transition(state: Any) -> None:
        print(f"[FSM] -> {controller.fsm.active_configuration}")

    def on_row(row: dict) -> None:
        print(
            f"[S3] f={row['qcm_freq_hz']} Hz  df={_fmt(row['delta_f_hz'], '+.1f')} Hz  "
            f"df/dt={_fmt(row['df_dt_hz_s'], '+.2f')} Hz/s  dm/dt={_fmt(row['dm_dt_ng_s'], '+.2f')} ng/s  "
            f"flow={row['flow_lpm']:.3f} L/min  RH={row['sht_rh_pct']:.1f}%  PWM={row['pwm_duty']}"
        )

    def on_fault(reason: str) -> None:
        print(f"[FAULT] {reason} (auto-confirming)")
        QTimer.singleShot(0, lambda: (controller.confirm_fault(), app.quit()))

    controller.state_changed.connect(on_transition)
    controller.fused_row_ready.connect(on_row)
    controller.fault_raised.connect(on_fault)
    controller.session_started.connect(lambda p: print(f"[CSV] session file: {p}"))

    if not controller.connect_ports():
        print(f"[ERROR] {controller.last_error or 'connection failed'}", file=sys.stderr)
        return 2
    if not controller.start_run():
        print("[ERROR] start rejected", file=sys.stderr)
        controller.shutdown()
        return 3

    for item in args.pwm:
        duty_s, _, at_s = item.partition("@")
        duty, at = int(duty_s), float(at_s or 0)
        QTimer.singleShot(int(at * 1000), lambda d=duty: controller.set_fan_pwm(d))

    if args.duration > 0:
        QTimer.singleShot(int(args.duration * 1000), app.quit)

    app.aboutToQuit.connect(controller.shutdown)
    app.exec()
    controller.shutdown()  # idempotent
    return 0


if __name__ == "__main__":
    sys.exit(main())
