"""S2 AND-superstate priming latch and S3 Zero-Order-Hold fusion engine.

Orthogonal regions of ``S2: TARE_AND_PRIME``::

    S2.2 R_QCM_TARE : S2.A1 WAIT_QCM_FIRST --valid QCM-------------> S2.A2 QCM_TARE_READY
    S2.1 R_AUX_PRIME: S2.B1 WAIT_AUX_FIRST --valid AUX & OK=1-------> S2.B2 AUX_ZOH_PRIMED
    join [in(S2.A2) && in(S2.B2)] / freeze f0,T0; prime ZOH -> S3

The engine owns the *data plane* of the Host FSM: it decides when the S2
regions latch and when the join fires, and reports those facts through
signals. The *control plane* (which top-level state the host is in) is owned
by :class:`software.fsm.host_fsm.HostStateMachine`; the controller wires the
two together.

Design notes
------------
* While ``S2.A2`` waits for ``S2.B2`` the tentative baseline keeps tracking the
  latest QCM sample, so the frozen baseline is ``f_qcm(t0)`` at the join
  instant ``t0`` (spec §5.1) rather than a potentially stale first sample.
* ``S2.B2`` keeps refreshing the ZOH cache with subsequent AUX packets so the
  primed cache is never older than one AUX period at the join.
* Packets stamped before the ``reset_for_tare`` arm time are discarded, which
  complements ``reset_input_buffer()`` for lines already in flight between
  the reader thread and the GUI thread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from software.pipeline.quality import (
    FLOW_TIMEOUT_S,
    SHT_TIMEOUT_S,
    is_aux_health_ok,
    is_flow_fresh,
    is_sht_fresh,
    zoh_age,
)
from software.pipeline.sauerbrey import SauerbreyCalculator


@dataclass
class ZOHCache:
    """Latest AUX values held between 1 Hz updates (Zero-Order Hold)."""

    flow_lpm: float = math.nan
    sht_temp_c: float = math.nan
    sht_rh_pct: float = math.nan
    pwm_duty: int = 0
    last_flow_ts: float = -math.inf
    last_sht_ts: float = -math.inf
    is_valid: bool = False
    health_ok: bool = False

    def prime(self, ts: float, aux: dict) -> None:
        """``S2.B1 -> S2.B2``: initialise every field from the first healthy packet."""
        self.flow_lpm = float(aux["flow_lpm"])
        self.sht_temp_c = float(aux["sht_temp_c"])
        self.sht_rh_pct = float(aux["sht_rh_pct"])
        self.pwm_duty = int(aux["pwm_duty"])
        self.health_ok = bool(aux["health_ok"])
        self.last_flow_ts = ts
        # A held (FRESH=0) SHT value is at most one AUX period old, well inside
        # the 6 s budget, so it is accepted as the priming value.
        self.last_sht_ts = ts
        self.is_valid = True

    def refresh(self, ts: float, aux: dict) -> None:
        """S3 self-loop: flow/PWM every packet, SHT21 only on fresh (E2.1) polls."""
        self.flow_lpm = float(aux["flow_lpm"])
        self.pwm_duty = int(aux["pwm_duty"])
        self.health_ok = bool(aux["health_ok"])
        self.last_flow_ts = ts
        if aux.get("fresh_flag"):
            self.sht_temp_c = float(aux["sht_temp_c"])
            self.sht_rh_pct = float(aux["sht_rh_pct"])
            self.last_sht_ts = ts

    def invalidate(self) -> None:
        self.__init__()  # type: ignore[misc]  # reset every field to defaults


class FusionMode(Enum):
    DISARMED = auto()  # S0 / S1 / S4: packets are ignored
    PRIMING = auto()  # S2: TARE_AND_PRIME
    STREAMING = auto()  # S3: ACTIVE_STREAMING & ZOH_FUSION


class ZOHFusionEngine(QObject):
    qcm_tare_ready = pyqtSignal(int, float)  # S2.A1 -> S2.A2 (f_qcm, T_qcm)
    aux_zoh_primed = pyqtSignal(dict)  # S2.B1 -> S2.B2 (aux dict)
    s2_join_complete = pyqtSignal(int, float)  # S2 -> S3 (frozen f0, T0)
    fused_row_ready = pyqtSignal(dict)  # S3 fused sample (CSV schema keys + extras)
    staleness_fault = pyqtSignal(str)  # S3 ZOH age violation -> S4

    def __init__(
        self,
        flow_timeout_s: float = FLOW_TIMEOUT_S,
        sht_timeout_s: float = SHT_TIMEOUT_S,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.flow_timeout_s = flow_timeout_s
        self.sht_timeout_s = sht_timeout_s
        self.cache = ZOHCache()
        self.sauerbrey = SauerbreyCalculator()
        self.mode = FusionMode.DISARMED
        self.in_s2_a2 = False
        self.in_s2_b2 = False
        self._armed_at = -math.inf
        self._baseline: Optional[tuple[float, int, float]] = None  # (ts, f, T)

    # ------------------------------------------------------------ lifecycle
    def reset_for_tare(self, armed_at: Optional[float] = None) -> None:
        """``S1 -> S2`` entry: clear both regions to ``S2.A1`` / ``S2.B1``."""
        self._clear()
        self._armed_at = -math.inf if armed_at is None else armed_at
        self.mode = FusionMode.PRIMING

    def invalidate(self) -> None:
        """``S4`` / ``S0`` entry: invalidate ZOH cache and halt derivative math."""
        self._clear()
        self.mode = FusionMode.DISARMED

    def _clear(self) -> None:
        self.in_s2_a2 = False
        self.in_s2_b2 = False
        self._baseline = None
        self.cache.invalidate()
        self.sauerbrey.reset()

    # ------------------------------------------------------------ ingestion
    @pyqtSlot(float, int, float)
    def ingest_qcm(self, ts: float, freq_hz: int, qcm_temp_c: float) -> None:
        if ts < self._armed_at:
            return
        if self.mode is FusionMode.PRIMING:
            self._baseline = (ts, int(freq_hz), float(qcm_temp_c))
            if not self.in_s2_a2:
                self.in_s2_a2 = True
                self.qcm_tare_ready.emit(int(freq_hz), float(qcm_temp_c))
            self.check_s2_join()
        elif self.mode is FusionMode.STREAMING:
            self._fuse(ts, int(freq_hz), float(qcm_temp_c))

    @pyqtSlot(float, dict)
    def ingest_aux(self, ts: float, aux_data: dict) -> None:
        if ts < self._armed_at:
            return
        if self.mode is FusionMode.PRIMING:
            if not self.in_s2_b2:
                if not is_aux_health_ok(aux_data):
                    return  # guard: sensor health flags must be nominal
                self.cache.prime(ts, aux_data)
                self.in_s2_b2 = True
                self.aux_zoh_primed.emit(dict(aux_data))
                self.check_s2_join()
            else:
                self.cache.refresh(ts, aux_data)
        elif self.mode is FusionMode.STREAMING:
            self.cache.refresh(ts, aux_data)

    def check_s2_join(self) -> None:
        """Join completion ``[in(S2.A2) && in(S2.B2)]``."""
        if self.mode is not FusionMode.PRIMING or not (self.in_s2_a2 and self.in_s2_b2):
            return
        assert self._baseline is not None
        ts0, f0, t0 = self._baseline
        self.sauerbrey.freeze_baseline(ts0, f0, t0)
        self.mode = FusionMode.STREAMING  # state committed before notifying listeners
        self.s2_join_complete.emit(f0, t0)

    # --------------------------------------------------------------- fusion
    def _fuse(self, ts: float, freq_hz: int, qcm_temp_c: float) -> None:
        cache = self.cache
        flow_age = zoh_age(ts, cache.last_flow_ts)
        sht_age = zoh_age(ts, cache.last_sht_ts)

        if not cache.is_valid:
            reason = "ZOH cache invalid during S3"
        elif not is_flow_fresh(flow_age, self.flow_timeout_s):
            reason = f"ZOH flow stale: age {flow_age:.2f}s > {self.flow_timeout_s:.1f}s"
        elif not is_sht_fresh(sht_age, self.sht_timeout_s):
            reason = f"ZOH SHT21 stale: age {sht_age:.2f}s > {self.sht_timeout_s:.1f}s"
        else:
            reason = None

        if reason is not None:
            self.invalidate()
            self.staleness_fault.emit(reason)
            return

        result = self.sauerbrey.compute(ts, freq_hz)
        row = {
            "pc_timestamp_s": ts,
            "qcm_freq_hz": freq_hz,
            "delta_f_hz": result.delta_f_hz,
            "df_dt_hz_s": result.df_dt_hz_s,
            "dm_dt_ng_s": result.dm_dt_ng_s,
            "qcm_temp_c": qcm_temp_c,
            "flow_lpm": cache.flow_lpm,
            "sht_temp_c": cache.sht_temp_c,
            "sht_rh_pct": cache.sht_rh_pct,
            "pwm_duty": cache.pwm_duty,
            "zoh_flow_age_s": flow_age,
            "zoh_sht_age_s": sht_age,
            # extras (not part of the CSV schema; useful for plots)
            "delta_m_ng": result.delta_m_ng,
            "baseline_f0_hz": self.sauerbrey.f0_hz,
            "aux_health_ok": cache.health_ok,
        }
        self.fused_row_ready.emit(row)

