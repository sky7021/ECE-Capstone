"""Unified session CSV writer.

Lifecycle (spec §5.5 / Host FSM):

* ``open_session_csv()``  on ``S1 -> S2`` (``data/sessions/session_YYYYMMDD_HHMMSS.csv``)
* ``log_fused_row()``     for every fused ``S3`` sample (flushed per row: 1 Hz is cheap
                           and protects data against hard crashes / cable pulls)
* ``flush_and_close()``   on ``S4: FAULT`` entry or ``HS_CONNECTED -> S0`` exit

Pure Python; ``log_fused_row`` can be connected directly to a ``pyqtSignal(dict)``.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path
from typing import IO, Optional

CSV_COLUMNS: tuple[str, ...] = (
    "pc_timestamp_s",
    "qcm_freq_hz",
    "delta_f_hz",
    "df_dt_hz_s",
    "dm_dt_ng_s",
    "qcm_temp_c",
    "flow_lpm",
    "sht_temp_c",
    "sht_rh_pct",
    "pwm_duty",
    "zoh_flow_age_s",
    "zoh_sht_age_s",
)

DEFAULT_LOG_DIR: Path = Path(__file__).resolve().parents[2] / "data" / "sessions"


def _fmt(value: object) -> object:
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return repr(value)
    return value


class CSVLogger:
    def __init__(self, log_dir: str | Path = DEFAULT_LOG_DIR) -> None:
        self.log_dir = Path(log_dir)
        self._fh: Optional[IO[str]] = None
        self._writer: Optional[csv.DictWriter] = None
        self.path: Optional[Path] = None
        self.rows_written: int = 0

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    def open_session_csv(self, now: Optional[datetime] = None) -> Path:
        """Create a new session file and write the header. Closes any prior session."""
        self.flush_and_close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
        path = self.log_dir / f"session_{stamp}.csv"
        suffix = 1
        while path.exists():  # two sessions started within the same second
            path = self.log_dir / f"session_{stamp}_{suffix}.csv"
            suffix += 1

        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        self._writer.writeheader()
        self._fh.flush()
        self.path = path
        self.rows_written = 0
        return path

    def log_fused_row(self, row: dict) -> None:
        """Append one fused S3 row. Silently ignored when no session is open."""
        if self._writer is None or self._fh is None:
            return
        self._writer.writerow({k: _fmt(row.get(k, "")) for k in CSV_COLUMNS})
        self._fh.flush()
        self.rows_written += 1

    def flush_and_close(self) -> None:
        """Idempotent flush/close (safe to call on S4 entry *and* on app exit)."""
        fh, self._fh, self._writer = self._fh, None, None
        if fh is not None:
            try:
                fh.flush()
            finally:
                fh.close()

