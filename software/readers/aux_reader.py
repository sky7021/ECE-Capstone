"""Link B worker: 1 Hz ESP32 ASCII parser, 3.0 s / 6.0 s watchdogs & SET_PWM uplink."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal, pyqtSlot

from software.pipeline.quality import (
    FLOW_TIMEOUT_S,
    SHT_TIMEOUT_S,
    format_pwm_command,
    parse_aux_line,
)
from software.readers.serial_worker import DEFAULT_BAUD, BaseSerialWorker


class AuxReader(BaseSerialWorker):
    aux_packet_received = pyqtSignal(float, dict)  # pc_timestamp, aux_dict

    def __init__(
        self,
        port: Optional[str],
        baud: int = DEFAULT_BAUD,
        flow_timeout_s: float = FLOW_TIMEOUT_S,
        sht_timeout_s: float = SHT_TIMEOUT_S,
        **kwargs,
    ) -> None:
        kwargs.setdefault("name", "AUX")
        super().__init__(port, baud, **kwargs)
        self.flow_timeout_s = flow_timeout_s
        self.sht_timeout_s = sht_timeout_s
        self.last_flow_ts = 0.0
        self.last_fresh_sht_ts = 0.0

    def parse_ascii_line(self, line: bytes) -> Optional[dict]:
        return parse_aux_line(line)

    def reset_watchdogs(self, now: float) -> None:
        self.last_flow_ts = now
        self.last_fresh_sht_ts = now

    def handle_line(self, now: float, line: bytes) -> None:
        aux = self.parse_ascii_line(line)
        if aux is None:
            self.malformed_count += 1
            return
        self.last_flow_ts = now
        if aux["fresh_flag"]:
            self.last_fresh_sht_ts = now
        self.aux_packet_received.emit(now, aux)

    def check_watchdogs(self, now: float) -> Optional[str]:
        if now - self.last_flow_ts > self.flow_timeout_s:
            return f"AUX flow timeout > {self.flow_timeout_s:.1f}s"
        if now - self.last_fresh_sht_ts > self.sht_timeout_s:
            return f"AUX SHT21 fresh timeout > {self.sht_timeout_s:.1f}s"
        return None

    @pyqtSlot(int)
    def send_pwm_command(self, duty: int) -> None:
        """Thread-safe: enqueue ``SET_PWM:<duty>\\n`` for the worker to transmit.

        Never touches the input buffer (guardrail 5).
        """
        self.enqueue_tx(format_pwm_command(duty))

