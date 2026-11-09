"""Pure-Python Host FSM tests (no Qt, no serial): transitions, guards, History, RTC."""

from __future__ import annotations

import pytest
from statemachine.exceptions import TransitionNotAllowed

from software.fsm.host_fsm import (
    AuxPrimeRegion,
    HostState,
    HostStateMachine,
    NullActions,
    QCMTareRegion,
)

S0, S1, S2, S3, S4, S5 = (
    HostState.S0_DISCONNECTED,
    HostState.S1_IDLE_STANDBY,
    HostState.S2_TARE_AND_PRIME,
    HostState.S3_ACTIVE_STREAMING,
    HostState.S4_FAULT,
    HostState.S5_PWM_FAN,
)


class RecordingActions(NullActions):
    def __init__(self, **overrides) -> None:
        self.calls: list[tuple] = []
        self.available = overrides.get("available", True)
        self.opens = overrides.get("opens", True)
        self.healthy = overrides.get("healthy", True)
        self.fail = set(overrides.get("fail", ()))
        self.on_show_fault = overrides.get("on_show_fault")

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise RuntimeError(f"{name} boom")

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def ports_available(self):
        return self.available

    def ports_healthy(self):
        return self.healthy

    def open_hardware(self):
        self._call("open_hardware")
        return self.opens

    def begin_session(self):
        self._call("begin_session")

    def begin_streaming(self, f0_hz, t0_c):
        self._call("begin_streaming", f0_hz, t0_c)

    def transmit_pwm(self, duty):
        self._call("transmit_pwm", duty)

    def park_hardware(self, reason):
        self._call("park_hardware", reason)

    def show_fault(self, reason):
        self._call("show_fault", reason)
        if self.on_show_fault:
            self.on_show_fault()

    def clear_fault(self):
        self._call("clear_fault")

    def terminate(self):
        self._call("terminate")


def to_s2(fsm: HostStateMachine) -> None:
    fsm.boot()
    fsm.start_run()


def to_s3(fsm: HostStateMachine) -> None:
    to_s2(fsm)
    fsm.qcm_tare_ready()
    fsm.aux_zoh_primed()
    fsm.join_completed(5_000_000, 25.0)


@pytest.fixture
def acts() -> RecordingActions:
    return RecordingActions()


@pytest.fixture
def fsm(acts) -> HostStateMachine:
    return HostStateMachine(acts)


class TestBoot:
    def test_initial_state(self, fsm):
        assert fsm.state is S0 and not fsm.is_connected

    def test_boot_to_idle(self, fsm, acts):
        fsm.boot()
        assert fsm.state is S1 and fsm.is_connected
        assert acts.names() == ["open_hardware"]

    def test_boot_guard_ports_unavailable(self):
        a = RecordingActions(available=False)
        fsm = HostStateMachine(a)
        with pytest.raises(TransitionNotAllowed):
            fsm.boot()
        assert fsm.state is S0 and a.names() == []

    def test_boot_open_failure_stays_in_s0(self):
        fsm = HostStateMachine(RecordingActions(opens=False))
        with pytest.raises(TransitionNotAllowed):
            fsm.boot()
        assert fsm.state is S0

    def test_boot_only_from_s0(self, fsm):
        fsm.boot()
        with pytest.raises(TransitionNotAllowed):
            fsm.boot()
        assert fsm.state is S1


class TestTareAndPrime:
    def test_start_run_enters_both_regions(self, fsm, acts):
        to_s2(fsm)
        assert fsm.state is S2
        assert fsm.qcm_region is QCMTareRegion.A1_WAIT_QCM_FIRST
        assert fsm.aux_region is AuxPrimeRegion.B1_WAIT_AUX_FIRST
        assert acts.names() == ["open_hardware", "begin_session"]

    def test_start_run_requires_s1_and_healthy_ports(self):
        fsm = HostStateMachine(RecordingActions(healthy=False))
        with pytest.raises(TransitionNotAllowed):
            fsm.start_run()  # S0
        fsm.boot()
        with pytest.raises(TransitionNotAllowed):
            fsm.start_run()  # S1 with healthy=False
        assert fsm.state is S1

    def test_join_guard_blocks_until_both_regions(self, fsm):
        to_s2(fsm)
        with pytest.raises(TransitionNotAllowed):
            fsm.join_completed(5_000_000, 25.0)
        fsm.qcm_tare_ready()
        with pytest.raises(TransitionNotAllowed):
            fsm.join_completed(5_000_000, 25.0)
        assert fsm.state is S2
        fsm.aux_zoh_primed()
        fsm.join_completed(5_000_000, 25.0)
        assert fsm.state is S3
        assert fsm.qcm_region is None and fsm.aux_region is None

    def test_region_events_are_order_independent_and_latch_once(self, fsm):
        to_s2(fsm)
        fsm.aux_zoh_primed()
        with pytest.raises(TransitionNotAllowed):
            fsm.aux_zoh_primed()  # already in S2.B2
        fsm.qcm_tare_ready()
        assert fsm.in_s2_a2 and fsm.in_s2_b2

    def test_region_events_rejected_outside_s2(self, fsm):
        fsm.boot()
        with pytest.raises(TransitionNotAllowed):
            fsm.qcm_tare_ready()
        with pytest.raises(TransitionNotAllowed):
            fsm.aux_zoh_primed()

    def test_active_configuration_string(self, fsm):
        to_s2(fsm)
        fsm.qcm_tare_ready()
        cfg = fsm.active_configuration
        assert "S2.A2" in cfg and "S2.B1" in cfg and cfg.startswith("HS_CONNECTED")


class TestPWMHistory:
    @pytest.mark.parametrize("setup,expected", [("s1", S1), ("s2", S2), ("s3", S3)])
    def test_returns_to_history_state(self, fsm, acts, setup, expected):
        {"s1": fsm.boot, "s2": lambda: to_s2(fsm), "s3": lambda: to_s3(fsm)}[setup]()
        fsm.fan_pwm_changed(75)
        assert fsm.state is expected and fsm.history_state is expected
        assert ("transmit_pwm", 75) in acts.calls
        assert fsm.state_path()[-2:] == [S5, expected]

    def test_history_preserves_s2_regions(self, fsm):
        to_s2(fsm)
        fsm.qcm_tare_ready()
        fsm.fan_pwm_changed(10)
        assert fsm.state is S2 and fsm.in_s2_a2 and not fsm.in_s2_b2

    @pytest.mark.parametrize("duty", [-1, 101, 3.5, True])
    def test_invalid_duty_rejected(self, fsm, acts, duty):
        fsm.boot()
        with pytest.raises(TransitionNotAllowed):
            fsm.fan_pwm_changed(duty)
        assert "transmit_pwm" not in acts.names() and fsm.state is S1

    def test_rejected_outside_hs_connected(self, fsm, acts):
        with pytest.raises(TransitionNotAllowed):
            fsm.fan_pwm_changed(50)  # S0
        to_s3(fsm)
        fsm.fault("x")
        with pytest.raises(TransitionNotAllowed):
            fsm.fan_pwm_changed(50)  # S4
        assert "transmit_pwm" not in acts.names()

    def test_transmit_failure_escalates_to_fault(self):
        a = RecordingActions(fail={"transmit_pwm"})
        fsm = HostStateMachine(a)
        to_s3(fsm)
        fsm.fan_pwm_changed(40)
        assert fsm.state is S4 and "transmit_pwm failed" in fsm.fault_reason


class TestFault:
    @pytest.mark.parametrize("setup", ["s1", "s2", "s3"])
    def test_group_preemptive_exit(self, fsm, acts, setup):
        {"s1": fsm.boot, "s2": lambda: to_s2(fsm), "s3": lambda: to_s3(fsm)}[setup]()
        fsm.fault("QCM timeout > 3.0s")
        assert fsm.state is S4 and fsm.fault_reason == "QCM timeout > 3.0s"
        assert acts.names()[-2:] == ["park_hardware", "show_fault"]
        assert fsm.qcm_region is None and fsm.aux_region is None

    def test_fault_ignored_in_s0_and_s4(self, fsm, acts):
        with pytest.raises(TransitionNotAllowed):
            fsm.fault("early")
        to_s3(fsm)
        fsm.fault("first")
        with pytest.raises(TransitionNotAllowed):
            fsm.fault("second")
        assert fsm.fault_reason == "first" and acts.names().count("park_hardware") == 1

    def test_confirm_returns_to_s0_and_allows_reboot(self, fsm, acts):
        to_s3(fsm)
        fsm.fault("x")
        fsm.fault_confirmed()
        assert fsm.state is S0 and fsm.fault_reason is None and "clear_fault" in acts.names()
        fsm.boot()
        assert fsm.state is S1

    def test_confirm_only_in_s4(self, fsm):
        fsm.boot()
        with pytest.raises(TransitionNotAllowed):
            fsm.fault_confirmed()

    def test_park_failure_still_reaches_s4(self):
        fsm = HostStateMachine(RecordingActions(fail={"park_hardware"}))
        to_s3(fsm)
        fsm.fault("x")
        assert fsm.state is S4

    def test_begin_session_failure_escalates(self):
        fsm = HostStateMachine(RecordingActions(fail={"begin_session"}))
        fsm.boot()
        fsm.start_run()
        assert fsm.state is S4 and "begin_session failed" in fsm.fault_reason

    def test_reentrant_confirm_from_modal_is_run_to_completion(self):
        holder = {}
        a = RecordingActions(on_show_fault=lambda: holder["fsm"].fault_confirmed())
        fsm = holder["fsm"] = HostStateMachine(a)
        to_s3(fsm)
        fsm.fault("cable pulled")
        assert fsm.state is S0
        assert fsm.state_path()[-2:] == [S4, S0]


class TestShutdown:
    @pytest.mark.parametrize("setup", ["s1", "s2", "s3", "s4"])
    def test_direct_termination(self, fsm, acts, setup):
        {
            "s1": fsm.boot,
            "s2": lambda: to_s2(fsm),
            "s3": lambda: to_s3(fsm),
            "s4": lambda: (to_s3(fsm), fsm.fault("x")),
        }[setup]()
        fsm.shutdown()
        assert fsm.state is S0 and acts.names()[-1] == "terminate"

    def test_shutdown_in_s0_still_terminates_without_transition(self, fsm, acts):
        n = len(fsm.state_path())
        fsm.shutdown()
        assert acts.names() == ["terminate"] and len(fsm.state_path()) == n


def test_full_acceptance_path(fsm):
    """S0 -> S1 -> S2 -> S3 -> S5 -> S3 -> S4 -> S0 (spec §7 step 3)."""
    to_s3(fsm)
    fsm.fan_pwm_changed(75)
    fsm.fault("Devices unplugged")
    fsm.fault_confirmed()
    assert fsm.state_path() == [S0, S1, S2, S3, S5, S3, S4, S0]


def test_native_listener_observer(fsm):
    seen = []

    class Observer:
        def after_transition(self, event, source, target):
            seen.append(target.value)

    fsm.add_listener(Observer())
    fsm.boot()
    assert fsm.state is S1 and seen == [S1]
