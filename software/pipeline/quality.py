"""Wire-level validators, sensor health checks and ZOH age evaluators.

This module is pure Python (no Qt / pyserial) so that every guard condition
used by the Host PC FSM can be unit tested in isolation.

ICD references (``capstone_agentic_architecture_specification.md`` §2):

* Link A (FPGA -> PC):  ``QCM_FREQ=0005000123,TEMP_X16=000400\\r\\n``
* Link B (ESP32 -> PC): ``FLOW=1.250,TEMP=24.50,RH=42.10,PWM=50,FRESH=1,OK=1\\r\\n``
* Link B (PC -> ESP32): ``SET_PWM:<duty>\\n`` with ``0 <= duty <= 100``
"""

from __future__ import annotations

import math
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Timing invariants (seconds)
# ---------------------------------------------------------------------------
QCM_TIMEOUT_S: float = 3.0
"""No valid QCM line for longer than this triggers ``S4: FAULT``."""

FLOW_TIMEOUT_S: float = 3.0
"""No valid AUX (flow) line / flow ZOH age longer than this triggers ``S4: FAULT``."""

SHT_TIMEOUT_S: float = 6.0
"""No fresh SHT21 (``FRESH=1``) line / SHT ZOH age longer than this triggers ``S4: FAULT``."""

PWM_MIN: int = 0
PWM_MAX: int = 100

# ---------------------------------------------------------------------------
# Regex guards (exactly as specified in the ICD)
# ---------------------------------------------------------------------------
QCM_PATTERN = re.compile(rb"QCM_FREQ=(\d+),TEMP_X16=(-?\d+)")
AUX_PATTERN = re.compile(
    rb"FLOW=([\d.]+),TEMP=(-?[\d.]+),RH=([\d.]+),PWM=(\d+),FRESH=([01]),OK=([01])"
)

AUX_KEYS = ("flow_lpm", "sht_temp_c", "sht_rh_pct", "pwm_duty", "fresh_flag", "health_ok")


def parse_qcm_line(line: bytes) -> Optional[tuple[int, float]]:
    """Parse a Link A ASCII record.

    Returns ``(qcm_freq_hz, qcm_temp_c)`` or ``None`` if the line is malformed.
    ``TEMP_X16`` is the MCP9808 reading in 1/16 degC (e.g. ``-000080`` -> -5.0 degC).
    """
    if not isinstance(line, (bytes, bytearray)):
        return None
    m = QCM_PATTERN.search(line)
    if m is None:
        return None
    try:
        freq_hz = int(m.group(1))
        temp_c = int(m.group(2)) / 16.0
    except ValueError:  # pragma: no cover - regex already guarantees digits
        return None
    return freq_hz, temp_c


def parse_aux_line(line: bytes) -> Optional[dict]:
    """Parse a Link B downlink ASCII record.

    Returns a dict with keys :data:`AUX_KEYS` or ``None`` if the line is
    malformed (regex miss, unparsable float such as ``1.2.3``, non-finite
    value, or a PWM duty outside ``0..100``).
    """
    if not isinstance(line, (bytes, bytearray)):
        return None
    m = AUX_PATTERN.search(line)
    if m is None:
        return None
    try:
        flow_lpm = float(m.group(1))
        sht_temp_c = float(m.group(2))
        sht_rh_pct = float(m.group(3))
        pwm_duty = int(m.group(4))
    except ValueError:
        return None
    if not all(math.isfinite(v) for v in (flow_lpm, sht_temp_c, sht_rh_pct)):
        return None
    if not is_valid_pwm_duty(pwm_duty):
        return None
    return {
        "flow_lpm": flow_lpm,
        "sht_temp_c": sht_temp_c,
        "sht_rh_pct": sht_rh_pct,
        "pwm_duty": pwm_duty,
        "fresh_flag": m.group(5) == b"1",
        "health_ok": m.group(6) == b"1",
    }


def is_aux_health_ok(aux: Optional[dict]) -> bool:
    """``S2.B1 -> S2.B2`` guard: regex matched *and* ESP32 health flag nominal."""
    return bool(aux) and bool(aux.get("health_ok", False))


def is_valid_pwm_duty(duty: object) -> bool:
    """``S5: PWM FAN`` guard: integer duty with ``0 <= duty <= 100``."""
    return isinstance(duty, int) and not isinstance(duty, bool) and PWM_MIN <= duty <= PWM_MAX


def format_pwm_command(duty: int) -> bytes:
    """Encode the Link B uplink command ``SET_PWM:<duty>\\n``."""
    if not is_valid_pwm_duty(duty):
        raise ValueError(f"PWM duty must be an int in [{PWM_MIN}, {PWM_MAX}], got {duty!r}")
    return f"SET_PWM:{duty}\n".encode("ascii")


# ---------------------------------------------------------------------------
# ZOH age evaluators
# ---------------------------------------------------------------------------
def zoh_age(now: float, last_ts: float) -> float:
    """Age of a ZOH-held value.

    Timestamps originate from two different reader threads, so a QCM sample
    can be stamped a hair *before* the most recent AUX sample; negative ages
    are clamped to zero.
    """
    return max(0.0, now - last_ts)


def is_flow_fresh(age_s: float, limit_s: float = FLOW_TIMEOUT_S) -> bool:
    return age_s <= limit_s


def is_sht_fresh(age_s: float, limit_s: float = SHT_TIMEOUT_S) -> bool:
    return age_s <= limit_s

