"""Base QThread pyserial manager: line framing, buffer purge, TX queue & fault handling.

Guardrail 3 (non-blocking GUI): every ``readline()``, ``write()`` and
``reset_input_buffer()`` call executes inside :meth:`BaseSerialWorker.run`
on the worker thread. Methods callable from the GUI thread
(:meth:`purge_input_buffer`, :meth:`enqueue_tx`, :meth:`stop_and_close`)
only set thread-safe flags / queues that the worker loop services.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

try:
    import serial  # type: ignore[import-untyped]
except ModuleNotFoundError:  # pragma: no cover - fallback when pyserial is not installed

    class _SerialFallback:
        EIGHTBITS = 8
        PARITY_NONE = "N"
        STOPBITS_ONE = 1

        class SerialException(OSError):
            pass

        class Serial:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise _SerialFallback.SerialException(
                    "pyserial is not installed in the active Python environment"
                )

    serial = _SerialFallback()  # type: ignore[assignment]

from PyQt6.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)

DEFAULT_BAUD = 115_200


class BaseSerialWorker(QThread):
    serial_fault = pyqtSignal(str)
    bytes_transmitted = pyqtSignal(bytes)

    READ_TIMEOUT_S: float = 0.1
    """Bounded ``readline()`` timeout so stop/purge/TX/watchdog are serviced at >= 10 Hz."""

    MAX_LINE_BYTES: int = 4096
    """Partial-line buffer cap (protects against a newline-free garbage stream)."""

    def __init__(
        self,
        port: Optional[str],
        baud: int = DEFAULT_BAUD,
        name: str = "serial",
        clock: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self.worker_name = name
        self.ser: Any = None
        self.last_error: Optional[str] = None
        self.malformed_count = 0
        self._clock = clock
        self._stop_evt = threading.Event()
        self._purge_evt = threading.Event()
        self._tx_queue: "queue.Queue[bytes]" = queue.Queue()
        self._rx_buf = bytearray()
        self._fault_emitted = False

    # ------------------------------------------------------------ overrides
    def _create_serial(self) -> Any:
        """Factory for the port handle (overridden by mock readers)."""
        return serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.READ_TIMEOUT_S,
            write_timeout=1.0,
        )

    def reset_watchdogs(self, now: float) -> None:
        """Called once on the worker thread before the read loop starts."""

    def handle_line(self, now: float, line: bytes) -> None:
        """Process one newline-delimited record (stripped). Runs on the worker thread."""
        raise NotImplementedError

    def check_watchdogs(self, now: float) -> Optional[str]:
        """Return a fault message if a no-data timeout expired, else ``None``."""
        return None

    # ------------------------------------------------------- GUI-thread API
    def open_port(self) -> bool:
        """Open the port handle (S0 -> S1 guard). Returns ``False`` on failure."""
        if self.ser is not None and getattr(self.ser, "is_open", False):
            return True
        try:
            self.ser = self._create_serial()
        except (serial.SerialException, OSError, ValueError) as exc:
            self.last_error = f"{self.worker_name}: cannot open {self.port!r}: {exc}"
            log.error(self.last_error)
            self.ser = None
            return False
        return bool(getattr(self.ser, "is_open", False))

    @property
    def is_port_open(self) -> bool:
        return self.ser is not None and bool(getattr(self.ser, "is_open", False))

    @property
    def is_healthy(self) -> bool:
        """``S1`` Start Run guard: running, port open, and no fault raised."""
        return self.is_port_open and self.isRunning() and not self._fault_emitted

    def purge_input_buffer(self) -> None:
        """Request ``reset_input_buffer()``; executed on the worker thread.

        Only legal on the ``S1 -> S2`` transition (guardrail 5).
        """
        self._purge_evt.set()

    def enqueue_tx(self, data: bytes) -> None:
        """Thread-safe enqueue of raw bytes for transmission by the worker."""
        self._tx_queue.put(bytes(data))

    def stop_and_close(self, timeout_ms: int = 2000) -> None:
        """Stop the worker loop and close the port (port closed on the worker thread)."""
        self._stop_evt.set()
        if self.isRunning():
            if not self.wait(timeout_ms):
                log.warning("%s worker did not stop within %d ms", self.worker_name, timeout_ms)
        else:
            self._close_port()

    # ----------------------------------------------------------- worker loop
    def run(self) -> None:  # noqa: D401 - QThread entry point
        if self.ser is None and not self.open_port():
            self._emit_fault(self.last_error or f"{self.worker_name}: port not open")
            return
        self.reset_watchdogs(self._clock())
        try:
            while not self._stop_evt.is_set():
                if self._purge_evt.is_set():
                    self._purge_evt.clear()
                    self.ser.reset_input_buffer()
                    self._rx_buf.clear()
                self._drain_tx()

                chunk = self.ser.readline()
                now = self._clock()
                if self._purge_evt.is_set():
                    self._purge_evt.clear()
                    self.ser.reset_input_buffer()
                    self._rx_buf.clear()
                    chunk = b""
                if chunk:
                    self._consume(now, chunk)

                if self._stop_evt.is_set():
                    break
                fault = self.check_watchdogs(now)
                if fault:
                    self._emit_fault(fault)
                    break
        except (serial.SerialException, OSError) as exc:
            if not self._stop_evt.is_set():
                self._emit_fault(f"{self.worker_name} serial error: {exc}")
        except Exception as exc:  # never let the thread die silently
            log.exception("%s worker crashed", self.worker_name)
            self._emit_fault(f"{self.worker_name} worker crashed: {exc}")
        finally:
            self._close_port()

    def _drain_tx(self) -> None:
        while True:
            try:
                data = self._tx_queue.get_nowait()
            except queue.Empty:
                return
            self.ser.write(data)
            if hasattr(self.ser, "flush"):
                self.ser.flush()
            self.bytes_transmitted.emit(data)

    def _consume(self, now: float, chunk: bytes) -> None:
        self._rx_buf.extend(chunk)
        while True:
            idx = self._rx_buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._rx_buf[:idx]).strip()
            del self._rx_buf[: idx + 1]
            if line:
                self.handle_line(now, line)
        if len(self._rx_buf) > self.MAX_LINE_BYTES:
            self.malformed_count += 1
            self._rx_buf.clear()

    def _emit_fault(self, message: str) -> None:
        if self._fault_emitted:
            return
        self._fault_emitted = True
        self.last_error = message
        log.error("%s", message)
        self.serial_fault.emit(message)

    def _close_port(self) -> None:
        ser = self.ser
        if ser is None:
            return
        try:
            ser.close()
        except Exception:  # pragma: no cover - best effort
            log.exception("%s: error closing port", self.worker_name)

