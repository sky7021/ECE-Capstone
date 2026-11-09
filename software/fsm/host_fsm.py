"""Host PC Finite State Machine (master orchestrator control plane).

Declarative UML StateChart using ``python-statemachine`` (v3.2.2).
Zero adapter tax: native StateChart transitions, guards, SCXML RTC queuing,
and Deep History.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from statemachine import HistoryState, HistoryType, State, StateChart
from statemachine.exceptions import TransitionNotAllowed

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
        return self.name.split("_", 1)[0]


HS_CONNECTED_STATES: frozenset[HostState] = frozenset(
    {HostState.S1_IDLE_STANDBY, HostState.S2_TARE_AND_PRIME, HostState.S3_ACTIVE_STREAMING}
)


class QCMTareRegion(Enum):
    A1_WAIT_QCM_FIRST = "S2.A1: WAIT_QCM_FIRST"
    A2_QCM_TARE_READY = "S2.A2: QCM_TARE_READY"


class AuxPrimeRegion(Enum):
    B1_WAIT_AUX_FIRST = "S2.B1: WAIT_AUX_FIRST"
    B2_AUX_ZOH_PRIMED = "S2.B2: AUX_ZOH_PRIMED"


class HostActions(Protocol):
    """Side effects and hardware guards the FSM delegates to the controller."""

    def ports_available(self) -> bool: ...
    def ports_healthy(self) -> bool: ...
    def open_hardware(self) -> bool: ...
    def begin_session(self) -> None: ...
    def begin_streaming(self, f0_hz: int, t0_c: float) -> None: ...
    def transmit_pwm(self, duty: int) -> None: ...
    def park_hardware(self, reason: str) -> None: ...
    def show_fault(self, reason: str) -> None: ...
    def clear_fault(self) -> None: ...
    def terminate(self) -> None: ...


class NullActions:
    """No-op HostActions with permissive guards (tests / dry runs)."""

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


class HostStateMachine(StateChart):
    """Declarative Host PC UML StateChart (v3.2.2)."""

    allow_event_without_transition = False

    s0_disconnected = State(HostState.S0_DISCONNECTED, initial=True, value=HostState.S0_DISCONNECTED)

    class hs_connected(State.Compound):
        h = HistoryState(type=HistoryType.DEEP)
        s1_idle = State(HostState.S1_IDLE_STANDBY, initial=True, value=HostState.S1_IDLE_STANDBY)

        class s2_tare_prime(State.Parallel, value=HostState.S2_TARE_AND_PRIME):
            class r_aux(State.Compound):
                b1 = State(AuxPrimeRegion.B1_WAIT_AUX_FIRST, initial=True, value=AuxPrimeRegion.B1_WAIT_AUX_FIRST)
                b2 = State(AuxPrimeRegion.B2_AUX_ZOH_PRIMED, value=AuxPrimeRegion.B2_AUX_ZOH_PRIMED)
                aux_zoh_primed = b1.to(b2)

            class r_qcm(State.Compound):
                a1 = State(QCMTareRegion.A1_WAIT_QCM_FIRST, initial=True, value=QCMTareRegion.A1_WAIT_QCM_FIRST)
                a2 = State(QCMTareRegion.A2_QCM_TARE_READY, value=QCMTareRegion.A2_QCM_TARE_READY)
                qcm_tare_ready = a1.to(a2)

        s3_active = State(HostState.S3_ACTIVE_STREAMING, value=HostState.S3_ACTIVE_STREAMING)

    s4_fault = State(HostState.S4_FAULT, value=HostState.S4_FAULT)
    s5_pwm_fan = State(HostState.S5_PWM_FAN, value=HostState.S5_PWM_FAN)

    # Declarative transitions using public event names
    boot = s0_disconnected.to(
        hs_connected.s1_idle, cond=["guard_ports_available", "guard_open_hardware"]
    )
    start_run = hs_connected.s1_idle.to(
        hs_connected.s2_tare_prime, cond="guard_ports_healthy", on="do_begin_session"
    )
    join_completed = hs_connected.s2_tare_prime.to(
        hs_connected.s3_active, cond=["in_s2_a2", "in_s2_b2"], on="do_begin_streaming"
    )
    fan_pwm_changed = hs_connected.to(
        s5_pwm_fan, cond="guard_valid_duty", before="do_store_history", on="do_transmit_pwm"
    )
    s5_pwm_fan.to(hs_connected.h, cond="can_return_from_s5")
    fault = s4_fault.from_(
        hs_connected, s5_pwm_fan, before="do_park_hardware", on="do_show_fault"
    )
    fault_confirmed = s4_fault.to(s0_disconnected, before="do_clear_fault")
    shutdown = (
        s0_disconnected.from_(hs_connected, s5_pwm_fan, s4_fault, before="do_terminate")
        | s0_disconnected.to.itself(internal=True, on="do_terminate")
    )

    def __init__(self, actions: Optional[HostActions] = None) -> None:
        self._actions: HostActions = actions if actions is not None else NullActions()
        self._history: Optional[HostState] = None
        self.fault_reason: Optional[str] = None
        self._state_path: list[HostState] = [HostState.S0_DISCONNECTED]
        super().__init__()

    # ================================================================ queries
    @property
    def state(self) -> HostState:
        return next(
            (v for v in self.configuration_values if isinstance(v, HostState)),
            HostState.S0_DISCONNECTED,
        )

    @property
    def history_state(self) -> Optional[HostState]:
        return self._history

    @property
    def is_connected(self) -> bool:
        return self.hs_connected in self.configuration or self.s5_pwm_fan in self.configuration

    @property
    def in_s2_a2(self) -> bool:
        return self.hs_connected.s2_tare_prime.r_qcm.a2 in self.configuration

    @property
    def in_s2_b2(self) -> bool:
        return self.hs_connected.s2_tare_prime.r_aux.b2 in self.configuration

    @property
    def qcm_region(self) -> Optional[QCMTareRegion]:
        if self.hs_connected.s2_tare_prime not in self.configuration:
            return None
        return QCMTareRegion.A2_QCM_TARE_READY if self.in_s2_a2 else QCMTareRegion.A1_WAIT_QCM_FIRST

    @property
    def aux_region(self) -> Optional[AuxPrimeRegion]:
        if self.hs_connected.s2_tare_prime not in self.configuration:
            return None
        return AuxPrimeRegion.B2_AUX_ZOH_PRIMED if self.in_s2_b2 else AuxPrimeRegion.B1_WAIT_AUX_FIRST

    @property
    def active_configuration(self) -> str:
        if self.state is HostState.S2_TARE_AND_PRIME:
            q = self.qcm_region.value if self.qcm_region else "?"
            a = self.aux_region.value if self.aux_region else "?"
            return f"HS_CONNECTED / {self.state.label} [{q} || {a}]"
        if self.is_connected:
            return f"HS_CONNECTED / {self.state.label}"
        return self.state.label

    def state_path(self) -> list[HostState]:
        return list(self._state_path)

    # ================================================================= guards
    def _guard(self, fn: Callable[[], bool]) -> bool:
        try:
            return bool(fn())
        except Exception:
            log.exception("Host FSM guard %s raised", getattr(fn, "__name__", fn))
            return False

    def _run(self, name: str, fn: Callable[..., Any], *args: Any, escalate: bool = True) -> None:
        try:
            fn(*args)
        except Exception as exc:
            log.exception("Host FSM %s failed", name)
            if escalate:
                self.fault(reason=f"{name} failed: {exc}")

    def guard_ports_available(self) -> bool:
        return self._guard(self._actions.ports_available)

    def guard_open_hardware(self) -> bool:
        return self._guard(self._actions.open_hardware)

    def guard_ports_healthy(self) -> bool:
        return self._guard(self._actions.ports_healthy)

    def guard_valid_duty(self, duty: int = 0) -> bool:
        return is_valid_pwm_duty(duty)

    def can_return_from_s5(self) -> bool:
        return self.fault_reason is None

    # ================================================================ actions
    def do_store_history(self) -> None:
        self._history = self.state

    def do_begin_session(self) -> None:
        self._run("begin_session", self._actions.begin_session)

    def do_begin_streaming(self, f0_hz: int = 0, t0_c: float = 0.0) -> None:
        self._run("begin_streaming", self._actions.begin_streaming, f0_hz, t0_c)

    def do_transmit_pwm(self, duty: int = 0) -> None:
        self._run("transmit_pwm", self._actions.transmit_pwm, duty)

    def do_park_hardware(self, reason: str = "") -> None:
        self.fault_reason = reason or self.fault_reason
        self._run("park_hardware", self._actions.park_hardware, self.fault_reason or "", escalate=False)

    def do_show_fault(self, reason: str = "") -> None:
        self._run("show_fault", self._actions.show_fault, reason or self.fault_reason or "", escalate=False)

    def do_clear_fault(self) -> None:
        self._run("clear_fault", self._actions.clear_fault, escalate=False)
        self.fault_reason = None
        self._history = None

    def do_terminate(self) -> None:
        self._run("terminate", self._actions.terminate, escalate=False)
        self.fault_reason = None
        self._history = None

    def after_transition(self) -> None:
        if self._state_path[-1] is not self.state:
            self._state_path.append(self.state)
