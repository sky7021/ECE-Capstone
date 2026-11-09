# ECE 490 Capstone Codebase Architecture & Technical Overview

This document provides a comprehensive, code-level breakdown of the software and repository layout for the ECE 490 Resonant Mass Sensing Platform. It details the structural organization, class and module responsibilities, signal/slot wiring, asynchronous concurrency model, and developer execution workflows.

---

## 1. Architectural Philosophy & Separation of Concerns

The host software architecture is structured across three decoupled architectural planes to ensure thread safety, determinism, and testability without hardware in the loop:

1. **Control Plane (`software.fsm.host_fsm`):**
   * Encapsulated entirely in [HostStateMachine](software/fsm/host_fsm.py#L209).
   * Pure Python implementation containing zero Qt or serial dependencies.
   * State transitions, hierarchical superstates, orthogonal regions, and history tracking execute deterministically.
   * All hardware interactions, UI updates, and I/O side effects are strictly delegated through the abstract [HostActions](software/fsm/host_fsm.py#L118) protocol.

2. **Data Plane (`software.pipeline`):**
   * Encapsulated in [ZOHFusionEngine](software/pipeline/zoh_fusion.py#L94), [SauerbreyCalculator](software/pipeline/sauerbrey.py#L51), and [CSVLogger](software/pipeline/csv_logger.py#L47).
   * Ingests parsed sensor records asynchronously, manages the zero-order-hold (ZOH) cache, computes net mass and derivative rates, checks sample staleness against timeout thresholds, and writes session CSV logs.
   * Communicates state progress to the control plane via Qt signals without directly altering the state machine.

3. **I/O Plane (`software.readers`):**
   * Encapsulated in [BaseSerialWorker](software/readers/serial_worker.py#L45) subclasses running in dedicated background worker threads (`QThread`).
   * Handles non-blocking serial line framing, regex validation, watchdog timer evaluation, input buffer purging, and thread-safe uplink command queues.
   * Isolates the main thread / GUI event loop completely from operating system serial calls.

### Core Implementation Guardrails Enforced in Code
* **Autonomous Always-On Ingestion:** The Basys 3 FPGA and ESP32 stream ASCII records autonomously from power-up at 1 Hz. The host performs no software start/stop serial handshaking.
* **Non-Blocking GUI Event Loop:** No pyserial calls (`readline()`, `write()`, `reset_input_buffer()`) occur on the GUI thread.
* **Controlled Serial Purging:** `reset_input_buffer()` is strictly restricted to session start; it is never called during fan PWM adjustments to avoid dropping telemetry.
* **Hardware-Accelerated Visualization:** Real-time plotting uses `pyqtgraph` exclusively; `matplotlib` is prohibited in the live rendering path.

---

## 2. Repository Directory Structure

```text
./
├── .agents/                                # Agent configuration and subagent specifications
│   ├── AGENTS.md                           # Tooling guidelines, python environment path, token rules
│   └── agents/
│       └── scout.md                        # Context-lean research & execution subagent spec
├── docs/                                   # Architectural specifications and engineering artifacts
│   └── .gitkeep
├── firmware/                               # Embedded microcontroller and FPGA HDL projects
│   ├── esp32/                              # ESP32 dual-core FreeRTOS firmware (sensor telemetry, PWM)
│   │   └── .gitkeep
│   └── fpga/                               # Basys 3 AMD Artix-7 Vivado reciprocal counter RTL
│       └── .gitkeep
├── hardware/                               # Electronics and mechanical CAD assets
│   ├── 3d_models/                          # Dual-crystal chamber & flow duct CAD/STL models
│   │   └── .gitkeep
│   ├── pcb/                                # Altium PCB layouts and Gerber outputs
│   │   └── .gitkeep
│   └── schematics/                         # Analog front-end and digital schematics
│       └── .gitkeep
├── software/                               # Host PC software stack (master orchestrator and GUI)
│   ├── __init__.py                         # Root package initialization
│   ├── fsm/                                # Control plane finite state machine and controllers
│   │   ├── __init__.py                     # FSM exports
│   │   ├── headless.py                     # CLI entry point for non-GUI execution and CI/CD
│   │   ├── host_controller.py              # Qt QObject orchestrator linking FSM, readers, and pipeline
│   │   └── host_fsm.py                     # Pure-Python hierarchical Host PC state machine
│   ├── gui/                                # PyQt6 desktop dashboard and pyqtgraph widgets
│   │   ├── __init__.py                     # GUI package exports
│   │   ├── app.py                          # GUI application entry point and signal wiring
│   │   ├── base.py                         # Architectural interface definition stub
│   │   ├── main_window.py                  # Primary dashboard window, layout, and fault modals
│   │   ├── views/                          # Dashboard sub-views
│   │   │   ├── __init__.py
│   │   │   ├── diagnostic_view.py          # Port health, ZOH freshness indicators, PWM slider
│   │   │   └── time_series_view.py         # 60 FPS multi-plot pyqtgraph real-time charts
│   │   └── widgets/                        # Specialized visual components
│   │       ├── __init__.py
│   │       ├── dynamic_ribbon.py           # Sliding-window mean and standard deviation envelope
│   │       └── status_indicator.py         # State badges and orthogonal lock indicators
│   ├── pipeline/                           # Data processing, quality validation, and calibration math
│   │   ├── __init__.py                     # Pipeline exports
│   │   ├── csv_logger.py                   # Immutable session CSV logging engine
│   │   ├── empirical_fit.py                # Reference SMPS scan alignment and calibration solver
│   │   ├── quality.py                      # Regex parsers, health checkers, and ZOH age checkers
│   │   ├── sauerbrey.py                    # Sauerbrey differential mass and derivative engine
│   │   └── zoh_fusion.py                   # Multi-rate zero-order-hold fusion and S2 latching
│   ├── readers/                            # Dedicated serial worker threads
│   │   ├── __init__.py                     # Reader exports
│   │   ├── aux_reader.py                   # Link B worker: ESP32 ASCII parser and PWM uplink
│   │   ├── qcm_reader.py                   # Link A worker: FPGA 1 Hz ASCII parser with watchdog
│   │   ├── serial_worker.py                # Base QThread serial worker with queue and watchdog loop
│   │   └── smps_adapter.py                 # File-watcher adapter for reference TSI SMPS scan exports
│   └── tests/                              # Hardware-decoupled test suite and virtual emulation
│       ├── __init__.py                     # Test suite initialization and fallback pytest shim
│       ├── _pytest_shim.py                 # Standalone test runner when pytest is not installed
│       ├── conftest.py                     # Process-wide QCoreApplication fixtures and event pumpers
│       ├── serial_mock.py                  # Synthetic in-process FPGA/ESP32 serial generators
│       ├── test_host_fsm.py                # Unit tests for pure-Python HostStateMachine
│       ├── test_pipeline.py                # Full integration test suite for pipeline, workers, and GUI
│       └── virtual_loopback.py             # Virtual serial port harness (in-process pipe & OS COM pairs)
├── capstone_agentic_architecture_specification.md  # Comprehensive system specification document
├── fpga_esp32_testing.py                   # Standalone serial emulator script for hardware test benches
├── proposed_setup1.py                      # Proof-of-concept numerical SMPS validation script
├── README.md                               # Project documentation, team roles, and quickstart guide
└── requirements.txt                        # Python dependencies (PyQt6, pyqtgraph, pyserial, etc.)
```

---

## 3. Package & Module Breakdown (`software/`)

### 3.1. Control Plane: `software/fsm/`

This package coordinates state lifecycle management, operational safety guards, and UI/headless execution.

#### [host_fsm.py](software/fsm/host_fsm.py)
A pure-Python implementation of the host state machine. It contains no GUI or hardware dependencies.
* [`HostState`](software/fsm/host_fsm.py#L63): Enumeration defining `S0_DISCONNECTED`, `S1_IDLE_STANDBY`, `S2_TARE_AND_PRIME`, `S3_ACTIVE_STREAMING`, `S4_FAULT`, and `S5_PWM_FAN`. The states `S1`, `S2`, and `S3` constitute the `HS_CONNECTED` superstate.
* [`QCMTareRegion`](software/fsm/host_fsm.py#L86): Orthogonal region `S2.2` states: `A1_WAIT_QCM_FIRST` and `A2_QCM_TARE_READY`.
* [`AuxPrimeRegion`](software/fsm/host_fsm.py#L93): Orthogonal region `S2.1` states: `B1_WAIT_AUX_FIRST` and `B2_AUX_ZOH_PRIMED`.
* [`Transition`](software/fsm/host_fsm.py#L101): Immutable dataclass recording source state, target state, event name, and detail metadata. Internal orthogonal transitions are flagged via `is_internal`.
* [`HostActions`](software/fsm/host_fsm.py#L118): Structural typing protocol defining required delegate callbacks:
  * Guards: `ports_available() -> bool`, `ports_healthy() -> bool`.
  * Actions: `open_hardware() -> bool`, `begin_session() -> None`, `begin_streaming(f0_hz, t0_c) -> None`, `transmit_pwm(duty) -> None`, `park_hardware(reason) -> None`, `show_fault(reason) -> None`, `clear_fault() -> None`, `terminate() -> None`.
* [`NullActions`](software/fsm/host_fsm.py#L165): Default no-op implementation of `HostActions` with permissive guards for headless dry-runs and unit testing.
* [`HostStateMachine`](software/fsm/host_fsm.py#L209): State engine handling:
  * Run-to-completion event queuing via `_dispatch` and internal `_queue` to safely process concurrent/re-entrant triggers.
  * History pseudostate `H` tracking for transient `S5: PWM FAN` transitions back to the previous substate.
  * Parallel synchronization of `S2.2` and `S2.1` before allowing transition to `S3`.
  * Group preemptive fault exit from any connected substate to `S4`.

#### [host_controller.py](software/fsm/host_controller.py)
A Qt-aware controller bridging `HostStateMachine`, reader threads, data fusion, and CSV persistence.
* [`HostConfig`](software/fsm/host_controller.py#L50): Configuration dataclass specifying port names, baud rate, mock flags, timeout thresholds, and log directory.
* [`default_reader_factory`](software/fsm/host_controller.py#L65): Instantiates production serial workers (`QCMReader`, `AuxReader`) or synthetic testing mock readers based on `HostConfig.mock`.
* [`HostController`](software/fsm/host_controller.py#L88): Main `QObject` orchestrator. Exposes public slots (`connect_ports`, `start_run`, `set_fan_pwm`, `on_fault_triggered`, `confirm_fault`, `shutdown`), connects worker signals to data fusion engines, verifies worker lifecycle, and emits control and data signals.
* [`_ControllerActions`](software/fsm/host_controller.py#L252): Private class implementing the `HostActions` protocol on behalf of `HostController`.

#### [headless.py](software/fsm/headless.py)
CLI runner executing the complete pipeline without GUI dependencies.
* [`parse_arguments`](software/fsm/headless.py#L30): CLI parser for `--mock`, `--qcm-port`, `--aux-port`, `--baud`, `--duration`, and scheduled `--pwm DUTY@SECONDS`.
* [`configure_signal_handling`](software/fsm/headless.py#L59): Attaches a keep-alive `QTimer` to allow OS `SIGINT` / `SIGTERM` signals to be caught by Python while Qt's C++ loop is blocked.
* [`main`](software/fsm/headless.py#L81): Boots the controller, begins the run, prints streaming telemetry rows, schedules PWM steps, and shuts down cleanly on expiry or fault.

---

### 3.2. Asynchronous Ingestion & Workers: `software/readers/`

Dedicated background worker threads (`QThread`) handling wire-level transport.

#### [serial_worker.py](software/readers/serial_worker.py)
* [`BaseSerialWorker`](software/readers/serial_worker.py#L45): Subclass of `QThread`. Contains the core serial read loop inside `run()`.
  * Thread-Safe Signaling: Uses internal thread-safe synchronization flags: `_stop_evt` (`threading.Event`), `_purge_evt` (`threading.Event`), and `_tx_queue` (`queue.Queue[bytes]`).
  * Buffer Protection: Implements partial-line accumulation in `_rx_buf` with a cap of `MAX_LINE_BYTES` (4096 bytes) to protect against un-terminated noise streams.
  * Bounded Reads: Enforces `READ_TIMEOUT_S = 0.1s` so that watchdog checks, purges, and uplink writes are serviced at a minimum of 10 Hz even without incoming data.
* [`_SerialFallback`](software/readers/serial_worker.py#L22): Mock serial interface class allowing import without crashing when `pyserial` is unavailable in minimal offline environments.

#### [qcm_reader.py](software/readers/qcm_reader.py)
* [`QCMReader`](software/readers/qcm_reader.py#L13): Manages Link A (Basys 3 FPGA USB-UART at 115,200 baud).
  * Line Parsing: Decodes 1 Hz ASCII frames `QCM_FREQ=<10d>,TEMP_X16=<6d>\r\n`.
  * Signals: Emits `qcm_packet_received(ts, freq_hz, temp_c)`.
  * Watchdog: Evaluates elapsed time since last valid packet against `timeout_s` (default 3.0 s), emitting `serial_fault` if breached.

#### [aux_reader.py](software/readers/aux_reader.py)
* [`AuxReader`](software/readers/aux_reader.py#L18): Manages Link B (ESP32 bidirectional USB-UART at 115,200 baud).
  * Downlink Parsing: Ingests 1 Hz ASCII telemetry: `FLOW=...,TEMP=...,RH=...,PWM=...,FRESH=...,OK=...`.
  * Uplink Dispatch: Provides thread-safe slot `send_pwm_command(duty: int)`, which enqueues `SET_PWM:<duty>\n` into the worker's transmission queue without touching the input buffer.
  * Multi-Rate Watchdogs: Evaluates both 3.0 s flow timeout and 6.0 s SHT21 fresh measurement timeout (`fresh_flag == 1`).

#### [smps_adapter.py](software/readers/smps_adapter.py)
* [`parse_smps_delimited`](software/readers/smps_adapter.py#L19): Robust CSV and tab-delimited parser supporting TSI Aerosol Instrument Manager (AIM) exports. Normalizes varied column headers (`sample #`, `start_s`, `mass_conc_ug_m3`) into standard scan objects.
* [`SMPSFileWatcher`](software/readers/smps_adapter.py#L91): Qt polling watcher (`QTimer`) monitoring an active reference scan export file and emitting newly appended `SMPSScan` objects.

---

### 3.3. Processing & Analytics Pipeline: `software/pipeline/`

Modules responsible for signal validation, multi-rate data fusion, and empirical mass modeling.

#### [quality.py](software/pipeline/quality.py)
* Constants: Defines timing limits (`QCM_TIMEOUT_S = 3.0`, `FLOW_TIMEOUT_S = 3.0`, `SHT_TIMEOUT_S = 6.0`) and PWM range (`PWM_MIN = 0`, `PWM_MAX = 100`).
* Regex Parsers:
  * [`parse_qcm_line`](software/pipeline/quality.py#L45): Matches `QCM_FREQ=(\d+),TEMP_X16=(-?\d+)`, converting `TEMP_X16` to degrees Celsius by dividing by 16.0.
  * [`parse_aux_line`](software/pipeline/quality.py#L64): Matches `FLOW=([\d.]+),TEMP=(-?[\d.]+),RH=([\d.]+),PWM=(\d+),FRESH=([01]),OK=([01])` and returns a validated dictionary.
* Health & Command Validators:
  * [`is_aux_health_ok`](software/pipeline/quality.py#L97): Verifies that ESP32 health bit is asserted (`OK=1`).
  * [`is_valid_pwm_duty`](software/pipeline/quality.py#L102): Type-guards PWM duty integers in range `[0, 100]`.
  * [`format_pwm_command`](software/pipeline/quality.py#L107): Formats `SET_PWM:<duty>\n` ASCII commands.
* ZOH Age Utilities:
  * [`zoh_age`](software/pipeline/quality.py#L117): Clamps timestamp skews between independent serial threads to non-negative values.
  * [`is_flow_fresh`](software/pipeline/quality.py#L127) and [`is_sht_fresh`](software/pipeline/quality.py#L131): Threshold checks for cache staleness.

#### [sauerbrey.py](software/pipeline/sauerbrey.py)
* Physical Constants: AT-cut quartz properties (`QUARTZ_DENSITY_G_CM3 = 2.648`, `QUARTZ_SHEAR_MODULUS_G_CM_S2 = 2.947e11`, `ELECTRODE_AREA_CM2 = 0.205`, `NOMINAL_F0_HZ = 5.0e6`).
* [`sauerbrey_constant_ng_per_hz`](software/pipeline/sauerbrey.py#L30): Computes the crystal mass sensitivity constant (~3.62 ng/Hz for the 5 MHz openQCM crystal).
* [`SauerbreyResult`](software/pipeline/sauerbrey.py#L44): Immutable container storing net frequency shift, first derivative of frequency, net mass deposition, and mass deposition rate.
* [`SauerbreyCalculator`](software/pipeline/sauerbrey.py#L51): Manages the frozen tare baseline (`f0`, `T0`). Computes real-time derivative rates using backwards difference anchoring, correctly handling non-advancing timestamps with `NaN`.

#### [zoh_fusion.py](software/pipeline/zoh_fusion.py)
* [`ZOHCache`](software/pipeline/zoh_fusion.py#L48): Holds the most recent auxiliary values (flow, temperatures, relative humidity, PWM duty, timestamps) across multi-rate intervals.
* [`FusionMode`](software/pipeline/zoh_fusion.py#L88): States representing `DISARMED`, `PRIMING`, and `STREAMING`.
* [`ZOHFusionEngine`](software/pipeline/zoh_fusion.py#L94): Core fusion manager.
  * Ingestion Slots: `ingest_qcm` and `ingest_aux`. Discards samples timestamped prior to session arming.
  * Orthogonal Priming: Tracks `in_s2_a2` and `in_s2_b2`. Continues updating the tentative tare baseline on every fresh QCM tick until both regions are ready.
  * S2 Join Evaluation: On concurrent lock of both regions, freezes baseline `f0` and `T0` in `SauerbreyCalculator`, enters `STREAMING`, and emits `s2_join_complete`.
  * Streaming Fusion: Joins 1 Hz QCM packets with ZOH-held AUX data, validates cache freshness against staleness limits, computes Sauerbrey mass rates, and emits `fused_row_ready`.

#### [csv_logger.py](software/pipeline/csv_logger.py)
* [`CSV_COLUMNS`](software/pipeline/csv_logger.py#L21): Canonical 12-column tuple matching specification fields.
* [`CSVLogger`](software/pipeline/csv_logger.py#L47): Session logging engine.
  * Generates timestamps in `data/sessions/session_YYYYMMDD_HHMMSS.csv`.
  * `log_fused_row()` flushes immediately on every 1 Hz record to protect against data loss in the event of power cuts or unexpected disconnects.
  * `flush_and_close()` is idempotent and safe to invoke repeatedly during fault shutdown or application exit.

#### [empirical_fit.py](software/pipeline/empirical_fit.py)
* Data Containers: [`SMPSScan`](software/pipeline/empirical_fit.py#L31), [`EmpiricalFitResult`](software/pipeline/empirical_fit.py#L41), and [`ScanAlignmentRecord`](software/pipeline/empirical_fit.py#L52).
* [`compute_cumulative_dose`](software/pipeline/empirical_fit.py#L66): Evaluates cumulative mass/exposure integrals using piecewise trapezoidal integration.
* [`flow_lpm_to_m3_s`](software/pipeline/empirical_fit.py#L94): Unit conversion from L/min to m³/s.
* [`solve_k_empirical`](software/pipeline/empirical_fit.py#L101): Linear least squares regression forced through the origin solving for calibration constant and goodness-of-fit.
* [`align_and_solve_scans`](software/pipeline/empirical_fit.py#L137): Applies plumbing transport lag compensation, slices high-speed QCM and flow data over SMPS time windows, integrates sampled branch volume, and determines the empirical calibration factor.

---

### 3.4. GUI Presentation Layer: `software/gui/`

Desktop dashboard built using PyQt6 and hardware-accelerated pyqtgraph.

#### [app.py](software/gui/app.py)
* [`parse_arguments`](software/gui/app.py#L27): Parses GUI execution flags (`--mock`, `--qcm-port`, `--aux-port`, `--baud`, `--log-dir`).
* [`ApplicationController`](software/gui/app.py#L42): Top-level GUI orchestrator. Initializes reader threads, configures signal wiring between readers, ZOH fusion, CSV logger, and `MainWindow`, and manages application lifecycle.
* [`configure_signal_handling`](software/gui/app.py#L125): Hooks OS signals to clean application shutdown.
* [`main`](software/gui/app.py#L144): Entry point constructing `QApplication`, `MainWindow`, and `ApplicationController`.

#### [main_window.py](software/gui/main_window.py)
* [`_WindowActions`](software/gui/main_window.py#L23): Implements `HostActions` for standalone GUI execution without `HostController`.
* [`MainWindow`](software/gui/main_window.py#L82): Master dashboard window.
  * Embeds [DiagnosticView](software/gui/views/diagnostic_view.py#L20) and [TimeSeriesView](software/gui/views/time_series_view.py#L24).
  * State Synchronization: Listens to FSM transitions via `_sync_from_transition` to update state badges and lock indicators.
  * Fault Handling: Presents blocking modal dialogs (`QMessageBox.critical`) when `S4: FAULT` is triggered, auto-confirming return to `S0: DISCONNECTED`.

#### [views/time_series_view.py](software/gui/views/time_series_view.py)
* [`TimeSeriesView`](software/gui/views/time_series_view.py#L24): Four synchronized real-time `pyqtgraph` waveform plots:
  1. Raw QCM frequency.
  2. Tared net frequency shift.
  3. Sauerbrey mass deposition rate.
  4. Crystal temperature vs chamber air temperature.
  Includes a graceful widget fallback when `pyqtgraph` is not present in headless environments.

#### [views/diagnostic_view.py](software/gui/views/diagnostic_view.py)
* [`DiagnosticView`](software/gui/views/diagnostic_view.py#L20): Control panel containing the "Start Run" button, 0–100% horizontal fan PWM slider, digital flow/RH readouts, and ZOH cache age displays.

#### [widgets/status_indicator.py](software/gui/widgets/status_indicator.py)
* [`StatusIndicator`](software/gui/widgets/status_indicator.py#L12): Status indicator widget displaying active FSM state, S2 orthogonal region lock status (`S2.A2` and `S2.B2`), and ZOH cache freshness flags.

#### [widgets/dynamic_ribbon.py](software/gui/widgets/dynamic_ribbon.py)
* [`DynamicRibbon`](software/gui/widgets/dynamic_ribbon.py#L11): Rolling statistical calculator tracking sliding-window mean and ±1σ confidence envelopes for noisy mass rate signals.

---

### 3.5. Testing & Emulation Framework: `software/tests/`

Framework providing hardware-free verification and loopback emulation.

#### [serial_mock.py](software/tests/serial_mock.py)
* [`SyntheticFPGASource`](software/tests/serial_mock.py#L34): Synthesizes 1 Hz Basys 3 FPGA frames, adding Gaussian frequency noise and simulating an aerosol mass deposition ramp between t=10s and t=70s.
* [`SyntheticESP32Source`](software/tests/serial_mock.py#L69): Emulates ESP32 dual-core telemetry. Emits fresh SHT21 data on even ticks, held data on odd ticks, scales airflow with received PWM duty, and parses incoming `SET_PWM:<duty>` commands.
* [`FakeSerialPort`](software/tests/serial_mock.py#L126): Drop-in replacement for `serial.Serial`. Supports realistic input buffer accumulation, `reset_input_buffer()`, and fault injection (`simulate_disconnect()`, `pause_stream()`, `inject_raw()`).
* [`MockQCMReader`](software/tests/serial_mock.py#L244) & [`MockAuxReader`](software/tests/serial_mock.py#L266): Subclasses of production workers overriding port creation with `FakeSerialPort`.

#### [virtual_loopback.py](software/tests/virtual_loopback.py)
* [`_BytePipe`](software/tests/virtual_loopback.py#L31): Thread-safe in-memory bidirectional byte stream connecting endpoints.
* [`LoopbackSerialEndpoint`](software/tests/virtual_loopback.py#L95): Endpoint implementing the complete `pyserial` API for loopback testing.
* [`InProcessLoopbackPair`](software/tests/virtual_loopback.py#L160): Paired virtual cable linking `tx_end` to `rx_end`.
* [`VirtualLoopbackHarness`](software/tests/virtual_loopback.py#L248): Orchestrator running background synthetic device threads writing to virtual COM pairs while host readers ingest from the receiving end.

#### Test Suites & Shims
* [conftest.py](software/tests/conftest.py): Provides session-wide `qapp` fixture running offscreen and `waiter` helper for pumping cross-thread Qt signals.
* [_pytest_shim.py](software/tests/_pytest_shim.py): Embedded testing shim providing `pytest.approx`, `pytest.raises`, `pytest.fixture`, and `mark.parametrize` mapped to Python's standard `unittest` framework when `pytest` is not installed.
* [test_host_fsm.py](software/tests/test_host_fsm.py): 100% pure-Python test suite validating state transitions, guard rejections, orthogonal S2 latching, history return, and re-entrant event dispatching.
* [test_pipeline.py](software/tests/test_pipeline.py): Comprehensive integration test suite covering quality parsers, Sauerbrey math, ZOH fusion, CSV logging, empirical fitting, reader watchdogs, loopback harness, and GUI workflows.

---

## 4. Architectural Wiring & Execution Flow in Code

### 4.1. Detailed Flow: Boot & Initialization (`S0 -> S1`)
1. User or CLI invokes [HostController.connect_ports()](software/fsm/host_controller.py#L133).
2. `HostController` invokes `HostStateMachine.boot()`.
3. `HostStateMachine` evaluates guard `HostActions.ports_available()`, implemented in `_ControllerActions.ports_available()`.
4. `HostStateMachine` executes action `HostActions.open_hardware()`, implemented in `_ControllerActions.open_hardware()`:
   * Teardown of prior workers (if any) via `_teardown_workers()`.
   * Calls `_reader_factory(config)` to construct `QCMReader` and `AuxReader`.
   * Invokes `open_port()` on both reader instances.
   * Wires reader signals (`qcm_packet_received`, `aux_packet_received`, `serial_fault`) to controller slots via `_bind_workers()`.
   * Calls `start()` on both `QThread` instances.
5. FSM enters `S1: IDLE / STANDBY` and records the transition.
6. `HostController._on_transition` emits `state_changed(Transition)`.

### 4.2. Detailed Flow: Run Start & Dual Priming Join (`S1 -> S2 -> S3`)
1. User clicks "Start Run" or CLI calls [HostController.start_run()](software/fsm/host_controller.py#L143).
2. `HostStateMachine.start_run()` executes:
   * Checks `ports_healthy()` guard (verifies both reader threads are running with open ports).
   * Executes `HostActions.begin_session()`:
     * Calls `QCMReader.purge_input_buffer()` and `AuxReader.purge_input_buffer()` (sets `_purge_evt` on worker threads).
     * Invokes `CSVLogger.open_session_csv()`.
     * Calls `ZOHFusionEngine.reset_for_tare(armed_at)`.
   * Initializes orthogonal regions: `_qcm_region = A1_WAIT_QCM_FIRST`, `_aux_region = B1_WAIT_AUX_FIRST`.
   * Enters `S2: TARE_AND_PRIME`.
3. Asynchronous Priming:
   * `QCMReader` reads first valid line and emits `qcm_packet_received(ts, freq, temp)`.
   * `HostController._on_qcm_packet` forwards packet to `ZOHFusionEngine.ingest_qcm`.
   * `ZOHFusionEngine` sets `in_s2_a2 = True` and emits `qcm_tare_ready`.
   * `HostController._on_qcm_tare_ready` triggers `HostStateMachine.qcm_tare_ready()`, transitioning `S2.A1 -> S2.A2`.
   * Concurrently, `AuxReader` reads first healthy packet (`OK=1`) and emits `aux_packet_received(ts, dict)`.
   * `HostController._on_aux_packet` forwards packet to `ZOHFusionEngine.ingest_aux`.
   * `ZOHFusionEngine` primes `ZOHCache`, sets `in_s2_b2 = True`, and emits `aux_zoh_primed`.
   * `HostController._on_aux_zoh_primed` triggers `HostStateMachine.aux_zoh_primed()`, transitioning `S2.B1 -> S2.B2`.
4. Join Completion:
   * `ZOHFusionEngine.check_s2_join()` detects `in_s2_a2 and in_s2_b2`.
   * Baseline `f0` and `T0` are frozen in `SauerbreyCalculator`.
   * Engine switches mode to `FusionMode.STREAMING` and emits `s2_join_complete(f0, t0)`.
   * `HostController._on_join_complete` calls `HostStateMachine.join_completed(f0, t0)`.
   * FSM enters `S3: ACTIVE_STREAMING & ZOH_FUSION`.

### 4.3. Detailed Flow: Streaming Telemetry & Data Logging (`S3`)
1. On each 1 Hz clock edge, `QCMReader` parses an FPGA record and emits `qcm_packet_received`.
2. `HostController` relays packet to `ZOHFusionEngine.ingest_qcm`.
3. In `FusionMode.STREAMING`, `ZOHFusionEngine._fuse` executes:
   * Evaluates flow age (`zoh_age(ts, cache.last_flow_ts)`) and SHT age against staleness thresholds.
   * Invokes `SauerbreyCalculator.compute(ts, freq_hz)` to obtain frequency shift, derivatives, and mass rates.
   * Bundles telemetry into a unified dictionary and emits `fused_row_ready(row)`.
4. Signal Distribution:
   * `CSVLogger.log_fused_row(row)` writes and flushes the 12-column record to disk.
   * `MainWindow.update_plots(row)` appends data to `TimeSeriesView` buffers (triggering pyqtgraph curve redraws) and updates `DiagnosticView` digital readouts.

### 4.4. Detailed Flow: Fan Speed Modulation & History Return (`HS_CONNECTED -> S5 -> H`)
1. User adjusts slider in `DiagnosticView`, emitting `fan_pwm_changed(duty)`.
2. `MainWindow.on_fan_slider_changed` or `HostController.set_fan_pwm` invokes `HostStateMachine.fan_pwm_changed(duty)`.
3. FSM validates `0 <= duty <= 100` and records current state into `_history`.
4. FSM transitions to `S5: PWM FAN` and executes `HostActions.transmit_pwm(duty)`.
5. `_ControllerActions.transmit_pwm` calls `AuxReader.send_pwm_command(duty)`.
6. `AuxReader` puts `SET_PWM:<duty>\n` into thread-safe `_tx_queue` without altering input buffers.
7. Background worker thread drains queue and executes `ser.write()` in non-blocking loop.
8. FSM automatically transitions from `S5` back to the saved `_history` state (`S1`, `S2`, or `S3`).

### 4.5. Detailed Flow: Fault Detection, Preemptive Parking, & Modal Recovery (`HS_CONNECTED -> S4 -> S0`)
1. Fault Trigger:
   * Reader serial timeout or exception: `QCMReader` or `AuxReader` emits `serial_fault(msg)`.
   * Pipeline staleness timeout: `ZOHFusionEngine` emits `staleness_fault(msg)`.
2. Slot Invocation:
   * `HostController.on_fault_triggered(msg)` invokes `HostStateMachine.fault(msg)`.
3. Group Preemptive Exit:
   * FSM preempts active state and calls `HostActions.park_hardware(reason)`.
   * `_ControllerActions.park_hardware` invalidates `ZOHFusionEngine` cache, flushes and closes active CSV file, stops reader threads, and closes serial ports.
   * FSM enters `S4: FAULT`.
   * FSM calls `HostActions.show_fault(reason)`.
4. User Modal Confirmation:
   * `MainWindow` displays blocking modal error dialog (`QMessageBox.critical`).
   * When user clicks "OK", `MainWindow.on_fault_modal_confirmed()` calls `HostController.confirm_fault()`.
   * `HostStateMachine.fault_confirmed()` transitions system from `S4: FAULT` to `S0: DISCONNECTED`.

---

## 5. Developer Quick-Reference Commands

All commands assume an active Python environment (Python 3.10+) with repository dependencies installed:
```bash
pip install -r requirements.txt
```

### 5.1. Headless Backend Mode
Run the backend without GUI dependencies for automated verification or batch runs:

```bash
# 30-second run using in-process synthetic mocks, logging to data/sessions/
python -m software.fsm.headless --mock --duration 30

# 60-second mock run scheduling fan PWM changes at 10s and 30s
python -m software.fsm.headless --mock --duration 60 --pwm 50@10 --pwm 80@30

# Connect to physical hardware COM ports
python -m software.fsm.headless --qcm-port COM3 --aux-port COM4 --baud 115200
```

### 5.2. Graphical User Interface (GUI)
Launch the PyQt6 real-time monitoring dashboard:

```bash
# Launch GUI in mock mode (in-process synthetic signal generators)
python software/gui/app.py --mock

# Launch GUI connected to physical USB serial ports
python software/gui/app.py --qcm-port COM3 --aux-port COM4
```

### 5.3. Virtual Loopback Emulation (Tier 2)
Test the full serial communication stack using virtual serial pairs:

```bash
# Windows com0com: spawn synthetic emitters on COM10 (FPGA) and COM12 (ESP32)
python -m software.tests.serial_mock --qcm-tx COM10 --aux-tx COM12

# Run the host connecting to the receiving ends (COM11 and COM13)
python -m software.fsm.headless --qcm-port COM11 --aux-port COM13
```

### 5.4. Unit & Integration Test Suites
Execute automated verification tests:

```bash
# Run all unit and integration tests via pytest
pytest

# Run pure-Python FSM control plane tests
pytest software/tests/test_host_fsm.py -v

# Run pipeline, reader watchdog, and GUI integration tests
pytest software/tests/test_pipeline.py -v

# Run tests using the embedded fallback runner (standard library unittest)
python -m unittest discover -s software/tests -v
```
