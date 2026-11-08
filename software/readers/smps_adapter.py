"""Reference TSI-AIM / SMPS delimited file watcher and scan parser.

Parses delimited (CSV or tab-separated) SMPS export files into :class:`SMPSScan`
records for post-run or live dose-domain alignment in ``empirical_fit.py``.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from software.pipeline.empirical_fit import SMPSScan


def parse_smps_delimited(
    path: str | Path,
    default_scan_duration_s: float = 60.0,
) -> list[SMPSScan]:
    """Parse a delimited SMPS scan file into :class:`SMPSScan` objects.

    Recognises columns (case-insensitive):
    * ``scan_id`` / ``sample #`` / ``scan`` (defaults to 1-based row index)
    * ``start_t`` / ``start_s`` / ``time_s``
    * ``end_t`` / ``end_s`` (defaults to ``start_t + default_scan_duration_s``)
    * ``concentration_ug_m3`` / ``total_conc_ug_m3`` / ``mass_conc_ug_m3`` / ``conc_ug_m3``
    """
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return []

    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []

    delimiter = "\t" if "\t" in lines[0] and "," not in lines[0] else ","
    reader = csv.DictReader(lines, delimiter=delimiter)
    scans: list[SMPSScan] = []

    for idx, raw_row in enumerate(reader, start=1):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        try:
            scan_id = int(
                row.get("scan_id")
                or row.get("sample #")
                or row.get("scan")
                or idx
            )
            start_t = float(
                row.get("start_t")
                or row.get("start_s")
                or row.get("time_s")
                or (idx - 1) * default_scan_duration_s
            )
            end_s_raw = row.get("end_t") or row.get("end_s")
            end_t = float(end_s_raw) if end_s_raw else start_t + default_scan_duration_s
            conc_raw = (
                row.get("concentration_ug_m3")
                or row.get("total_conc_ug_m3")
                or row.get("mass_conc_ug_m3")
                or row.get("conc_ug_m3")
            )
            if conc_raw is None:
                continue
            conc = float(conc_raw)
        except ValueError:
            continue

        if not (math.isfinite(start_t) and math.isfinite(end_t) and math.isfinite(conc)):
            continue
        if end_t <= start_t:
            continue

        scans.append(
            SMPSScan(
                scan_id=scan_id,
                start_t=start_t,
                end_t=end_t,
                concentration_ug_m3=conc,
            )
        )
    return scans


class SMPSFileWatcher(QObject):
    """Polls a delimited SMPS scan file and emits newly appended :class:`SMPSScan` records."""

    scan_received = pyqtSignal(object)  # SMPSScan

    def __init__(
        self,
        path: str | Path,
        poll_interval_ms: int = 1000,
        default_scan_duration_s: float = 60.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.default_scan_duration_s = default_scan_duration_s
        self._seen_count = 0
        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self.poll_once)

    def start(self) -> None:
        self.poll_once()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def poll_once(self) -> list[SMPSScan]:
        scans = parse_smps_delimited(self.path, self.default_scan_duration_s)
        new_scans = scans[self._seen_count :]
        self._seen_count = len(scans)
        for scan in new_scans:
            self.scan_received.emit(scan)
        return new_scans

