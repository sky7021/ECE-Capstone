"""Virtual COM port pair orchestrator (Tier 2 loopback harness, spec §6).

Supports three loopback backends:

1. **POSIX PTY (`pty.openpty`)** on Linux / macOS.
2. **Windows `com0com`** paired ports (`COM10 <-> COM11` for Link A,
   `COM12 <-> COM13` for Link B).
3. **Cross-platform in-process byte pipe (`InProcessLoopbackPair`)** that
   faithfully implements the `pyserial` stream API (`readline`, `read`, `write`,
   `in_waiting`, `reset_input_buffer`, `close`, `SerialException` on cable
   disconnect) without requiring OS kernel driver installation.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from software.fsm.host_controller import HostConfig
from software.readers.aux_reader import AuxReader
from software.readers.qcm_reader import QCMReader
from software.readers.serial_worker import DEFAULT_BAUD, serial
from software.tests.serial_mock import SyntheticESP32Source, SyntheticFPGASource


class _BytePipe:
    """Thread-safe byte stream connecting one writer to one reader."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._clock = clock
        self.purge_count = 0

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        with self._lock:
            self._buf.extend(data)
        self._wake.set()
        return len(data)

    def read_available(self, size: Optional[int] = None) -> bytes:
        with self._lock:
            if size is None or size >= len(self._buf):
                out = bytes(self._buf)
                self._buf.clear()
            else:
                out = bytes(self._buf[:size])
                del self._buf[:size]
            return out

    def readline(self, timeout: float, check_io: Callable[[], None]) -> bytes:
        deadline = self._clock() + timeout
        while True:
            check_io()
            with self._lock:
                nl = self._buf.find(b"\n")
                if nl >= 0:
                    out = bytes(self._buf[: nl + 1])
                    del self._buf[: nl + 1]
                    return out
            now = self._clock()
            if now >= deadline:
                with self._lock:
                    if self._buf:
                        out = bytes(self._buf)
                        self._buf.clear()
                        return out
                return b""
            self._wake.wait(max(0.0, min(0.02, deadline - now)))
            self._wake.clear()

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self.purge_count += 1
        self._wake.set()

    def notify(self) -> None:
        self._wake.set()


class LoopbackSerialEndpoint:
    """One end of a cross-wired bidirectional virtual serial cable."""

    def __init__(
        self,
        name: str,
        rx_pipe: _BytePipe,
        tx_pipe: _BytePipe,
        timeout: float = 0.1,
    ) -> None:
        self.port = name
        self.timeout = timeout
        self.is_open = True
        self._rx = rx_pipe
        self._tx = tx_pipe
        self._disconnected = False
        self.written: Deque[bytes] = deque(maxlen=1024)

    @property
    def in_waiting(self) -> int:
        self._check_io()
        return self._rx.in_waiting

    @property
    def purge_count(self) -> int:
        return self._rx.purge_count

    def readline(self) -> bytes:
        return self._rx.readline(self.timeout, self._check_io)

    def read(self, size: int = 1) -> bytes:
        self._check_io()
        return self._rx.read_available(size)

    def write(self, data: bytes) -> int:
        self._check_io()
        payload = bytes(data)
        self.written.append(payload)
        return self._tx.write(payload)

    def flush(self) -> None:
        self._check_io()

    def reset_input_buffer(self) -> None:
        self._check_io()
        self._rx.clear()

    def close(self) -> None:
        self.is_open = False
        self._rx.notify()
        self._tx.notify()

    def simulate_disconnect(self) -> None:
        self._disconnected = True
        self._rx.notify()
        self._tx.notify()

    def _check_io(self) -> None:
        if self._disconnected:
            raise serial.SerialException(f"{self.port}: loopback cable unplugged")
        if not self.is_open:
            raise serial.SerialException(f"{self.port}: port is closed")


@dataclass
class InProcessLoopbackPair:
    """Cross-wired virtual serial pair (``tx_end`` <-> ``rx_end``)."""

    tx_end: LoopbackSerialEndpoint
    rx_end: LoopbackSerialEndpoint

    def disconnect(self) -> None:
        self.tx_end.simulate_disconnect()
        self.rx_end.simulate_disconnect()

    def close(self) -> None:
        self.tx_end.close()
        self.rx_end.close()


def create_inprocess_loopback_pair(
    tx_name: str = "LOOP_TX",
    rx_name: str = "LOOP_RX",
    timeout: float = 0.1,
) -> InProcessLoopbackPair:
    """Create a cross-wired pair where ``tx_end.write`` feeds ``rx_end.readline`` and vice versa."""
    a_to_b = _BytePipe()
    b_to_a = _BytePipe()
    tx_end = LoopbackSerialEndpoint(tx_name, rx_pipe=b_to_a, tx_pipe=a_to_b, timeout=timeout)
    rx_end = LoopbackSerialEndpoint(rx_name, rx_pipe=a_to_b, tx_pipe=b_to_a, timeout=timeout)
    return InProcessLoopbackPair(tx_end=tx_end, rx_end=rx_end)


@dataclass
class OSSerialPair:
    """OS-level virtual serial port names (POSIX PTY or Windows com0com)."""

    tx_port: str
    rx_port: str
    master_fd: Optional[int] = None
    slave_fd: Optional[int] = None

    def close(self) -> None:
        for fd in (self.master_fd, self.slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def create_os_loopback_pair(
    default_tx: str = "COM10",
    default_rx: str = "COM11",
) -> OSSerialPair:
    """Create a POSIX ``pty`` pair when available, or return Windows ``com0com`` port names."""
    if os.name == "posix":
        import pty

        master_fd, slave_fd = pty.openpty()
        return OSSerialPair(
            tx_port=os.ttyname(master_fd),
            rx_port=os.ttyname(slave_fd),
            master_fd=master_fd,
            slave_fd=slave_fd,
        )
    return OSSerialPair(tx_port=default_tx, rx_port=default_rx)


class LoopbackQCMReader(QCMReader):
    """QCMReader bound to a :class:`LoopbackSerialEndpoint`."""

    def __init__(self, endpoint: LoopbackSerialEndpoint, **kwargs) -> None:
        super().__init__(endpoint.port, **kwargs)
        self._endpoint = endpoint
        self._endpoint.timeout = self.READ_TIMEOUT_S

    def _create_serial(self) -> LoopbackSerialEndpoint:
        return self._endpoint


class LoopbackAuxReader(AuxReader):
    """AuxReader bound to a :class:`LoopbackSerialEndpoint`."""

    def __init__(self, endpoint: LoopbackSerialEndpoint, **kwargs) -> None:
        super().__init__(endpoint.port, **kwargs)
        self._endpoint = endpoint
        self._endpoint.timeout = self.READ_TIMEOUT_S

    def _create_serial(self) -> LoopbackSerialEndpoint:
        return self._endpoint


class VirtualLoopbackHarness:
    """End-to-end Tier 2 loopback orchestrator for Link A (FPGA) and Link B (ESP32).

    Spawns background device threads that write 1 Hz (or accelerated) ASCII
    lines into the ``tx_end`` of each virtual cable while the Host PC's
    ``QCMReader`` and ``AuxReader`` read/write the ``rx_end``.
    """

    def __init__(
        self,
        period_s: float = 0.05,
        fpga_source: Optional[SyntheticFPGASource] = None,
        esp32_source: Optional[SyntheticESP32Source] = None,
    ) -> None:
        self.period_s = period_s
        self.fpga_source = fpga_source or SyntheticFPGASource(seed=42)
        self.esp32_source = esp32_source or SyntheticESP32Source(seed=42)
        self.qcm_pair = create_inprocess_loopback_pair("ttyV_QCM_TX", "ttyV_QCM_RX")
        self.aux_pair = create_inprocess_loopback_pair("ttyV_AUX_TX", "ttyV_AUX_RX")
        self._stop = threading.Event()
        self._qcm_pause = threading.Event()
        self._aux_pause = threading.Event()
        self._threads: list[threading.Thread] = []

    def start_devices(self) -> None:
        self._stop.clear()
        t_qcm = threading.Thread(target=self._run_fpga, name="LoopbackFPGA", daemon=True)
        t_aux = threading.Thread(target=self._run_esp32, name="LoopbackESP32", daemon=True)
        self._threads = [t_qcm, t_aux]
        for t in self._threads:
            t.start()

    def stop_devices(self) -> None:
        self._stop.set()
        self.qcm_pair.close()
        self.aux_pair.close()
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()

    def reader_factory(self, config: HostConfig) -> tuple[QCMReader, AuxReader]:
        return (
            LoopbackQCMReader(self.qcm_pair.rx_end, baud=config.baud, timeout_s=config.qcm_timeout_s),
            LoopbackAuxReader(
                self.aux_pair.rx_end,
                baud=config.baud,
                flow_timeout_s=config.flow_timeout_s,
                sht_timeout_s=config.sht_timeout_s,
            ),
        )

    def pause_qcm(self) -> None:
        self._qcm_pause.set()

    def pause_aux(self) -> None:
        self._aux_pause.set()

    def disconnect_qcm(self) -> None:
        self.qcm_pair.disconnect()

    def disconnect_aux(self) -> None:
        self.aux_pair.disconnect()

    def _run_fpga(self) -> None:
        tick = 0
        tx = self.qcm_pair.tx_end
        while not self._stop.is_set() and tx.is_open:
            if not self._qcm_pause.is_set():
                try:
                    tx.write(self.fpga_source.line_for_tick(tick))
                    tick += 1
                except serial.SerialException:
                    break
            self._stop.wait(self.period_s)

    def _run_esp32(self) -> None:
        tick = 0
        tx = self.aux_pair.tx_end
        while not self._stop.is_set() and tx.is_open:
            try:
                waiting = tx.in_waiting
                if waiting:
                    self.esp32_source.handle_command(tx.read(waiting))
                if not self._aux_pause.is_set():
                    tx.write(self.esp32_source.line_for_tick(tick))
                    tick += 1
            except serial.SerialException:
                break
            self._stop.wait(self.period_s)

    def __enter__(self) -> "VirtualLoopbackHarness":
        self.start_devices()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop_devices()


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Tier 2 virtual loopback port orchestrator")
    ap.add_argument("--qcm-tx", default="COM10")
    ap.add_argument("--qcm-rx", default="COM11")
    ap.add_argument("--aux-tx", default="COM12")
    ap.add_argument("--aux-rx", default="COM13")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    args = ap.parse_args(argv)
    qcm_pair = create_os_loopback_pair(args.qcm_tx, args.qcm_rx)
    aux_pair = create_os_loopback_pair(args.aux_tx, args.aux_rx)
    print(f"Link A (FPGA):  TX={qcm_pair.tx_port} <-> RX={qcm_pair.rx_port}")
    print(f"Link B (ESP32): TX={aux_pair.tx_port} <-> RX={aux_pair.rx_port}")


if __name__ == "__main__":
    main()

