"""Integral dose transform (D_SMPS) and empirical calibration constant (K_empirical) solver.

Formulation (spec §5.4):

Because the QCM is an integrating cumulative mass transducer (``delta_f ~ delta_m``)
whereas the reference Scanning Mobility Particle Sizer (SMPS) measures instantaneous
volumetric concentration ``C_SMPS(t)`` (ug/m^3), the domains are aligned via the
Integral Dose Transform over the SMPS scan window ``[t_start, t_end]``:

    D_SMPS(t) = integral_{t_start}^{t} Q(tau) * C_SMPS(tau) d(tau)

where ``Q(tau)`` is the ZOH-held volumetric airflow rate from the Omron D6F sensor,
solving for ``K_empirical`` via linear least squares through the origin:

    |delta_f_particle(t)| = K_empirical * D_SMPS(t)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

LPM_TO_M3_PER_S: float = 1.0 / 60_000.0
"""Conversion factor from volumetric flow in L/min to m^3/s (1 L = 1e-3 m^3)."""


@dataclass(frozen=True)
class SMPSScan:
    """Single reference SMPS scan summary."""

    scan_id: int
    start_t: float
    end_t: float
    concentration_ug_m3: float


@dataclass(frozen=True)
class EmpiricalFitResult:
    """Result of solving ``|delta_f(t)| = K_empirical * D_SMPS(t)``."""

    k_empirical: float
    r_squared: float
    doses: tuple[float, ...]
    abs_delta_f_hz: tuple[float, ...]
    residuals_hz: tuple[float, ...]


@dataclass(frozen=True)
class ScanAlignmentRecord:
    """Transport-lag-aligned QCM/SMPS paired scan record."""

    scan_id: int
    window_start_s: float
    window_end_s: float
    concentration_ug_m3: float
    smps_dose_ug_s_m3: float
    sampled_volume_m3: float
    mass_dose_ug: float
    delta_f_net_hz: float
    k_scan: float


def compute_cumulative_dose(
    timestamps_s: Sequence[float],
    flow_m3_s: Sequence[float],
    concentration_ug_m3: Sequence[float],
) -> np.ndarray:
    """Evaluate ``D_SMPS(t_k) = integral_{t_0}^{t_k} Q(tau) * C_SMPS(tau) d(tau)``.

    Uses piecewise trapezoidal integration over strictly non-decreasing
    timestamps ``timestamps_s``. With ``flow_m3_s`` in m^3/s and
    ``concentration_ug_m3`` in ug/m^3, ``D_SMPS`` has units of ug (total
    challenge aerosol mass entering the QCM branch). Pass ``flow_m3_s`` of all
    ones to obtain the concentration-time exposure integral in ``ug*s/m^3``.
    """
    t = np.asarray(timestamps_s, dtype=float)
    q = np.asarray(flow_m3_s, dtype=float)
    c = np.asarray(concentration_ug_m3, dtype=float)
    if not (t.ndim == q.ndim == c.ndim == 1 and len(t) == len(q) == len(c)):
        raise ValueError("timestamps_s, flow_m3_s, and concentration_ug_m3 must be 1D of equal length")
    if len(t) == 0:
        return np.array([], dtype=float)
    dt = np.diff(t)
    if np.any(dt < 0):
        raise ValueError("timestamps_s must be monotonically non-decreasing")
    integrand = q * c
    increments = 0.5 * (integrand[:-1] + integrand[1:]) * dt
    return np.concatenate(([0.0], np.cumsum(increments)))


def flow_lpm_to_m3_s(flow_lpm: Sequence[float] | float) -> np.ndarray | float:
    """Convert Omron D6F flow rate from L/min to m^3/s."""
    if isinstance(flow_lpm, (int, float)):
        return float(flow_lpm) * LPM_TO_M3_PER_S
    return np.asarray(flow_lpm, dtype=float) * LPM_TO_M3_PER_S


def solve_k_empirical(
    doses: Sequence[float],
    delta_f_hz: Sequence[float],
) -> EmpiricalFitResult:
    """Solve ``|delta_f(t)| = K_empirical * D_SMPS(t)`` via linear least squares.

    For the single-parameter model through the origin ``y = K * x``:
        ``K = sum(x_i * y_i) / sum(x_i^2)``
    """
    x = np.asarray(doses, dtype=float)
    y = np.abs(np.asarray(delta_f_hz, dtype=float))
    if x.ndim != 1 or y.ndim != 1 or len(x) != len(y) or len(x) == 0:
        raise ValueError("doses and delta_f_hz must be non-empty 1D sequences of equal length")
    denom = float(np.dot(x, x))
    if denom <= 0.0:
        raise ValueError("sum of squared doses must be positive to solve K_empirical")

    k = float(np.dot(x, y) / denom)
    y_hat = k * x
    residuals = y - y_hat
    ss_res = float(np.dot(residuals, residuals))
    ss_tot = float(np.dot(y - np.mean(y), y - np.mean(y)))
    if ss_tot > 0.0:
        r2 = 1.0 - ss_res / ss_tot
    else:
        r2 = 1.0 if ss_res == 0.0 else 0.0

    return EmpiricalFitResult(
        k_empirical=k,
        r_squared=r2,
        doses=tuple(float(v) for v in x),
        abs_delta_f_hz=tuple(float(v) for v in y),
        residuals_hz=tuple(float(v) for v in residuals),
    )


def align_and_solve_scans(
    scans: Iterable[SMPSScan],
    qcm_timestamps_s: Sequence[float],
    qcm_freq_hz: Sequence[float],
    flow_m3_s: Sequence[float],
    transport_lag_s: float = 0.0,
    use_mass_dose: bool = False,
) -> tuple[list[ScanAlignmentRecord], EmpiricalFitResult]:
    """Align SMPS scan windows with transport-shifted QCM series and fit ``K_empirical``.

    Parameters
    ----------
    scans:
        Reference SMPS scan windows.
    qcm_timestamps_s, qcm_freq_hz, flow_m3_s:
        1 Hz (or higher-rate) QCM and flow arrays.
    transport_lag_s:
        Plumbing transport delay (seconds) between SMPS and QCM branch.
    use_mass_dose:
        If ``True``, solves against ``D_SMPS = integral Q(tau)*C(tau) d(tau)`` (ug).
        If ``False`` (default), solves against concentration-time dose
        ``C_SMPS * (t_end - t_start)`` (ug*s/m^3).
    """
    t = np.asarray(qcm_timestamps_s, dtype=float)
    f = np.asarray(qcm_freq_hz, dtype=float)
    q = np.asarray(flow_m3_s, dtype=float)
    records: list[ScanAlignmentRecord] = []

    for scan in scans:
        w_start = scan.start_t + transport_lag_s
        w_end = scan.end_t + transport_lag_s
        mask = (t >= w_start) & (t <= w_end)
        t_w = t[mask]
        f_w = f[mask]
        q_w = q[mask]
        if len(t_w) < 2:
            raise ValueError(f"Scan {scan.scan_id}: fewer than 2 QCM samples in [{w_start}, {w_end}]")

        duration = scan.end_t - scan.start_t
        smps_dose = scan.concentration_ug_m3 * duration
        c_vec = np.full_like(t_w, scan.concentration_ug_m3, dtype=float)
        mass_dose_curve = compute_cumulative_dose(t_w, q_w, c_vec)
        mass_dose = float(mass_dose_curve[-1])
        vol_curve = compute_cumulative_dose(t_w, q_w, np.ones_like(t_w))
        sampled_volume = float(vol_curve[-1])

        delta_f_net = float(f_w[-1] - f_w[0])
        dose_for_k = mass_dose if use_mass_dose else smps_dose
        k_scan = abs(delta_f_net) / dose_for_k if dose_for_k > 0 else math.nan

        records.append(
            ScanAlignmentRecord(
                scan_id=scan.scan_id,
                window_start_s=w_start,
                window_end_s=w_end,
                concentration_ug_m3=scan.concentration_ug_m3,
                smps_dose_ug_s_m3=smps_dose,
                sampled_volume_m3=sampled_volume,
                mass_dose_ug=mass_dose,
                delta_f_net_hz=delta_f_net,
                k_scan=k_scan,
            )
        )

    doses = [r.mass_dose_ug if use_mass_dose else r.smps_dose_ug_s_m3 for r in records]
    deltas = [r.delta_f_net_hz for r in records]
    fit = solve_k_empirical(doses, deltas)
    return records, fit
