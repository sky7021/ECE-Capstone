"""Acceptance test suite for the Host PC FSM backend (Steps 1-4, spec §7).

Covers:
* Step 1: Pipeline data structures & math (``quality``, ``sauerbrey``,
  ``zoh_fusion``, ``csv_logger``, ``empirical_fit``)
* Step 2: Asynchronous QThread serial workers (``serial_worker``, ``qcm_reader``,
  ``aux_reader``, ``smps_adapter``) including positive/negative ``TEMP_X16``,
  malformed line resilience, and 3.0 s / 6.0 s watchdog faults
* Step 3: Mock generators, Tier 2 loopback harness & full headless integration
  across ``S0 -> S1 -> S2 -> S3 -> S5 -> S3 -> S4 -> S0``
* Step 4: ``MainWindow`` and ``ApplicationController`` FSM orchestration
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import software.tests  # noqa: F401 - activates pytest shim when pytest is not installed
import pytest

from software.fsm.host_controller import HostConfig, HostController
from software.fsm.host_fsm import HostState
from software.gui.app import ApplicationController, parse_arguments
from software.gui.main_window import MainWindow
from software.pipeline.csv_logger import CSV_COLUMNS, CSVLogger
from software.pipeline.empirical_fit import (
    SMPSScan,
    align_and_solve_scans,
    compute_cumulative_dose,
    flow_lpm_to_m3_s,
    solve_k_empirical,
)
from software.pipeline.quality import (
    format_pwm_command,
    is_aux_health_ok,
    is_valid_pwm_duty,
    parse_aux_line,
    parse_qcm_line,
    zoh_age,
)
from software.pipeline.sauerbrey import SauerbreyCalculator, sauerbrey_constant_ng_per_hz
from software.pipeline.zoh_fusion import FusionMode, ZOHFusionEngine
from software.readers.smps_adapter import parse_smps_delimited
from software.tests.serial_mock import (
    MockAuxReader,
    MockQCMReader,
    SyntheticESP32Source,
    SyntheticFPGASource,
)
from software.tests.virtual_loopback import VirtualLoopbackHarness

AUX_FRESH = b"FLOW=1.250,TEMP=24.50,RH=42.10,PWM=50,FRESH=1,OK=1\r\n"
AUX_HELD = b"FLOW=1.300,TEMP=99.00,RH=99.00,PWM=50,FRESH=0,OK=1\r\n"
AUX_SICK = b"FLOW=1.250,TEMP=24.50,RH=42.10,PWM=50,FRESH=1,OK=0\r\n"


# =============================================================================
# Step 1: quality.py, sauerbrey.py, zoh_fusion.py, csv_logger.py, empirical_fit.py
# =============================================================================
class TestQCMRegex:
    def test_positive_temperature(self):
        assert parse_qcm_line(b"QCM_FREQ=0005000123,TEMP_X16=000400\r\n") == (5_000_123, 25.0)

    def test_negative_temperature(self):
        assert parse_qcm_line(b"QCM_FREQ=0005000123,TEMP_X16=-000080\r\n") == (5_000_123, -5.0)

    @pytest.mark.parametrize(
        "line",
        [b"", b"garbage", b"QCM_FREQ=abc,TEMP_X16=000400", b"QCM_FREQ=,TEMP_X16=1", b"\xff\xfe\x00"],
    )
    def test_malformed_rejected(self, line):
        assert parse_qcm_line(line) is None

    def test_non_bytes_rejected(self):
        assert parse_qcm_line("QCM_FREQ=1,TEMP_X16=1") is None  # type: ignore[arg-type]


class TestAuxRegex:
    def test_fresh_line(self):
        aux = parse_aux_line(AUX_FRESH)
        assert aux == {
            "flow_lpm": 1.25,
            "sht_temp_c": 24.5,
            "sht_rh_pct": 42.1,
            "pwm_duty": 50,
            "fresh_flag": True,
            "health_ok": True,
        }

    def test_held_and_negative_temp(self):
        aux = parse_aux_line(b"FLOW=0.500,TEMP=-3.25,RH=10.00,PWM=0,FRESH=0,OK=1")
        assert aux["sht_temp_c"] == -3.25 and aux["fresh_flag"] is False

    @pytest.mark.parametrize(
        "line",
        [
            b"FLOW=1.2.3,TEMP=24.50,RH=42.10,PWM=50,FRESH=1,OK=1",  # unparsable float
            b"FLOW=1.250,TEMP=24.50,RH=42.10,PWM=101,FRESH=1,OK=1",  # PWM out of range
            b"FLOW=1.250,TEMP=24.50,RH=42.10,PWM=50,FRESH=2,OK=1",  # bad flag
            b"FLOW=1.250,TEMP=24.50",  # truncated
            b"",
        ],
    )
    def test_malformed_rejected(self, line):
        assert parse_aux_line(line) is None

    def test_health_guard(self):
        assert is_aux_health_ok(parse_aux_line(AUX_FRESH))
        assert not is_aux_health_ok(parse_aux_line(AUX_SICK))
        assert not is_aux_health_ok(None)


class TestPWM:
    @pytest.mark.parametrize("duty", [0, 1, 50, 100])
    def test_valid(self, duty):
        assert is_valid_pwm_duty(duty)
        assert format_pwm_command(duty) == f"SET_PWM:{duty}\n".encode()

    @pytest.mark.parametrize("duty", [-1, 101, 50.0, True, "50", None])
    def test_invalid(self, duty):
        assert not is_valid_pwm_duty(duty)
        with pytest.raises(ValueError):
            format_pwm_command(duty)  # type: ignore[arg-type]


def test_zoh_age_clamps_negative():
    assert zoh_age(10.0, 10.2) == 0.0
    assert zoh_age(10.0, 7.5) == pytest.approx(2.5)


class TestSauerbrey:
    def test_constant_5mhz(self):
        # 17.7 ng/(cm^2 Hz) * 0.205 cm^2 ~= 3.62 ng/Hz
        assert sauerbrey_constant_ng_per_hz(5.0e6) == pytest.approx(3.622, rel=1e-3)

    def test_requires_baseline(self):
        with pytest.raises(RuntimeError):
            SauerbreyCalculator().compute(1.0, 5_000_000)

    def test_delta_f_and_derivatives(self):
        calc = SauerbreyCalculator()
        calc.freeze_baseline(100.0, 5_000_000, 25.0)
        c = calc.c_qcm_ng_per_hz

        r1 = calc.compute(101.0, 4_999_996)  # -4 Hz over 1 s
        assert r1.delta_f_hz == -4.0
        assert r1.df_dt_hz_s == pytest.approx(-4.0)
        assert r1.dm_dt_ng_s == pytest.approx(4.0 * c)  # mass increases
        assert r1.delta_m_ng == pytest.approx(4.0 * c)

        r2 = calc.compute(103.0, 4_999_990)  # further -6 Hz over 2 s
        assert r2.delta_f_hz == -10.0
        assert r2.df_dt_hz_s == pytest.approx(-3.0)
        assert r2.dm_dt_ng_s == pytest.approx(3.0 * c)

    def test_non_advancing_timestamp_gives_nan(self):
        calc = SauerbreyCalculator()
        calc.freeze_baseline(5.0, 5_000_000, 25.0)
        r = calc.compute(5.0, 4_999_999)
        assert math.isnan(r.df_dt_hz_s) and math.isnan(r.dm_dt_ng_s)
        assert calc.compute(6.0, 4_999_999).df_dt_hz_s == pytest.approx(-1.0)

    def test_reset_halts_math(self):
        calc = SauerbreyCalculator()
        calc.freeze_baseline(0.0, 5_000_000, 25.0)
        calc.reset()
        assert not calc.is_frozen


class Recorder:
    def __init__(self, engine: ZOHFusionEngine) -> None:
        self.tare, self.primed, self.join, self.rows, self.faults = [], [], [], [], []
        engine.qcm_tare_ready.connect(lambda f, t: self.tare.append((f, t)))
        engine.aux_zoh_primed.connect(self.primed.append)
        engine.s2_join_complete.connect(lambda f, t: self.join.append((f, t)))
        engine.fused_row_ready.connect(self.rows.append)
        engine.staleness_fault.connect(self.faults.append)


@pytest.fixture
def engine(qapp):
    e = ZOHFusionEngine()
    e.reset_for_tare(armed_at=0.0)
    return e


def aux(line: bytes) -> dict:
    d = parse_aux_line(line)
    assert d is not None
    return d


class TestS2Join:
    def test_disarmed_engine_ignores_everything(self, qapp):
        e = ZOHFusionEngine()
        rec = Recorder(e)
        e.ingest_qcm(1.0, 5_000_000, 25.0)
        e.ingest_aux(1.0, aux(AUX_FRESH))
        assert e.mode is FusionMode.DISARMED
        assert not (rec.tare or rec.primed or rec.join or rec.rows)

    def test_qcm_alone_does_not_enter_s3(self, engine):
        rec = Recorder(engine)
        for k in range(5):
            engine.ingest_qcm(1.0 + k, 5_000_000 + k, 25.0)
        assert engine.in_s2_a2 and not engine.in_s2_b2
        assert rec.tare == [(5_000_000, 25.0)]  # latched once
        assert rec.join == [] and engine.mode is FusionMode.PRIMING

    def test_unhealthy_aux_does_not_prime(self, engine):
        rec = Recorder(engine)
        engine.ingest_qcm(1.0, 5_000_000, 25.0)
        engine.ingest_aux(1.1, aux(AUX_SICK))
        assert not engine.in_s2_b2 and rec.join == []

    def test_join_after_both_regions_latch(self, engine):
        rec = Recorder(engine)
        engine.ingest_aux(1.0, aux(AUX_FRESH))
        assert engine.in_s2_b2 and not engine.in_s2_a2 and rec.join == []
        engine.ingest_qcm(1.2, 5_000_100, 25.5)
        assert rec.join == [(5_000_100, 25.5)]
        assert engine.mode is FusionMode.STREAMING
        assert engine.cache.is_valid and engine.cache.flow_lpm == 1.25

    def test_baseline_tracks_latest_qcm_until_join(self, engine):
        rec = Recorder(engine)
        engine.ingest_qcm(1.0, 5_000_000, 25.0)
        engine.ingest_qcm(2.0, 5_000_007, 25.1)
        engine.ingest_aux(2.1, aux(AUX_FRESH))
        assert rec.join == [(5_000_007, 25.1)]
        assert engine.sauerbrey.f0_hz == 5_000_007

    def test_packets_before_arm_time_are_discarded(self, qapp):
        e = ZOHFusionEngine()
        e.reset_for_tare(armed_at=10.0)
        e.ingest_qcm(9.9, 5_000_000, 25.0)
        e.ingest_aux(9.9, aux(AUX_FRESH))
        assert not (e.in_s2_a2 or e.in_s2_b2)


class TestS3Fusion:
    def _joined(self, engine) -> Recorder:
        rec = Recorder(engine)
        engine.ingest_aux(0.5, aux(AUX_FRESH))
        engine.ingest_qcm(1.0, 5_000_000, 25.0)
        assert engine.mode is FusionMode.STREAMING
        return rec

    def test_fused_row_math_and_schema(self, engine):
        rec = self._joined(engine)
        engine.ingest_aux(1.5, aux(AUX_HELD))  # held: SHT values must NOT change
        engine.ingest_qcm(2.0, 4_999_996, 25.0)
        (row,) = rec.rows
        assert set(CSV_COLUMNS) <= row.keys()
        assert row["delta_f_hz"] == -4.0
        assert row["df_dt_hz_s"] == pytest.approx(-4.0)
        assert row["dm_dt_ng_s"] == pytest.approx(4.0 * engine.sauerbrey.c_qcm_ng_per_hz)
        assert row["flow_lpm"] == 1.3  # refreshed from held packet
        assert row["sht_temp_c"] == 24.5  # held packet ignored for SHT
        assert row["zoh_flow_age_s"] == pytest.approx(0.5)
        assert row["zoh_sht_age_s"] == pytest.approx(1.5)

    def test_flow_staleness_fault(self, engine):
        rec = self._joined(engine)
        engine.ingest_qcm(3.6, 5_000_000, 25.0)  # flow age 3.1 s
        assert len(rec.faults) == 1 and "flow" in rec.faults[0]
        assert engine.mode is FusionMode.DISARMED and not engine.cache.is_valid
        engine.ingest_qcm(3.7, 5_000_000, 25.0)  # halted: no more rows/faults
        assert rec.rows == [] and len(rec.faults) == 1

    def test_flow_exactly_at_limit_is_fresh(self, engine):
        rec = self._joined(engine)
        engine.ingest_qcm(3.5, 5_000_000, 25.0)  # flow age == 3.0 s
        assert rec.faults == [] and len(rec.rows) == 1

    def test_sht_staleness_fault(self, engine):
        rec = self._joined(engine)
        t = 1.0
        while t < 7.0:  # held packets keep flow fresh, SHT ages
            engine.ingest_aux(t, aux(AUX_HELD))
            t += 1.0
        engine.ingest_qcm(7.0, 5_000_000, 25.0)  # sht age 6.5 s
        assert len(rec.faults) == 1 and "SHT21" in rec.faults[0]

    def test_invalidate_halts_streaming(self, engine):
        rec = self._joined(engine)
        engine.invalidate()
        engine.ingest_qcm(2.0, 5_000_000, 25.0)
        assert rec.rows == [] and not engine.sauerbrey.is_frozen


class TestCSVLogger:
    def test_session_lifecycle(self, tmp_path):
        logger = CSVLogger(tmp_path / "sessions")
        path = logger.open_session_csv()
        assert path.name.startswith("session_") and path.suffix == ".csv"
        logger.log_fused_row({k: 1 for k in CSV_COLUMNS} | {"df_dt_hz_s": math.nan, "extra": 9})
        logger.flush_and_close()
        logger.flush_and_close()  # idempotent
        logger.log_fused_row({"pc_timestamp_s": 2})  # ignored when closed

        with open(path, newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == list(CSV_COLUMNS)
        assert len(rows) == 2
        assert rows[1][CSV_COLUMNS.index("df_dt_hz_s")] == "nan"

    def test_same_second_sessions_do_not_collide(self, tmp_path):
        from datetime import datetime

        logger = CSVLogger(tmp_path)
        stamp = datetime(2026, 1, 1, 12, 0, 0)
        p1 = logger.open_session_csv(now=stamp)
        p2 = logger.open_session_csv(now=stamp)
        logger.flush_and_close()
        assert p1 != p2 and p1.exists() and p2.exists()


class TestEmpiricalFitAndSMPS:
    def test_dose_integral_and_least_squares(self):
        t = [0.0, 1.0, 2.0, 3.0]
        q = [flow_lpm_to_m3_s(1.2)] * 4  # 2e-5 m^3/s
        c = [100.0] * 4  # 100 ug/m^3 -> 2e-3 ug/s
        doses = compute_cumulative_dose(t, q, c)
        assert doses[-1] == pytest.approx(6e-3)
        delta_f = [-2.5 * d for d in doses]
        fit = solve_k_empirical(doses[1:], delta_f[1:])
        assert fit.k_empirical == pytest.approx(2.5)
        assert fit.r_squared == pytest.approx(1.0)

    def test_smps_delimited_parser_and_alignment(self, tmp_path):
        smps_file = tmp_path / "smps.csv"
        smps_file.write_text(
            "scan_id,start_t,end_t,concentration_ug_m3\n"
            "1,0.0,60.0,40.0\n"
            "2,60.0,120.0,150.0\n",
            encoding="utf-8",
        )
        scans = parse_smps_delimited(smps_file)
        assert len(scans) == 2 and scans[0] == SMPSScan(1, 0.0, 60.0, 40.0)

        t = [float(i) for i in range(130)]
        f = [5_000_000.0 - 0.01 * i for i in range(130)]
        q = [2e-5] * 130
        records, fit = align_and_solve_scans(scans, t, f, q, transport_lag_s=4.0)
        assert len(records) == 2 and fit.k_empirical > 0


# =============================================================================
# Step 2: Asynchronous Serial Workers (serial_worker.py, qcm_reader.py, aux_reader.py)
# =============================================================================
class TestSerialWorkers:
    def test_qcm_reader_parses_negative_temp_and_rejects_malformed(self, waiter):
        qcm = MockQCMReader(period_s=10.0, timeout_s=2.0)
        assert qcm.open_port()
        qcm.fake.pause_stream()
        received: list[tuple[int, float]] = []
        qcm.qcm_packet_received.connect(lambda _ts, f, tc: received.append((f, tc)))
        qcm.start()
        try:
            qcm.fake.inject_raw(b"NOT_A_VALID_LINE\r\n")
            qcm.fake.inject_raw(b"QCM_FREQ=0005000123,TEMP_X16=-000080\r\n")
            qcm.fake.inject_raw(b"QCM_FREQ=0005000000,TEMP_X16=000400\r\n")
            assert waiter(lambda: len(received) == 2, timeout=2.0)
            assert received[0] == (5_000_123, -5.0)
            assert received[1] == (5_000_000, 25.0)
            assert qcm.malformed_count == 1
        finally:
            qcm.stop_and_close()

    def test_qcm_watchdog_timeout_emits_fault(self, waiter):
        qcm = MockQCMReader(period_s=10.0, timeout_s=0.25)
        assert qcm.open_port()
        qcm.fake.pause_stream()
        faults: list[str] = []
        qcm.serial_fault.connect(faults.append)
        qcm.start()
        try:
            assert waiter(lambda: len(faults) == 1, timeout=2.0)
            assert "QCM timeout" in faults[0]
        finally:
            qcm.stop_and_close()

    def test_aux_flow_watchdog_timeout_emits_fault(self, waiter):
        aux_r = MockAuxReader(period_s=10.0, flow_timeout_s=0.25, sht_timeout_s=1.0)
        assert aux_r.open_port()
        aux_r.fake.pause_stream()
        faults: list[str] = []
        aux_r.serial_fault.connect(faults.append)
        aux_r.start()
        try:
            assert waiter(lambda: len(faults) == 1, timeout=2.0)
            assert "AUX flow timeout" in faults[0]
        finally:
            aux_r.stop_and_close()

    def test_aux_sht_fresh_watchdog_timeout_emits_fault(self, waiter):
        aux_r = MockAuxReader(period_s=10.0, flow_timeout_s=1.0, sht_timeout_s=0.30)
        assert aux_r.open_port()
        aux_r.fake.pause_stream()
        faults: list[str] = []
        aux_r.serial_fault.connect(faults.append)
        aux_r.start()
        try:
            # Keep feeding held (FRESH=0) packets so flow stays fresh while SHT expires
            for _ in range(5):
                aux_r.fake.inject_raw(AUX_HELD)
                waiter.pump(0.08)
            assert waiter(lambda: len(faults) == 1, timeout=2.0)
            assert "SHT21 fresh timeout" in faults[0]
        finally:
            aux_r.stop_and_close()

    def test_aux_send_pwm_never_purges_input_buffer(self, waiter):
        aux_r = MockAuxReader(period_s=0.05, flow_timeout_s=2.0, sht_timeout_s=4.0)
        assert aux_r.open_port()
        tx_bytes: list[bytes] = []
        aux_r.bytes_transmitted.connect(tx_bytes.append)
        aux_r.start()
        try:
            aux_r.send_pwm_command(75)
            assert waiter(lambda: len(tx_bytes) == 1, timeout=2.0)
            assert tx_bytes[0] == b"SET_PWM:75\n"
            assert aux_r.source.current_pwm == 75
            # Guardrail 5: send_pwm_command must NEVER call reset_input_buffer()
            assert aux_r.fake.purge_count == 0
        finally:
            aux_r.stop_and_close()


# =============================================================================
# Step 3 & Step 4: Full Headless Integration (S0 -> S1 -> S2 -> S3 -> S5 -> S3 -> S4 -> S0)
# =============================================================================
class TestFullStackIntegration:
    def test_mock_mode_full_state_lifecycle(self, tmp_path, waiter):
        """Acceptance test: S0 -> S1 -> S2 -> S3 -> S5 -> S3 -> S4 -> S0 in --mock mode."""
        cfg = HostConfig(
            mock=True,
            log_dir=str(tmp_path / "sessions"),
            mock_period_s=0.05,
            qcm_timeout_s=1.5,
            flow_timeout_s=1.5,
            sht_timeout_s=3.0,
        )
        ctrl = HostController(cfg)
        rows: list[dict] = []
        faults: list[str] = []
        ctrl.fused_row_ready.connect(rows.append)
        ctrl.fault_raised.connect(faults.append)

        # S0 -> S1
        assert ctrl.state is HostState.S0_DISCONNECTED
        assert ctrl.connect_ports()
        assert ctrl.state is HostState.S1_IDLE_STANDBY

        # S1 -> S2
        assert ctrl.start_run()
        assert ctrl.state is HostState.S2_TARE_AND_PRIME

        # S2 -> S3 (join completes automatically when both mock readers emit)
        assert waiter(lambda: ctrl.state is HostState.S3_ACTIVE_STREAMING and len(rows) >= 2, timeout=3.0)
        qcm_mock: MockQCMReader = ctrl.qcm_reader  # type: ignore[assignment]
        aux_mock: MockAuxReader = ctrl.aux_reader  # type: ignore[assignment]
        assert qcm_mock.fake.purge_count == 1
        assert aux_mock.fake.purge_count == 1

        # S3 -> S5 -> S3 (History H restoration + SET_PWM dispatch without buffer purge)
        assert ctrl.set_fan_pwm(80)
        assert ctrl.state is HostState.S3_ACTIVE_STREAMING
        assert ctrl.fsm.history_state is HostState.S3_ACTIVE_STREAMING
        assert waiter(lambda: aux_mock.source.current_pwm == 80, timeout=2.0)
        assert aux_mock.fake.purge_count == 1  # unchanged during S5!

        # S3 -> S4 (Simulated cable disconnect triggers Group Preemptive Exit)
        qcm_mock.fake.simulate_disconnect()
        assert waiter(lambda: ctrl.state is HostState.S4_FAULT, timeout=2.0)
        assert len(faults) == 1
        assert not ctrl.csv_logger.is_open
        assert not ctrl.engine.cache.is_valid

        # S4 -> S0 (Modal fault confirmation)
        assert ctrl.confirm_fault()
        assert ctrl.state is HostState.S0_DISCONNECTED

        assert ctrl.fsm.state_path() == [
            HostState.S0_DISCONNECTED,
            HostState.S1_IDLE_STANDBY,
            HostState.S2_TARE_AND_PRIME,
            HostState.S3_ACTIVE_STREAMING,
            HostState.S5_PWM_FAN,
            HostState.S3_ACTIVE_STREAMING,
            HostState.S4_FAULT,
            HostState.S0_DISCONNECTED,
        ]
        assert ctrl.csv_logger.path is not None and ctrl.csv_logger.path.exists()

    def test_tier2_virtual_loopback_lifecycle(self, tmp_path, waiter):
        """Tier 2 loopback test over bidirectional byte pipes."""
        cfg = HostConfig(
            mock=False,
            qcm_port="ttyV_QCM_RX",
            aux_port="ttyV_AUX_RX",
            log_dir=str(tmp_path / "loopback_sessions"),
            qcm_timeout_s=1.5,
            flow_timeout_s=1.5,
            sht_timeout_s=3.0,
        )
        with VirtualLoopbackHarness(period_s=0.05) as harness:
            ctrl = HostController(cfg, reader_factory=harness.reader_factory)
            rows: list[dict] = []
            ctrl.fused_row_ready.connect(rows.append)

            assert ctrl.connect_ports()
            assert ctrl.start_run()
            assert waiter(
                lambda: ctrl.state is HostState.S3_ACTIVE_STREAMING and len(rows) >= 2,
                timeout=3.0,
            )
            assert ctrl.set_fan_pwm(65)
            assert waiter(lambda: harness.esp32_source.current_pwm == 65, timeout=2.0)

            harness.disconnect_aux()
            assert waiter(lambda: ctrl.state is HostState.S4_FAULT, timeout=2.0)
            assert ctrl.confirm_fault()
            assert ctrl.state is HostState.S0_DISCONNECTED

    def test_gui_application_controller_and_main_window(self, tmp_path, waiter):
        """Step 4: Verify ApplicationController and MainWindow FSM handlers."""
        args = parse_arguments(["--mock", "--log-dir", str(tmp_path / "gui_sessions")])
        assert args.mock is True
        cfg = HostConfig(
            mock=True,
            log_dir=args.log_dir,
            mock_period_s=0.05,
            qcm_timeout_s=1.5,
            flow_timeout_s=1.5,
            sht_timeout_s=3.0,
        )
        win = MainWindow(modal_enabled=False)
        app_ctrl = ApplicationController(config=cfg, main_window=win)

        assert win.current_state is HostState.S0_DISCONNECTED
        assert app_ctrl.init_workers()
        assert win.current_state is HostState.S1_IDLE_STANDBY

        win.on_start_run_clicked()
        assert win.current_state is HostState.S2_TARE_AND_PRIME
        assert waiter(
            lambda: win.current_state is HostState.S3_ACTIVE_STREAMING
            and len(win.time_series_view.t_buf) >= 2,
            timeout=3.0,
        )

        win.on_fan_slider_changed(55)
        assert win.current_state is HostState.S3_ACTIVE_STREAMING
        assert win.history_state is HostState.S3_ACTIVE_STREAMING

        win.on_fault_triggered("Simulated cable pull")
        assert win.current_state is HostState.S4_FAULT
        win.on_fault_modal_confirmed()
        assert win.current_state is HostState.S0_DISCONNECTED
        app_ctrl.shutdown()

    def test_gui_unified_host_controller_and_main_window(self, tmp_path, waiter):
        """Step 4: Verify direct unified HostController + MainWindow architecture."""
        cfg = HostConfig(
            mock=True,
            log_dir=str(tmp_path / "gui_direct_unified"),
            mock_period_s=0.05,
            qcm_timeout_s=1.5,
            flow_timeout_s=1.5,
            sht_timeout_s=3.0,
        )
        ctrl = HostController(cfg)
        win = MainWindow(controller=ctrl, modal_enabled=False)

        assert win.current_state is HostState.S0_DISCONNECTED
        assert ctrl.connect_ports()
        assert win.current_state is HostState.S1_IDLE_STANDBY

        win.on_start_run_clicked()
        assert win.current_state is HostState.S2_TARE_AND_PRIME
        assert waiter(
            lambda: win.current_state is HostState.S3_ACTIVE_STREAMING
            and len(win.time_series_view.t_buf) >= 2,
            timeout=3.0,
        )

        win.on_fan_slider_changed(55)
        assert win.current_state is HostState.S3_ACTIVE_STREAMING
        assert win.history_state is HostState.S3_ACTIVE_STREAMING

        win.on_fault_triggered("Simulated cable pull")
        assert win.current_state is HostState.S4_FAULT
        win.on_fault_modal_confirmed()
        assert win.current_state is HostState.S0_DISCONNECTED
        ctrl.shutdown()



