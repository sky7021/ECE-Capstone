"""Host PC Finite State Machine (master orchestrator control plane).

This is a pure-Python, GUI-agnostic implementation of the hierarchical Host
FSM from spec §3.I. It contains *no* Qt or pyserial code: every side effect
(opening ports, purging buffers, writing CSVs, showing a modal ...) is
delegated to an injected :class:`HostActions` implementation, which keeps the
state machine deterministic and fully unit-testable.

State hierarchy::

    S0: DISCONNECTED / BOOT
    HS_CONNECTED (OR-superstate, with History pseudostate H)
    |-- S1: IDLE / STANDBY
    |-- S2: TARE_AND_PRIME (AND-superstate)
    |   |-- S2.2 R_QCM_TARE : S2.A1 WAIT_QCM_FIRST  -> S2.A2 QCM_TARE_READY
    |   `-- S2.1 R_AUX_PRIME: S2.B1 WAIT_AUX_FIRST  -> S2.B2 AUX_ZOH_PRIMED
    `-- S3: ACTIVE_STREAMING & ZOH_FUSION
    S4: FAULT
    S5: PWM FAN (transient; returns to H)

Transition table (spec §3.I) -> event method:

=====================  ==================================  ==========================
Source                 Event                               Method
=====================  ==================================  ==========================
S0                     program boot [ports available]      :meth:`boot`
HS_CONNECTED           fan slider [0 <= duty <= 100]       :meth:`fan_pwm_changed`
S1                     Start Run [ports healthy]           :meth:`start_run`
S2.A1                  valid QCM line                      :meth:`qcm_tare_ready`
S2.B1                  valid AUX line & health OK          :meth:`aux_zoh_primed`
S2                     join [in(S2.A2) && in(S2.B2)]       :meth:`join_completed`
HS_CONNECTED           SerialException / timeouts          :meth:`fault`
S4                     user confirms modal                 :meth:`fault_confirmed`
HS_CONNECTED           application exit                    :meth:`shutdown`
=====================  ==================================  ==========================

The S3 self-loops (valid QCM -> compute & log, valid AUX -> refresh ZOH) are
data-plane events handled by :class:`software.pipeline.zoh_fusion.ZOHFusionEngine`
and do not change the control state.

Run-to-completion semantics
---------------------------
Events raised *while* a transition is executing (e.g. an action that
synchronously triggers a fault, or a modal dialog's nested event loop
delivering the user's confirmation) are queued and processed in order once
the current transition has completed. Such deferred events return ``True``
("accepted for processing").
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Optional, Protocol

from software.pipeline.quality import is_valid_pwm_duty

log = logging.getLogger(__name__)


class HostState(Enum):
    S0_DISCONNECTED = "S0: DISCONNECTED / BOOT"
    S1_IDLE_STANDBY = "S1: IDLE / STANDBY"
    S2_TARE_AND_PRIME = "S2: TARE_AND_PRIME"
    S3_ACTIVE_STREAMING = "S3: ACTIVE_STREAMING & ZOH_FUSION"
    S4_FAULT = "S4: FAULT"
    S5_PWM_FAN = "S5: PWM FAN"

    @property
    def label(self) -> str:
        return self.value

    @property
    def short(self) -> str:
        return self.name.split("_", 1)[0]  # "S0".."S5"


HS_CONNECTED_STATES: frozenset[HostState] = frozenset(
    {HostState.S1_IDLE_STANDBY, HostState.S2_TARE_AND_PRIME, HostState.S3_ACTIVE_STREAMING}
)
"""Substates of the ``HS_CONNECTED: SERIAL_CONNECTED`` OR-superstate."""


class QCMTareRegion(Enum):
    """Orthogonal region ``S2.2: R_QCM_TARE``."""

    A1_WAIT_QCM_FIRST = "S2.A1: WAIT_QCM_FIRST"
    A2_QCM_TARE_READY = "S2.A2: QCM_TARE_READY"


class AuxPrimeRegion(Enum):
    """Orthogonal region ``S2.1: R_AUX_PRIME``."""

    B1_WAIT_AUX_FIRST = "S2.B1: WAIT_AUX_FIRST"
    B2_AUX_ZOH_PRIMED = "S2.B2: AUX_ZOH_PRIMED"


@dataclass(frozen=True)
class Transition:
    """One recorded transition. ``source == target`` marks an S2 region (internal) step."""

    source: HostState
    target: HostState
    event: str
    detail: str = ""

    @property
    def is_internal(self) -> bool:
        return self.source is self.target

    def __str__(self) -> str:
        extra = f" ({self.detail})" if self.detail else ""
        return f"{self.source.short} -> {self.target.short} on {self.event}{extra}"


class HostActions(Protocol):
    """Side effects and hardware guards the FSM delegates to the controller."""

    # --- guards -----------------------------------------------------------
    def ports_available(self) -> bool:
        """S0 boot guard: both COM ports configured/present (or ``--mock``)."""
        ...

    def ports_healthy(self) -> bool:
        """S1 Start Run guard: both reader workers running with open ports."""
        ...

    # --- actions ----------------------------------------------------------
    def open_hardware(self) -> bool:
        """S0 -> S1: open pyserial ports and spawn QThread workers. ``False`` = failed."""
        ...

    def begin_session(self) -> None:
        """S1 -> S2: ``reset_input_buffer()`` on both ports, open session CSV, arm priming."""
        ...

    def begin_streaming(self, f0_hz: int, t0_c: float) -> None:
        """S2 -> S3: baseline frozen; begin real-time fusion and CSV logging."""
        ...

    def transmit_pwm(self, duty: int) -> None:
        """S5 entry: transmit ``SET_PWM:<duty>\\n`` on the ESP32 uplink."""
        ...

    def park_hardware(self, reason: str) -> None:
        """HS_CONNECTED -> S4 group preemptive exit: invalidate ZOH, halt math,
        flush/close CSV, stop workers, close ports."""
        ...

    def show_fault(self, reason: str) -> None:
        """S4 entry: raise the modal GUI fault alert."""
        ...

    def clear_fault(self) -> None:
        """S4 -> S0: clear the fault alert and reset GUI to disconnected."""
        ...

    def terminate(self) -> None:
        """HS_CONNECTED -> S0 direct termination (must be idempotent)."""
        ...


class NullActions:
    """No-op :class:`HostActions` with permissive guards (tests / dry runs)."""

    def ports_available(self) -> bool:
        return True

    def ports_healthy(self) -> bool:
        return True

    def open_hardware(self) -> bool:
        return True

    def begin_session(self) -> None:
        pass

    def begin_streaming(self, f0_hz: int, t0_c: float) -> None:
        pass

    def transmit_pwm(self, duty: int) -> None:
        pass

    def park_hardware(self, reason: str) -> None:
        pass

    def show_fault(self, reason: str) -> None:
        pass

    def clear_fault(self) -> None:
        pass

    def terminate(self) -> None:
        pass


TransitionListener = Callable[[Transition], None]


@dataclass
class _QueuedEvent:
    name: str
    handler: Callable[..., bool]
    args: tuple[Any, ...] = field(default_factory=tuple)


class HostStateMachine:
    """Hierarchical Host PC FSM with AND-superstate S2 and History for S5."""

    def __init__(self, actions: Optional[HostActions] = None) -> None:
        self._actions: HostActions = actions if actions is not None else NullActions()
        self._state = HostState.S0_DISCONNECTED
        self._history: Optional[HostState] = None
        self._qcm_region: Optional[QCMTareRegion] = None
        self._aux_region: Optional[AuxPrimeRegion] = None
        self.fault_reason: Optional[str] = None
        self.last_rejection: Optional[str] = None
        self.transitions: list[Transition] = []
        self._listeners: list[TransitionListener] = []
        self._queue: Deque[_QueuedEvent] = deque()
        self._busy = False

    # ================================================================ queries
    @property
    def state(self) -> HostState:
        return self._state

    @property
    def history_state(self) -> Optional[HostState]:
        """Last substate of HS_CONNECTED recorded by the History pseudostate ``H``."""
        return self._history

    @property
    def is_connected(self) -> bool:
        """True while inside the ``HS_CONNECTED`` superstate (incl. transient S5)."""
        return self._state in HS_CONNECTED_STATES or self._state is HostState.S5_PWM_FAN

    @property
    def qcm_region(self) -> Optional[QCMTareRegion]:
        return self._qcm_region

    @property
    def aux_region(self) -> Optional[AuxPrimeRegion]:
        return self._aux_region

    @property
    def in_s2_a2(self) -> bool:
        return self._qcm_region is QCMTareRegion.A2_QCM_TARE_READY

    @property
    def in_s2_b2(self) -> bool:
        return self._aux_region is AuxPrimeRegion.B2_AUX_ZOH_PRIMED

    @property
    def active_configuration(self) -> str:
        """Human-readable active state configuration, e.g. for a status badge."""
        if self._state is HostState.S2_TARE_AND_PRIME:
            q = self._qcm_region.value if self._qcm_region else "?"
            a = self._aux_region.value if self._aux_region else "?"
            return f"HS_CONNECTED / {self._state.label} [{q} || {a}]"
        if self.is_connected:
            return f"HS_CONNECTED / {self._state.label}"
        return self._state.label

    def state_path(self) -> list[HostState]:
        """Sequence of top-level states visited (internal S2 region steps omitted)."""
        path = [self.transitions[0].source] if self.transitions else [self._state]
        path.extend(t.target for t in self.transitions if not t.is_internal)
        return path

    def add_listener(self, listener: TransitionListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: TransitionListener) -> None:
        self._listeners.remove(listener)

    # ================================================================= events
    def boot(self) -> bool:
        """``S0 -> HS_CONNECTED (S1)`` on program boot ``[ports available]``."""
        return self._dispatch("boot", self._on_boot)

    def start_run(self) -> bool:
        """``S1 -> S2 (S2.A1 || S2.B1)`` on "Start Run" ``[ports healthy]``."""
        return self._dispatch("start_run", self._on_start_run)

    def qcm_tare_ready(self) -> bool:
        """``S2.A1 -> S2.A2`` on first valid QCM line."""
        return self._dispatch("qcm_tare_ready", self._on_qcm_tare_ready)

    def aux_zoh_primed(self) -> bool:
        """``S2.B1 -> S2.B2`` on first valid, healthy AUX line."""
        return self._dispatch("aux_zoh_primed", self._on_aux_zoh_primed)

    def join_completed(self, f0_hz: int, t0_c: float) -> bool:
        """``S2 -> S3`` on join completion ``[in(S2.A2) && in(S2.B2)]``."""
        return self._dispatch("join_completed", self._on_join_completed, f0_hz, t0_c)

    def fan_pwm_changed(self, duty: int) -> bool:
        """``HS_CONNECTED -> S5 -> H`` on fan slider change ``[0 <= duty <= 100]``."""
        return self._dispatch("fan_pwm_changed", self._on_fan_pwm_changed, duty)

    def fault(self, reason: str) -> bool:
        """``HS_CONNECTED -> S4`` group preemptive exit (serial error / staleness)."""
        return self._dispatch("fault", self._on_fault, reason)

    def fault_confirmed(self) -> bool:
        """``S4 -> S0`` when the user confirms the modal fault alert."""
        return self._dispatch("fault_confirmed", self._on_fault_confirmed)

    def shutdown(self) -> bool:
        """``HS_CONNECTED/S4 -> S0`` direct termination on application exit."""
        return self._dispatch("shutdown", self._on_shutdown)

    # =============================================================== handlers
    def _on_boot(self) -> bool:
        if self._state is not HostState.S0_DISCONNECTED:
            return self._reject("boot", "not in S0")
        if not self._guard(self._actions.ports_available):
            return self._reject("boot", "COM ports unavailable")
        if not self._guard(self._actions.open_hardware):
            return self._reject("boot", "failed to open serial ports / spawn workers")
        self._history = None
        self._enter(HostState.S1_IDLE_STANDBY, "boot")
        return True

    def _on_start_run(self) -> bool:
        if self._state is not HostState.S1_IDLE_STANDBY:
            return self._reject("start_run", "not in S1")
        if not self._guard(self._actions.ports_healthy):
            return self._reject("start_run", "ports not healthy")
        self._run_action("begin_session", self._actions.begin_session)
        # Default entry into both orthogonal regions of the AND-superstate.
        self._qcm_region = QCMTareRegion.A1_WAIT_QCM_FIRST
        self._aux_region = AuxPrimeRegion.B1_WAIT_AUX_FIRST
        self._enter(HostState.S2_TARE_AND_PRIME, "start_run")
        return True

    def _on_qcm_tare_ready(self) -> bool:
        if self._state is not HostState.S2_TARE_AND_PRIME:
            return self._reject("qcm_tare_ready", "not in S2")
        if self._qcm_region is not QCMTareRegion.A1_WAIT_QCM_FIRST:
            return self._reject("qcm_tare_ready", "S2.2 region already in S2.A2")
        self._qcm_region = QCMTareRegion.A2_QCM_TARE_READY
        self._record(self._state, self._state, "qcm_tare_ready", "S2.A1 -> S2.A2")
        return True

    def _on_aux_zoh_primed(self) -> bool:
        if self._state is not HostState.S2_TARE_AND_PRIME:
            return self._reject("aux_zoh_primed", "not in S2")
        if self._aux_region is not AuxPrimeRegion.B1_WAIT_AUX_FIRST:
            return self._reject("aux_zoh_primed", "S2.1 region already in S2.B2")
        self._aux_region = AuxPrimeRegion.B2_AUX_ZOH_PRIMED
        self._record(self._state, self._state, "aux_zoh_primed", "S2.B1 -> S2.B2")
        return True

    def _on_join_completed(self, f0_hz: int, t0_c: float) -> bool:
        if self._state is not HostState.S2_TARE_AND_PRIME:
            return self._reject("join_completed", "not in S2")
        if not (self.in_s2_a2 and self.in_s2_b2):
            return self._reject("join_completed", "join guard in(S2.A2) && in(S2.B2) is false")
        self._run_action("begin_streaming", self._actions.begin_streaming, f0_hz, t0_c)
        self._qcm_region = None
        self._aux_region = None
        self._enter(
            HostState.S3_ACTIVE_STREAMING,
            "join_completed",
            f"f0={f0_hz} Hz, T0={t0_c:.4f} C",
        )
        return True

    def _on_fan_pwm_changed(self, duty: int) -> bool:
        if self._state not in HS_CONNECTED_STATES:
            return self._reject("fan_pwm_changed", f"not in HS_CONNECTED ({self._state.short})")
        if not is_valid_pwm_duty(duty):
            return self._reject("fan_pwm_changed", f"duty {duty!r} outside [0, 100]")
        # Record the History pseudostate. S2 region substates are deliberately
        # left untouched so returning via H restores the full configuration.
        self._history = self._state
        self._enter(HostState.S5_PWM_FAN, "fan_pwm_changed", f"duty={duty}")
        self._run_action("transmit_pwm", self._actions.transmit_pwm, duty)
        self._enter(self._history, "history_return", "H")
        return True

    def _on_fault(self, reason: str) -> bool:
        if not self.is_connected:
            # Already parked (S4) or never connected (S0): nothing to preempt.
            return self._reject("fault", f"ignored in {self._state.short}: {reason}")
        self.fault_reason = reason
        # Group preemptive exit actions are best-effort: a failure while
        # parking must never prevent reaching S4.
        self._run_action("park_hardware", self._actions.park_hardware, reason, escalate=False)
        self._qcm_region = None
        self._aux_region = None
        self._enter(HostState.S4_FAULT, "fault", reason)
        self._run_action("show_fault", self._actions.show_fault, reason, escalate=False)
        return True

    def _on_fault_confirmed(self) -> bool:
        if self._state is not HostState.S4_FAULT:
            return self._reject("fault_confirmed", "not in S4")
        self._run_action("clear_fault", self._actions.clear_fault, escalate=False)
        self.fault_reason = None
        self._history = None
        self._enter(HostState.S0_DISCONNECTED, "fault_confirmed")
        return True

    def _on_shutdown(self) -> bool:
        # terminate() is idempotent, so it is always invoked to guarantee that
        # CSVs are flushed and ports are released no matter where we are.
        self._run_action("terminate", self._actions.terminate, escalate=False)
        if self._state is HostState.S0_DISCONNECTED:
            return True
        self._qcm_region = None
        self._aux_region = None
        self._history = None
        self._enter(HostState.S0_DISCONNECTED, "shutdown")
        return True

    # ============================================================== machinery
    def _dispatch(self, name: str, handler: Callable[..., bool], *args: Any) -> bool:
        if self._busy:
            log.debug("Deferring %s until current transition completes", name)
            self._queue.append(_QueuedEvent(name, handler, args))
            return True
        self._busy = True
        try:
            result = handler(*args)
            while self._queue:
                ev = self._queue.popleft()
                ev.handler(*ev.args)
            return result
        finally:
            self._busy = False

    def _enter(self, target: HostState, event: str, detail: str = "") -> None:
        source = self._state
        self._state = target
        self._record(source, target, event, detail)

    def _record(self, source: HostState, target: HostState, event: str, detail: str) -> None:
        t = Transition(source, target, event, detail)
        self.transitions.append(t)
        log.info("Host FSM: %s", t)
        for listener in list(self._listeners):
            try:
                listener(t)
            except Exception:  # listeners must never break the FSM
                log.exception("Host FSM listener failed on %s", t)

    def _reject(self, event: str, why: str) -> bool:
        self.last_rejection = f"{event}: {why}"
        log.debug("Host FSM rejected %s", self.last_rejection)
        return False

    def _guard(self, fn: Callable[[], bool]) -> bool:
        try:
            return bool(fn())
        except Exception:
            log.exception("Host FSM guard %s raised", getattr(fn, "__name__", fn))
            return False

    def _run_action(
        self, name: str, fn: Callable[..., Any], *args: Any, escalate: bool = True
    ) -> None:
        """Execute an action; failures inside HS_CONNECTED escalate to ``S4: FAULT``."""
        try:
            fn(*args)
        except Exception as exc:
            log.exception("Host FSM action %s failed", name)
            if escalate:
                # Queued (run-to-completion): processed after the current transition.
                self._queue.append(
                    _QueuedEvent("fault", self._on_fault, (f"{name} failed: {exc}",))
                )
