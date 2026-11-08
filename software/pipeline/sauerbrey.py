"""Single-crystal baseline tare, net frequency shift and Sauerbrey dm/dt.

Formulation (spec §5):

    f0 = f_qcm(t0),  T0 = T_qcm(t0)                       (S2 -> S3 join action)
    delta_f(t_k) = f_qcm(t_k) - f0
    df/dt|t_k    = (delta_f(t_k) - delta_f(t_{k-1})) / (t_k - t_{k-1})
    C_QCM        = A * sqrt(rho_q * mu_q) / (2 * f0^2)    [g/Hz]
    delta_m(t_k) = -C_QCM * delta_f(t_k)
    dm/dt|t_k    = -C_QCM * df/dt|t_k                     [ng/s]

Pure Python; no Qt dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# AT-cut quartz / openQCM 5 MHz crystal constants (CGS units)
QUARTZ_DENSITY_G_CM3: float = 2.648
QUARTZ_SHEAR_MODULUS_G_CM_S2: float = 2.947e11
ELECTRODE_AREA_CM2: float = 0.205
NOMINAL_F0_HZ: float = 5.0e6

G_TO_NG: float = 1.0e9


def sauerbrey_constant_ng_per_hz(
    f0_hz: float = NOMINAL_F0_HZ,
    area_cm2: float = ELECTRODE_AREA_CM2,
    rho_q: float = QUARTZ_DENSITY_G_CM3,
    mu_q: float = QUARTZ_SHEAR_MODULUS_G_CM_S2,
) -> float:
    """Return ``C_QCM`` in ng/Hz (~3.62 ng/Hz for a 5 MHz, 0.205 cm^2 crystal)."""
    if f0_hz <= 0:
        raise ValueError(f"f0 must be positive, got {f0_hz!r}")
    c_g_per_hz = area_cm2 * math.sqrt(rho_q * mu_q) / (2.0 * f0_hz**2)
    return c_g_per_hz * G_TO_NG


@dataclass(frozen=True)
class SauerbreyResult:
    delta_f_hz: float
    df_dt_hz_s: float
    delta_m_ng: float
    dm_dt_ng_s: float


class SauerbreyCalculator:
    """Holds the frozen tare baseline and computes per-sample derivatives.

    ``C_QCM`` is evaluated with the *measured* frozen baseline ``f0`` (spec:
    "fundamental resonant frequency f0 ~= 5.0e6 Hz"); the difference to the
    5 MHz nominal is negligible but this keeps the math self-consistent.
    """

    def __init__(self, area_cm2: float = ELECTRODE_AREA_CM2) -> None:
        self._area_cm2 = area_cm2
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self) -> None:
        """Halt derivative math and discard the baseline (S4 / new session)."""
        self.f0_hz: Optional[int] = None
        self.t0_c: Optional[float] = None
        self.baseline_ts: Optional[float] = None
        self.c_qcm_ng_per_hz: Optional[float] = None
        self._prev_ts: Optional[float] = None
        self._prev_delta_f: float = 0.0

    @property
    def is_frozen(self) -> bool:
        return self.f0_hz is not None

    def freeze_baseline(self, ts: float, f0_hz: int, t0_c: float) -> None:
        """``S2 -> S3`` join action: freeze ``f0`` and ``T0`` at host time ``ts``.

        The baseline sample itself is the derivative anchor
        (``delta_f(t0) = 0``) so the very first S3 sample already yields a
        valid ``df/dt``.
        """
        self.f0_hz = int(f0_hz)
        self.t0_c = float(t0_c)
        self.baseline_ts = float(ts)
        self.c_qcm_ng_per_hz = sauerbrey_constant_ng_per_hz(self.f0_hz, self._area_cm2)
        self._prev_ts = float(ts)
        self._prev_delta_f = 0.0

    # ------------------------------------------------------------------- math
    def compute(self, ts: float, freq_hz: int) -> SauerbreyResult:
        """Compute ``delta_f``, ``df/dt``, ``delta_m`` and ``dm/dt`` for one sample.

        If the host timestamp did not advance (``dt <= 0``), the derivative is
        undefined and reported as ``NaN``; the derivative anchor is *not*
        updated in that case.
        """
        if not self.is_frozen:
            raise RuntimeError("Sauerbrey baseline not frozen (S2 join has not completed)")
        assert self.c_qcm_ng_per_hz is not None and self._prev_ts is not None

        delta_f = float(freq_hz - self.f0_hz)
        dt = ts - self._prev_ts
        if dt > 0:
            df_dt = (delta_f - self._prev_delta_f) / dt
            self._prev_ts = ts
            self._prev_delta_f = delta_f
        else:
            df_dt = math.nan

        c = self.c_qcm_ng_per_hz
        return SauerbreyResult(
            delta_f_hz=delta_f,
            df_dt_hz_s=df_dt,
            delta_m_ng=-c * delta_f,
            dm_dt_ng_s=-c * df_dt,
        )

