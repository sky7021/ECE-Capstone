"""Synthetic 1 Hz FPGA & ESP32 ASCII generators (``--mock`` mode and loopback emitter).

Tier 1 (in-process ``--mock``): :class:`MockQCMReader` / :class:`MockAuxReader`
subclass the real readers and only swap the pyserial handle for a
:class:`FakeSerialPort`, so line framing, regex parsing, watchdogs, buffer
purge and the ``SET_PWM`` TX queue are exercised exactly as in production.

Tier 2 (OS loopback): ``python -m software.tests.serial_mock --qcm-tx COM10 --aux-tx COM12``
drives the synthetic sources onto real (virtual) serial ports.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import random
import sys
import threading
import time
from collections import deque
from typing import Callable, Deque, Optional, Protocol

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from software.pipeline.quality import FLOW_TIMEOUT_S, QCM_TIMEOUT_S, SHT_TIMEOUT_S
from software.readers.aux_reader import AuxReader
from software.readers.qcm_reader import QCMReader
from software.readers.serial_worker import DEFAULT_BAUD, serial


# =============================================================================
# Synthetic device sources
# =============================================================================
class LineSource(Protocol):
    def line_for_tick(self, tick: int) -> bytes: ...


class SyntheticFPGASource:
    """Basys 3 F1->F2->F3 loop: 5 MHz QCM with N(0, 0.8 Hz) noise and a
    -3.5 Hz/s deposition ramp between t = 10 s and t = 70 s."""

    def __init__(
        self,
        f_start_hz: float = 5_000_000.0,
        noise_hz: float = 0.8,
        ramp_hz_s: float = -3.5,
        ramp_start_s: int = 10,
        ramp_end_s: int = 70,
        temp_c: float = 25.0,
        temp_noise_c: float = 0.05,
        seed: Optional[int] = None,
    ) -> None:
        self.f_start_hz = f_start_hz
        self.noise_hz = noise_hz
        self.ramp_hz_s = ramp_hz_s
        self.ramp_start_s = ramp_start_s
        self.ramp_end_s = ramp_end_s
        self.temp_c = temp_c
        self.temp_noise_c = temp_noise_c
        self._rng = random.Random(seed)

    def ideal_freq(self, tick: int) -> float:
        elapsed_ramp = min(max(tick, self.ramp_start_s), self.ramp_end_s) - self.ramp_start_s
        return self.f_start_hz + self.ramp_hz_s * elapsed_ramp

    def line_for_tick(self, tick: int) -> bytes:
        f = int(round(self.ideal_freq(tick) + self._rng.gauss(0.0, self.noise_hz)))
        temp_x16 = int(round((self.temp_c + self._rng.gauss(0.0, self.temp_noise_c)) * 16))
        sign = "-" if temp_x16 < 0 else ""
        return f"QCM_FREQ={f:010d},TEMP_X16={sign}{abs(temp_x16):06d}\r\n".encode("ascii")


class SyntheticESP32Source:
    """ESP32 E1 || E2: SHT21 fresh on even ticks (E2.1), held on odd ticks (E2.2);
    D6F flow scales with ``current_pwm``; parses ``SET_PWM:<duty>\\n``."""

    def __init__(
        self,
        sht_temp_c: float = 24.5,
        sht_rh_pct: float = 42.0,
        health_ok: bool = True,
        seed: Optional[int] = None,
    ) -> None:
        self.current_pwm = 0
        self.health_ok = health_ok
        self.base_temp_c = sht_temp_c
        self.base_rh_pct = sht_rh_pct
        self.sht_cache = (sht_temp_c, sht_rh_pct)
        self.commands_received: list[int] = []
        self._rx = bytearray()
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def handle_command(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)
            while b"\n" in self._rx:
                raw, _, rest = bytes(self._rx).partition(b"\n")
                self._rx = bytearray(rest)
                cmd = raw.decode("ascii", errors="ignore").strip()
                if cmd.startswith("SET_PWM:"):
                    try:
                        duty = int(cmd.split(":", 1)[1])
                    except ValueError:
                        continue
                    if 0 <= duty <= 100:
                        self.current_pwm = duty  # E1 Mealy self-loop
                        self.commands_received.append(duty)

    def line_for_tick(self, tick: int) -> bytes:
        with self._lock:
            pwm = self.current_pwm
        fresh = tick % 2 == 0
        if fresh:
            self.sht_cache = (
                round(self.base_temp_c + self._rng.gauss(0.0, 0.1), 2),
                round(self.base_rh_pct + self._rng.gauss(0.0, 0.3), 2),
            )
        temp, rh = self.sht_cache
        flow = max(0.0, 0.5 + (pwm / 100.0) * 1.5 + self._rng.gauss(0.0, 0.02))
        return (
            f"FLOW={flow:.3f},TEMP={temp:.2f},RH={rh:.2f},PWM={pwm},"
            f"FRESH={int(fresh)},OK={int(self.health_ok)}\r\n"
        ).encode("ascii")


# =============================================================================
# In-process fake pyserial handle
# =============================================================================
class FakeSerialPort:
    """Minimal ``serial.Serial`` stand-in emitting one line every ``period_s``.

    Lines accumulate in an emulated OS input buffer while unread, so
    ``reset_input_buffer()`` behaves like the real thing. Fault injection:
    :meth:`simulate_disconnect` (raises ``SerialException`` on the next I/O)
    and :meth:`pause_stream` / :meth:`resume_stream` (device goes silent).
    """

    def __init__(
        self,
        source: LineSource,
        period_s: float = 1.0,
        timeout: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        max_buffer_lines: int = 4096,
    ) -> None:
        self.source = source
        self.period_s = period_s
        self.timeout = timeout
        self.is_open = True
        self.purge_count = 0
        self.written: list[bytes] = []
        self._clock = clock
        self._buffer: Deque[bytes] = deque(maxlen=max_buffer_lines)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._tick = 0
        self._next_due = clock()
        self._paused = False
        self._disconnected = False

    # ----------------------------------------------------------- pyserial API
    @property
    def in_waiting(self) -> int:
        with self._lock:
            self._generate_due()
            return sum(len(b) for b in self._buffer)

    def readline(self) -> bytes:
        deadline = self._clock() + self.timeout
        while True:
            self._check_io()
            with self._lock:
                self._generate_due()
                if self._buffer:
                    return self._buffer.popleft()
                next_due = self._next_due if not self._paused else deadline
            now = self._clock()
            if now >= deadline:
                return b""
            self._wake.wait(max(0.0, min(deadline, next_due) - now))
            self._wake.clear()

    def write(self, data: bytes) -> int:
        self._check_io()
        self.written.append(bytes(data))
        handler = getattr(self.source, "handle_command", None)
        if handler is not None:
            handler(bytes(data))
        return len(data)

    def flush(self) -> None:
        self._check_io()

    def reset_input_buffer(self) -> None:
        self._check_io()
        with self._lock:
            self._generate_due()
            self._buffer.clear()
            self.purge_count += 1
        self._wake.set()

    def close(self) -> None:
        self.is_open = False
        self._wake.set()

    # -------------------------------------------------------- fault injection
    def simulate_disconnect(self) -> None:
        self._disconnected = True
        self._wake.set()

    def pause_stream(self) -> None:
        with self._lock:
            self._paused = True

    def resume_stream(self) -> None:
        with self._lock:
            self._paused = False
            self._next_due = self._clock()
        self._wake.set()

    def inject_raw(self, data: bytes) -> None:
        """Push arbitrary bytes (e.g. a malformed line) into the input buffer."""
        with self._lock:
            self._buffer.append(bytes(data))
        self._wake.set()

    # --------------------------------------------------------------- helpers
    def _check_io(self) -> None:
        if self._disconnected:
            raise serial.SerialException("device disconnected (simulated)")
        if not self.is_open:
            raise serial.SerialException("port is closed")

    def _generate_due(self) -> None:
        if self._paused:
            return
        now = self._clock()
        while now >= self._next_due:
            self._buffer.append(self.source.line_for_tick(self._tick))
            self._tick += 1
            self._next_due += self.period_s


# =============================================================================
# Mock readers (real reader logic + fake port)
# =============================================================================
class MockQCMReader(QCMReader):
    def __init__(
        self,
        period_s: float = 1.0,
        source: Optional[SyntheticFPGASource] = None,
        timeout_s: float = QCM_TIMEOUT_S,
        **kwargs,
    ) -> None:
        super().__init__("MOCK_QCM", kwargs.pop("baud", DEFAULT_BAUD), timeout_s=timeout_s, **kwargs)
        self.period_s = period_s
        self.source = source or SyntheticFPGASource()

    def _create_serial(self) -> FakeSerialPort:
        return FakeSerialPort(
            self.source, self.period_s, timeout=self.READ_TIMEOUT_S, clock=self._clock
        )

    @property
    def fake(self) -> FakeSerialPort:
        return self.ser


class MockAuxReader(AuxReader):
    def __init__(
        self,
        period_s: float = 1.0,
        source: Optional[SyntheticESP32Source] = None,
        flow_timeout_s: float = FLOW_TIMEOUT_S,
        sht_timeout_s: float = SHT_TIMEOUT_S,
        **kwargs,
    ) -> None:
        super().__init__(
            "MOCK_AUX",
            kwargs.pop("baud", DEFAULT_BAUD),
            flow_timeout_s=flow_timeout_s,
            sht_timeout_s=sht_timeout_s,
            **kwargs,
        )
        self.period_s = period_s
        self.source = source or SyntheticESP32Source()

    def _create_serial(self) -> FakeSerialPort:
        return FakeSerialPort(
            self.source, self.period_s, timeout=self.READ_TIMEOUT_S, clock=self._clock
        )

    @property
    def fake(self) -> FakeSerialPort:
        return self.ser


# =============================================================================
# Tier 2 loopback emitter CLI
# =============================================================================
def _emit_qcm(port: str, baud: int, period_s: float, stop: threading.Event) -> None:
    src = SyntheticFPGASource()
    with serial.Serial(port, baud, timeout=1) as ser:
        tick, t_next = 0, time.monotonic()
        while not stop.is_set():
            ser.write(src.line_for_tick(tick))
            tick += 1
            t_next += period_s
            stop.wait(max(0.0, t_next - time.monotonic()))


def _emit_aux(port: str, baud: int, period_s: float, stop: threading.Event) -> None:
    src = SyntheticESP32Source()
    with serial.Serial(port, baud, timeout=0.05) as ser:
        tick, t_next = 0, time.monotonic()
        while not stop.is_set():
            waiting = ser.in_waiting
            if waiting:
                src.handle_command(ser.read(waiting))
            if time.monotonic() >= t_next:
                ser.write(src.line_for_tick(tick))
                tick += 1
                t_next += period_s
            stop.wait(0.02)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Synthetic FPGA/ESP32 serial emitters for loopback tests")
    ap.add_argument("--qcm-tx", help="TX side of the FPGA virtual port pair (e.g. COM10)")
    ap.add_argument("--aux-tx", help="TX side of the ESP32 virtual port pair (e.g. COM12)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--period", type=float, default=1.0, help="seconds between lines")
    args = ap.parse_args(argv)
    if not (args.qcm_tx or args.aux_tx):
        ap.error("specify --qcm-tx and/or --aux-tx")

    stop = threading.Event()
    threads = []
    if args.qcm_tx:
        threads.append(threading.Thread(target=_emit_qcm, args=(args.qcm_tx, args.baud, args.period, stop), daemon=True))
    if args.aux_tx:
        threads.append(threading.Thread(target=_emit_aux, args=(args.aux_tx, args.baud, args.period, stop), daemon=True))
    for t in threads:
        t.start()
    print("Emitting synthetic streams; Ctrl+C to stop.")
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.2)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()

