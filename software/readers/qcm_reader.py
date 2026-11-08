"""Link A worker: 1 Hz Basys 3 FPGA ASCII parser with 3.0 s watchdog."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal

from software.pipeline.quality import QCM_TIMEOUT_S, parse_qcm_line
from software.readers.serial_worker import DEFAULT_BAUD, BaseSerialWorker


class QCMReader(BaseSerialWorker):
    qcm_packet_received = pyqtSignal(float, int, float)  # pc_timestamp, qcm_freq_hz, qcm_temp_c

    def __init__(
        self,
        port: Optional[str],
        baud: int = DEFAULT_BAUD,
        timeout_s: float = QCM_TIMEOUT_S,
        **kwargs,
    ) -> None:
        kwargs.setdefault("name", "QCM")
        super().__init__(port, baud, **kwargs)
        self.timeout_s = timeout_s
        self.last_valid_ts = 0.0

    def parse_ascii_line(self, line: bytes) -> Optional[tuple[int, float]]:
        return parse_qcm_line(line)

    def reset_watchdogs(self, now: float) -> None:
        self.last_valid_ts = now

    def handle_line(self, now: float, line: bytes) -> None:
        parsed = self.parse_ascii_line(line)
        if parsed is None:
            self.malformed_count += 1
            return
        freq_hz, temp_c = parsed
        self.last_valid_ts = now
        self.qcm_packet_received.emit(now, freq_hz, temp_c)

    def check_watchdogs(self, now: float) -> Optional[str]:
        if now - self.last_valid_ts > self.timeout_s:
            return f"QCM timeout > {self.timeout_s:.1f}s"
        return None

