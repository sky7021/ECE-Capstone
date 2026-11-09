# ECE 490 Capstone: System Architecture & Agentic Development Specification

> **Project:** FPGA-Based Resonant Mass Sensing Platform for Ultrafine Particulate Matter Detection ($< 0.25\ \mu\text{m}$)
>
> **Architecture Revision:** (3-2) Always-On Dual-UART ASCII Streaming with Host-Gated Logging
>
> **Target Environment:** Autonomous Coding Agents (Claude Code, Google Antigravity, OpenAI Codex) & Human Developers

## 1. System Topology & Core Engineering Invariants

The platform operates as three physically decoupled, communicating Finite State Machines (CFSMs)—the **Host PC**, the **Espressif ESP32**, and the **Digilent Basys 3 Artix-7 FPGA**—connected via two independent USB-over-UART physical links at $115,200\text{ baud}$ (`8N1`).

On the Host PC, the architecture strictly segregates the **Control Plane** (`software/fsm/host_fsm.py` using `python-statemachine` v3.2.2 `StateChart` with compound states, orthogonal regions, and deep history, coordinated via `HostController` in `software/fsm/host_controller.py` and executable headlessly via `software/fsm/headless.py`) from the **Data Plane** (`software/pipeline/zoh_fusion.py` real-time 1 Hz packet ingestion, Zero-Order-Hold caching, Sauerbrey derivative calculations, and CSV session logging). All cross-boundary messaging uses thread-safe PyQt6 signals and slots.

### Strict Architectural Guardrails for Coding Agents

1. **No Start/Stop Handshakes on Serial Links:** Both the Basys 3 FPGA and the ESP32 are **always-on** devices that autonomously stream $1\text{ Hz}$ newline-delimited ASCII records from power-on. Do **not** implement `0x01`/`0x02`, `START\n`/`STOP\n`, or `0x80` (`LAST_FRAME`) draining states (`S4: DRAINING` is deprecated and removed).

2. **Single-Crystal QCM Compensation:** The FPGA counts a single $5\text{ MHz}$ QCM oscillator (`QCM_FREQ`) over a $1\text{ s}$ gate and polls an on-holder MCP9808 temperature sensor (`TEMP_X16`) over $\text{I}^2\text{C}$. Do **not** expect dual-crystal (`f_sense`, `f_ref`) binary packets or 20-byte/23-byte binary struct framing.

3. **Non-Blocking GUI Thread & Decoupled FSM Control Plane:** All `pyserial` operations (`readline()`, `write()`, `reset_input_buffer()`) **must** execute inside dedicated `QThread` workers (`qcm_reader.py`, `aux_reader.py`). Communication between the GUI (`main_window.py`), the master FSM controller (`host_controller.py`), the serial workers, and the pipeline engine (`zoh_fusion.py`) must use thread-safe `pyqtSignal` and `pyqtSlot` connections. The FSM operates synchronously on the main thread via declarative UML `StateChart` transitions without cross-thread re-entrancy risks.

4. **Hardware-Accelerated Plotting Only:** Use `pyqtgraph` exclusively for real-time time-series plotting (`time_series_view.py`). Never import or embed `matplotlib` canvases in the real-time GUI loop.

5. **Preserve OS Serial Buffers During Fan PWM Updates:** Never call `reset_input_buffer()` during `S5: PWM FAN` (`SET_PWM:<duty>\n`). Calling `reset_input_buffer()` is restricted strictly to the `S1: IDLE -> S2: TARE_AND_PRIME` session start transition.

## 2. Wire-Level Interface Control Specification (ICD)

### Link A: Basys 3 FPGA $\to$ Host PC (Unidirectional Downlink)

* **Physical Transport:** USB-over-UART (FTDI FT2232HQ Virtual COM Port), $115,200\text{ baud}$, 8 data bits, no parity, 1 stop bit (`8N1`).

* **Direction & Cadence:** Transmit-only (`FPGA -> PC`), $1.0\text{ s}$ hardware gate ($1\text{ Hz}$ continuous autonomous stream).

* **ASCII Wire Format:**

  ```
  QCM_FREQ=0005000123,TEMP_X16=000400\r\n
  
  ```

  *(Note: For negative temperatures, the minus sign appears immediately after `=`, e.g., `TEMP_X16=-000080\r\n` for* $-5.0^\circ\text{C}$*).*

* **Host Regex Guard & Conversion:**

  * Pattern: `re.compile(rb"QCM_FREQ=(\d+),TEMP_X16=(-?\d+)")`

  * Frequency ($f_{\text{qcm}}$ in $\text{Hz}$): `int(match.group(1))`

  * Crystal Holder Temperature ($T_{\text{qcm}}$ in $^\circ\text{C}$): `int(match.group(2)) / 16.0`

* **Watchdog Timeout:** $> 3.0\text{ s}$ without a valid regex-matched QCM ASCII line triggers `S4: FAULT`.

### Link B: ESP32 $\leftrightarrow$ Host PC (Bidirectional Link)

* **Physical Transport:** USB-over-UART Virtual COM Port, $115,200\text{ baud}$, `8N1`.

* **Downlink (`ESP32 -> PC`) Cadence & Multi-Rate Behavior:**

  * Transmits 1 ASCII line per second ($1\text{ Hz}$) from Core 0 (`R_SENSOR_STREAM`).

  * Omron D6F-V03A1 analog airflow is filtered and updated every $1\text{ s}$ (both `E2.1` and `E2.2`).

  * Sensirion SHT21 temperature and relative humidity are polled over $\text{I}^2\text{C}$ (`0x40`) only on **even seconds** (`E2.1: ACQUIRE_FRESH_SHT`, $0.5\text{ Hz}$ rate to prevent sensor self-heating) and cached in `sht_cache`; on **odd seconds** (`E2.2: ACQUIRE_HELD_SHT`), the held `sht_cache` values are transmitted.

* **Downlink ASCII Wire Format:**

  ```
  FLOW=1.250,TEMP=24.50,RH=42.10,PWM=50,FRESH=1,OK=1\r\n
  
  ```

* **Host Regex Guard & Extraction:**

  * Pattern: `re.compile(rb"FLOW=([\d.]+),TEMP=(-?[\d.]+),RH=([\d.]+),PWM=(\d+),FRESH=([01]),OK=([01])")`

  * Fields: `flow_lpm` (`float`), `sht_temp_c` (`float`), `sht_rh_pct` (`float`), `pwm_duty` (`int`, $0\text{--}100$), `fresh_flag` (`bool`, `1` = fresh `E2.1` poll, `0` = held `E2.2` cache), `health_ok` (`bool`, `1` = nominal).

* **Downlink Watchdog & ZOH Staleness Timeouts:**

  * Flow Rate Timeout / ZOH Staleness: $> 3.0\text{ s}$ triggers `S4: FAULT`.

  * SHT21 Fresh Update Timeout / ZOH Staleness: $> 6.0\text{ s}$ triggers `S4: FAULT`.

* **Uplink (`PC -> ESP32`) Command Format:**

  * Triggered when user adjusts the GUI Fan PWM slider in `HS_CONNECTED (S1/S2/S3)` (`S5: PWM FAN`).

  * Wire Format: `SET_PWM:<duty>\n` where $0 \le \text{duty} \le 100$ (e.g., `SET_PWM:75\n`).

  * Core 1 (`E1: UART_CMD_LISTEN`) parses the integer duty cycle, immediately updates the $25\text{ kHz}$ LEDC hardware PWM duty register, and updates `current_pwm`.

## 3. Finite State Machine (FSM) Specifications

### I. Host PC Finite State Machine (Master Orchestrator Control Plane)

The Host PC control plane is implemented as a declarative UML StateChart in [`software/fsm/host_fsm.py`](file:///software/fsm/host_fsm.py) using the **`python-statemachine`** library (`statemachine.StateChart` v3.2.2). It strictly isolates control-plane state transitions from data-plane mathematical operations ([`software/pipeline/zoh_fusion.py`](file:///software/pipeline/zoh_fusion.py)) and asynchronous serial I/O workers ([`software/readers/`](file:///software/readers/)).

#### Declarative UML StateChart Architecture (`HostStateMachine`)

The `HostStateMachine` subclasses `statemachine.StateChart` with `allow_event_without_transition = False`, enforcing strict Run-To-Completion (RTC) semantics with zero manual event queuing boilerplate:

1. **State Topology & Hierarchy:**
   * **Root State:** `s0_disconnected = State(HostState.S0_DISCONNECTED, initial=True, value=HostState.S0_DISCONNECTED)` represents uninitialized or parked hardware.
   * **Hierarchical OR-Superstate (`hs_connected`):** A `State.Compound` encompassing all operational states where serial handles are open and threads are running. Unhandled events in any substate bubble up to `hs_connected`.
     * **Deep History Pseudostate (`h`):** Defined as `h = HistoryState(type=HistoryType.DEEP)`. Natively stores and restores the active substate and orthogonal region configuration across transient interruptions.
     * **Standby Substate (`s1_idle`):** `State(HostState.S1_IDLE_STANDBY, initial=True, value=HostState.S1_IDLE_STANDBY)`. Serial workers are idling and streaming raw health frames before session start.
     * **Orthogonal AND-Superstate (`s2_tare_prime`):** `State.Parallel` with `value=HostState.S2_TARE_AND_PRIME`, containing two concurrent, independent compound regions:
       * **Aux Prime Region (`r_aux`):** `b1 = State(AuxPrimeRegion.B1_WAIT_AUX_FIRST, initial=True)` and `b2 = State(AuxPrimeRegion.B2_AUX_ZOH_PRIMED)`. Transition `aux_zoh_primed = b1.to(b2)` latches upon the first valid AUX record with `health_ok == True`.
       * **QCM Tare Region (`r_qcm`):** `a1 = State(QCMTareRegion.A1_WAIT_QCM_FIRST, initial=True)` and `a2 = State(QCMTareRegion.A2_QCM_TARE_READY)`. Transition `qcm_tare_ready = a1.to(a2)` latches upon the first valid QCM record.
     * **Active Streaming Substate (`s3_active`):** `State(HostState.S3_ACTIVE_STREAMING, value=HostState.S3_ACTIVE_STREAMING)`. Real-time $1\text{ Hz}$ Sauerbrey mass calculations, ZOH freshness evaluation, and CSV recording.
   * **Preemptive Fault State (`s4_fault`):** `State(HostState.S4_FAULT, value=HostState.S4_FAULT)`. Reached via group preemptive transition upon serial link failure, timeout, or sensor staleness. Hardware is parked and CSV is flushed.
   * **Transient Actuation State (`s5_pwm_fan`):** `State(HostState.S5_PWM_FAN, value=HostState.S5_PWM_FAN)`. Transient state for transmitting `SET_PWM:<duty>\n` uplink commands.

2. **Declarative Transitions & Event Triggers:**
   * `boot`: `s0_disconnected.to(hs_connected.s1_idle, cond=["guard_ports_available", "guard_open_hardware"])`
   * `start_run`: `hs_connected.s1_idle.to(hs_connected.s2_tare_prime, cond="guard_ports_healthy", on="do_begin_session")`
   * `join_completed`: `hs_connected.s2_tare_prime.to(hs_connected.s3_active, cond=["in_s2_a2", "in_s2_b2"], on="do_begin_streaming")`
   * `fan_pwm_changed`: `hs_connected.to(s5_pwm_fan, cond="guard_valid_duty", before="do_store_history", on="do_transmit_pwm")`
   * **Eventless Deep History Return:** `s5_pwm_fan.to(hs_connected.h, cond="can_return_from_s5")`. Fires immediately without external triggers, returning to the exact prior substate (including the simultaneous `r_aux` and `r_qcm` substate positions if interrupted during `S2`).
   * **Group Preemptive Exit (`fault`):** `s4_fault.from_(hs_connected, s5_pwm_fan, before="do_park_hardware", on="do_show_fault")`. Automatically preempts any active substate in `hs_connected` or `s5_pwm_fan`.
   * `fault_confirmed`: `s4_fault.to(s0_disconnected, before="do_clear_fault")`. Triggered when user confirms the modal fault alert.
   * `shutdown`: `(s0_disconnected.from_(hs_connected, s5_pwm_fan, s4_fault, before="do_terminate") | s0_disconnected.to.itself(internal=True, on="do_terminate"))`. Direct application termination from any state.

3. **Action Delegation Protocol (`HostActions`):**
   * The FSM contains zero hardware or GUI coupling. All external side effects and guards are defined in a typed `HostActions(Protocol)`:
     * Guards: `ports_available() -> bool`, `ports_healthy() -> bool`.
     * Lifecycle Actions: `open_hardware() -> bool`, `begin_session() -> None`, `begin_streaming(f0_hz: int, t0_c: float) -> None`, `transmit_pwm(duty: int) -> None`, `park_hardware(reason: str) -> None`, `show_fault(reason: str) -> None`, `clear_fault() -> None`, `terminate() -> None`.
   * `NullActions`: Default no-op implementation with permissive guards for dry runs and headless unit testing.
   * `HostController`: Implements `HostActions` directly in [`software/fsm/host_controller.py`](file:///software/fsm/host_controller.py), bridging the FSM with Qt serial workers, CSV logging, and GUI signals.

4. **Exception Handling & RTC Semantics:**
   * Calling a transition when invalid raises `statemachine.exceptions.TransitionNotAllowed`, cleanly caught by `HostController` public slots.
   * Action failures inside `hs_connected` automatically escalate to `fault(reason=...)` via `_run()`.
   * Transition progress is observed via `after_transition()`, which notifies listeners (`add_listener()`) and emits Qt signals (`state_changed`, `s2_progress`).

#### Host PC State Transition Table

| Current State | Trigger / Method | Guard Condition | Next State | Output / Action (`HostActions`) | 
| ----- | ----- | ----- | ----- | ----- | 
| **`S0: DISCONNECTED / BOOT`** | `boot()` | `ports_available` $\land$ `open_hardware` | `HS_CONNECTED` $\to$ `S1: IDLE / STANDBY` | Open serial handles; spawn and start `QCMReader` & `AuxReader` `QThread` workers. | 
| **`HS_CONNECTED (S1/S2/S3)`** | `fan_pwm_changed(duty)` | `guard_valid_duty` ($0 \le \text{duty} \le 100$) | `S5: PWM FAN` $\to$ `(H)` | `do_store_history`; transmit `SET_PWM:<duty>\n` via `transmit_pwm`; eventless return to `(H)`. | 
| **`S1: IDLE / STANDBY`** | `start_run()` | `ports_healthy` | `S2: TARE & PRIME` (`S2.A1`, `S2.B1`) | `do_begin_session`: purge input buffers on both ports, open session CSV, reset engine for tare. | 
| **`S2.A1: WAIT_QCM_FIRST`** | `qcm_tare_ready()` | First valid QCM packet received | `S2.A2: QCM_TARE_READY` | Latch tentative baseline frequency $f_0$ and crystal temperature $T_{\text{qcm}}(0)$; evaluate join. | 
| **`S2.B1: WAIT_AUX_FIRST`** | `aux_zoh_primed()` | First valid AUX packet $\land$ `health_ok == True` | `S2.B2: AUX_ZOH_PRIMED` | Prime `ZOHCache` (flow, temp, RH, PWM); evaluate join. | 
| **`S2: TARE & PRIME`** | `join_completed(f0_hz, t0_c)` | `in_s2_a2` $\land$ `in_s2_b2` | `S3: ACTIVE_STREAMING & ZOH_FUSION` | `do_begin_streaming`: freeze baseline in Sauerbrey engine, transition engine to streaming. | 
| **`S3: ACTIVE_STREAMING & ZOH_FUSION`** | QCM packet received | Fresh ZOH ($\text{Age}_{\text{flow}} \le 3\text{ s}$, $\text{Age}_{\text{sht}} \le 6\text{ s}$) | `S3: ACTIVE_STREAMING & ZOH_FUSION` | Data-plane loop: fuse with `ZOHCache`, compute $\Delta f_{\text{particle}}$ and $\frac{dm}{dt}$, emit `fused_row_ready`, log CSV. | 
| **`S3: ACTIVE_STREAMING & ZOH_FUSION`** | AUX packet received | Regex & values finite | `S3: ACTIVE_STREAMING & ZOH_FUSION` | Data-plane loop: refresh `ZOHCache` (flow, PWM; SHT temp/RH on fresh flag). | 
| **`HS_CONNECTED (S1/S2/S3)`** | `fault(reason)` / worker error / timeout | Link fault, watchdog timeout, or staleness | `S4: FAULT` | **Group Preemptive Exit:** `do_park_hardware` (invalidate cache, flush/close CSV, stop workers); `do_show_fault`. | 
| **`S4: FAULT`** | `fault_confirmed()` | Modal alert acknowledged | `S0: DISCONNECTED / BOOT` | `do_clear_fault`: clear fault reason and reset GUI to disconnected state. | 
| **`HS_CONNECTED / S4 / S5`** | `shutdown()` | App quit / SIGINT / SIGTERM | `S0: DISCONNECTED / BOOT` | Direct termination: `do_terminate` (`_release_all()`), close serial handles, exit cleanly. | 

### II. ESP32 Finite State Machine (Environmental & Actuator Node)

| Current State | Trigger / Event | Guard Condition | Next State | Output / Action | 
| ----- | ----- | ----- | ----- | ----- | 
| **`E0: BOOT`** | Power-on / Reset | Watchdog & peripherals OK | `HE_OPERATIONAL` (`E1: UART_CMD_LISTEN` $\parallel$ `E2: ACTIVE_MULTI_RATE`) | Init `UART0` ($115,200\text{ baud}$), `ADC1` ($11\text{ dB}$), $\text{I}^2\text{C}$ (`0x40`), $25\text{ kHz}$ LEDC PWM; set `current_pwm := 0`; spawn Core 1 & Core 0 tasks. | 
| **`E1: UART_CMD_LISTEN` (Core 1)** | UART RX: `SET_PWM:<d>\n` | $0 \le d \le 100$ | `E1: UART_CMD_LISTEN` (Core 1) | Mealy self-loop: update $25\text{ kHz}$ LEDC PWM duty register immediately; set shared `current_pwm := d`. | 
| **`E2.1: ACQUIRE_FRESH_SHT` (Core 0)** | `1s_Tick` | $1\text{ s}$ elapsed (even tick) | `E2.2: ACQUIRE_HELD_SHT` (Core 0) | Filter ADC flow; poll SHT21 over $\text{I}^2\text{C}$; update `sht_cache`; pack `current_pwm`; send ASCII line (`FRESH=1`). | 
| **`E2.2: ACQUIRE_HELD_SHT` (Core 0)** | `1s_Tick` | $1\text{ s}$ elapsed (odd tick) | `E2.1: ACQUIRE_FRESH_SHT` (Core 0) | Filter ADC flow; read held `sht_cache`; pack `current_pwm`; send ASCII line (`FRESH=0`). | 

### III. Basys 3 FPGA Finite State Machine (High-Speed Counter & Serializer)

| Current State | Trigger / Event | Guard Condition | Next State | Output / Action | 
| ----- | ----- | ----- | ----- | ----- | 
| **`F0: POWER_ON_RESET`** | Power-on | `rst = 0` (Power stable) | `F1: GATE_AND_I2C_POLL` | Clear `f_sense`, `gate_ctr`, and `temp_x16`; set `gate_enable <= 0`; reset UART TX registers. | 
| **`F1: GATE_AND_I2C_POLL`** | `+clk` edge ($100\text{ MHz}$) | `gate_ctr` within $1\text{ s}$ ($< 10^8\text{ ticks}$) | `F1: GATE_AND_I2C_POLL` | Increment `gate_ctr` and synchronized $5\text{ MHz}$ QCM pulse counter (`f_sense`); poll MCP9808 temperature over $\text{I}^2\text{C}$. | 
| **`F1: GATE_AND_I2C_POLL`** | `+clk` edge ($100\text{ MHz}$) | `gate_ctr` reaches $1\text{ s}$ ($10^8\text{ ticks}$) | `F2: CONVERT_TO_ASCII` | Latch $1\text{ s}$ frequency count and MCP9808 `TEMP_X16` reading; de-assert `gate_enable <= 0`. | 
| **`F2: CONVERT_TO_ASCII`** | ASCII conversion completes | Buffer formatted ($N\text{ chars}$) | `F3: UART_SEND_LINE` $\to$ `F3.1: LOAD_CHAR` | Format ASCII line (`QCM_FREQ=...,TEMP_X16=...\r\n`); initialize `char_idx <= 0`. | 
| **`F3.1: LOAD_CHAR`** | `+clk` edge ($100\text{ MHz}$) | `!uart_tx_busy` | `F3.2: WAIT_CHAR_DONE` | Load `tx_byte <= ascii_buf[char_idx]`; assert `uart_tx_start <= 1`. | 
| **`F3.2: WAIT_CHAR_DONE`** | `uart_tx_done` | `char_idx < N - 1` | `F3.1: LOAD_CHAR` | Increment `char_idx <= char_idx + 1` to load next ASCII character. | 
| **`F3: UART_SEND_LINE` (from `F3.2`)** | `uart_tx_done` | `char_idx == N - 1` | `F1: GATE_AND_I2C_POLL` | Reset `uart_tx_start <= 0`; clear `f_sense` and `gate_ctr`. | 

## 4. Repository Structure & Host PC File Breakdown

```
ece490-qcm-platform/
├── .gitignore
├── README.md
├── PROJECT_OVERVIEW.md
├── codebase_learning_guide.md
├── capstone_agentic_architecture_specification.md  # System Architecture & Agentic Development Specification
├── requirements.txt                    # PyQt6, pyqtgraph, pyserial, numpy, pandas, scipy, pytest, python-statemachine
│
├── data/                               # Session CSV storage directory (anchored at code/data/sessions)
│   └── sessions/
│
├── software/                           # Host PC Master Orchestrator & Analytics Stack
│   ├── __init__.py
│   ├── fsm/                            # Declarative Control Plane & StateChart Engine
│   │   ├── __init__.py
│   │   ├── host_fsm.py                 # Declarative UML StateChart (HostStateMachine via python-statemachine)
│   │   ├── host_controller.py          # Unified HostController (QObject), slots, signal wiring & actions
│   │   └── headless.py                 # Headless runner for automated testing & CI without GUI
│   ├── gui/                            # Presentation Layer & GUI Widgets
│   │   ├── .gitkeep
│   │   ├── __init__.py
│   │   ├── app.py                      # Application entry point, CLI parser, signal handling & main window launch
│   │   ├── main_window.py              # Master GUI window, views coordination, fault modal handling
│   │   ├── views/
│   │   │   ├── __init__.py
│   │   │   ├── time_series_view.py     # 60 FPS pyqtgraph waveforms (f_qcm, delta_f, dm/dt, Temp)
│   │   │   └── diagnostic_view.py      # Telemetry readouts, status badges, Start Run, Fan PWM slider
│   │   └── widgets/
│   │       ├── __init__.py
│   │       └── status_indicator.py     # LED state badges (S0-S5, S2.A2/S2.B2 locks, ZOH health)
│   │
│   ├── readers/                        # Asynchronous UART-over-USB Ingestion Workers
│   │   ├── .gitkeep
│   │   ├── __init__.py
│   │   ├── serial_worker.py            # Base QThread pyserial worker, TX queue, purge & fault handling
│   │   ├── qcm_reader.py               # 1 Hz Basys 3 FPGA ASCII line parser & 3.0s watchdog
│   │   ├── aux_reader.py               # 1 Hz ESP32 ASCII line parser, 3.0s/6.0s watchdogs & SET_PWM TX
│   │   └── smps_adapter.py             # Reference TSI-AIM / SMPS delimited file watcher & scan parser
│   │
│   ├── pipeline/                       # Priming, Multi-Rate Data Fusion, Math & Storage Engine
│   │   ├── .gitkeep
│   │   ├── __init__.py
│   │   ├── zoh_fusion.py               # S2 AND-superstate priming latch, S3 ZOH fusion & engine phases
│   │   ├── sauerbrey.py                # Baseline tare freeze, delta_f & Sauerbrey dm/dt derivative math
│   │   ├── csv_logger.py               # Session CSV writer (open on S1->S2, log in S3, flush on S4/S0)
│   │   ├── quality.py                  # Standalone regex validators, health flag checks, ZOH age evaluators
│   │   └── empirical_fit.py            # Integral dose transform (D_SMPS) & K_empirical solver
│   │
│   └── tests/                          # Hardware-Decoupled Emulation & Verification Suite
│       ├── .gitkeep
│       ├── __init__.py
│       ├── conftest.py                 # Shared pytest fixtures (qapp, pump, wait_until, waiter)
│       ├── serial_mock.py              # Synthetic 1 Hz FPGA & ESP32 ASCII generators (--mock mode)
│       ├── virtual_loopback.py         # Virtual serial pair orchestrator (InProcessLoopbackPair / pty / com0com)
│       ├── test_host_fsm.py            # Pure-Python unit tests for HostStateMachine (37 tests)
│       └── test_pipeline.py            # Integration tests for pipeline, mock readers & controller (56 tests)
│
├── firmware/
│   ├── fpga/                           # Basys 3 Artix-7 Verilog RTL & XDC constraints (.gitkeep)
│   └── esp32/                          # ESP-IDF / Arduino Dual-Core C++ firmware (.gitkeep)
├── hardware/
│   ├── 3d_models/                      # 3D chamber/fixture CAD models (.gitkeep)
│   ├── pcb/                            # Altium PCB layout files (.gitkeep)
│   └── schematics/                     # Altium schematics (.gitkeep)
└── docs/                               # Interface Control Docs & calibration manifests (.gitkeep)
```

### Detailed Host PC Module & Class Specifications

#### A. `/software/fsm/` — Master Control Plane & Declarative StateChart

1. **`software/fsm/host_fsm.py`**

   * `class HostState(Enum)`: Canonical states `S0_DISCONNECTED`, `S1_IDLE_STANDBY`, `S2_TARE_AND_PRIME`, `S3_ACTIVE_STREAMING`, `S4_FAULT`, `S5_PWM_FAN`. Includes `.label` and `.short` helper properties.

   * `class QCMTareRegion(Enum)` & `class AuxPrimeRegion(Enum)`: Substate enums for orthogonal regions `S2.A1`/`S2.A2` and `S2.B1`/`S2.B2`.

   * `class HostActions(Protocol)`: Typed protocol defining hardware guards (`ports_available`, `ports_healthy`) and external lifecycle actions (`open_hardware`, `begin_session`, `begin_streaming`, `transmit_pwm`, `park_hardware`, `show_fault`, `clear_fault`, `terminate`).

   * `class NullActions(HostActions)`: Default permissive implementation used for dry runs and unit testing.

   * `class HostStateMachine(StateChart)`: Declarative UML StateChart built with `python-statemachine` (v3.2.2):
     * State Hierarchy: Root state `s0_disconnected`, compound OR-superstate `hs_connected` (containing `h = HistoryState(type=HistoryType.DEEP)`, `s1_idle`, parallel AND-superstate `s2_tare_prime` with concurrent compound regions `r_aux` and `r_qcm`, and `s3_active`), `s4_fault`, and `s5_pwm_fan`.
     * Public Declarative Transitions: `boot`, `start_run`, `join_completed`, `fan_pwm_changed`, eventless history return `s5_pwm_fan.to(hs_connected.h)`, group preemptive `fault`, `fault_confirmed`, and `shutdown`.
     * Orthogonal Substate Transitions: `r_qcm.qcm_tare_ready` (`a1.to(a2)`) and `r_aux.aux_zoh_primed` (`b1.to(b2)`).
     * Guards & Action Hooks: `guard_ports_available`, `guard_open_hardware`, `guard_ports_healthy`, `guard_valid_duty`, `can_return_from_s5`, `do_store_history`, `do_begin_session`, `do_begin_streaming`, `do_transmit_pwm`, `do_park_hardware`, `do_show_fault`, `do_clear_fault`, `do_terminate`.
     * Public Query Properties: `state` (`HostState`), `history_state` (`Optional[HostState]`), `is_connected` (`bool`), `in_s2_a2` (`bool`), `in_s2_b2` (`bool`), `qcm_region` (`Optional[QCMTareRegion]`), `aux_region` (`Optional[AuxPrimeRegion]`), `active_configuration` (`str`), and `state_path()` (`list[HostState]`).
     * Transition Listeners: `after_transition()` hook tracking state history and dispatching to registered callback listeners.

2. **`software/fsm/host_controller.py`**

   * `class HostConfig` (Dataclass): Configuration parameters including `mock: bool`, `qcm_port: Optional[str]`, `aux_port: Optional[str]`, `baud: int`, `log_dir: str`, `flow_timeout_s: float`, `sht_timeout_s: float`, `qcm_timeout_s: float`.

   * `class HostController(QObject)`: Central orchestrator unifying control plane, data plane, serial workers, and CSV logging:
     * Control-Plane Signals: `state_changed(object)`, `s2_progress(bool, bool)`, `connection_failed(str)`, `session_started(str)`, `fault_raised(str)`, `fault_cleared()`.
     * Data-Plane Signals: `fused_row_ready(dict)`.
     * Public Qt Slots: `connect_ports() -> bool`, `start_run() -> bool`, `set_fan_pwm(duty: int) -> bool`, `on_fault_triggered(error_msg: str) -> None`, `confirm_fault() -> bool`, `shutdown() -> None`. Each slot uses `_dispatch` to safely handle `statemachine.exceptions.TransitionNotAllowed`.
     * Worker Lifecycle & Filtering: `_bind_workers()` wires worker signals, `_teardown_workers()` safely joins threads and closes ports, and `_is_current_worker()` filters out late signals from previously torn-down workers.
     * `HostActions` Implementation: `HostController` implements `HostActions` directly (`self.fsm = HostStateMachine(actions=self)`), handling port opening, buffer purging, CSV initialization, PWM transmission, and hardware parking without an intermediate proxy class.

3. **`software/fsm/headless.py`**

   * `parse_arguments() -> argparse.Namespace`: Parses CLI options (`--mock`, `--qcm-port`, `--aux-port`, `--baud`, `--log-dir`, `--duration`, `--pwm <DUTY@SECONDS>`, `-v/--verbose`).
   * Imports `configure_signal_handling` directly from `software.fsm.host_controller`.
   * `main(argv) -> int`: Headless execution loop running on `QCoreApplication` without GUI overhead; coordinates full run lifecycle, schedules Fan PWM steps, auto-acknowledges faults, and returns deterministic status exit codes.

#### B. `/software/gui/` — Presentation Layer & GUI Widgets

1. **`software/gui/app.py`**

   * `parse_arguments() -> argparse.Namespace`: Parses GUI arguments (`--mock`, `--qcm-port`, `--aux-port`, `--baud`, `--log-dir` defaulting to `DEFAULT_LOG_DIR` anchored at `code/data/sessions`).
   * Imports `configure_signal_handling` directly from `software.fsm.host_controller`.
   * `main(argv) -> int`: Application entry point instantiating `HostController` and `MainWindow`, configuring OS signal handling, connecting ports, and running the Qt event loop.

2. **`software/gui/main_window.py`**

   * `class MainWindow(QMainWindow)`:
     * Observes `HostController` transitions via `state_changed` and `s2_progress` signals; receives active `HostState` directly.
     * Synchronizes `StatusIndicator` LED badges (including orthogonal `in_s2_a2` and `in_s2_b2` locks).
     * Updates 60 FPS pyqtgraph subplots upon `fused_row_ready`.
     * Manages modal `QMessageBox.critical` on fault and acknowledges via `controller.confirm_fault()`.

3. **`software/gui/views/diagnostic_view.py` & `time_series_view.py`**

   * `class DiagnosticView(QWidget)`: Telemetry and actuator panel rendering `StatusIndicator`, "Start Run" button, 0–100% Fan PWM `QSlider`, and digital readouts for D6F flow rate ($\text{L/min}$), SHT21 RH ($\%$), and ZOH age indicators. *(Note: COM ports are configured via CLI / `HostConfig` rather than GUI dropdowns).*
   * `class TimeSeriesView(QWidget)`: Manages 4 synchronized `pyqtgraph.PlotWidget` subplots:
     1. Raw QCM Frequency $f_{\text{qcm}}(t)$ ($\text{Hz}$)
     2. Tared Frequency Shift $\Delta f_{\text{particle}}(t)$ ($\text{Hz}$)
     3. Sauerbrey Mass Deposition Rate $\frac{dm}{dt}$ ($\text{ng/s}$)
     4. Crystal Holder Temperature $T_{\text{qcm}}(t)$ vs. SHT21 Chamber Temperature $T_{\text{sht}}(t)$ ($^\circ\text{C}$)

4. **`software/gui/widgets/status_indicator.py`**

   * `class StatusIndicator(QWidget)`: LED state badges (`S0`–`S5`), dual `S2` lock badges (`QCM Tare`, `AUX Prime`), and ZOH cache health indicators.

#### C. `/software/readers/` — Asynchronous Serial Ingestion Workers

1. **`software/readers/serial_worker.py`**

   * `class BaseSerialWorker(QThread)`:
     * Signals: `serial_fault = pyqtSignal(str)` and `bytes_transmitted = pyqtSignal(bytes)`.
     * Thread-Safe GUI Methods: `open_port() -> bool`, `purge_input_buffer() -> None` (sets `_purge_evt` for worker loop to execute `reset_input_buffer()`), `enqueue_tx(data: bytes) -> None` (appends bytes to internal `queue.Queue`), and `stop_and_close() -> None`.
     * Worker Execution Loop (`run()`): All `readline()`, `write()`, and buffer purges execute inside the worker thread. Drains TX queue via `_drain_tx()` emitting `bytes_transmitted`, enforces `MAX_LINE_BYTES = 4096` buffer protection, and polls watchdogs at $\ge 10\text{ Hz}$.

2. **`software/readers/qcm_reader.py`**

   * `class QCMReader(BaseSerialWorker)`:
     * Signal: `qcm_packet_received = pyqtSignal(float, int, float)` (`pc_timestamp`, `qcm_freq_hz`, `qcm_temp_c`).
     * Parser: Standalone/static parser `parse_ascii_line(line: bytes)` delegating to `quality.parse_qcm_line()`.
     * Watchdog: Emits `serial_fault("QCM timeout > 3.0s")` if elapsed time since last valid packet exceeds $3.0\text{ s}$.

3. **`software/readers/aux_reader.py`**

   * `class AuxReader(BaseSerialWorker)`:
     * Signal: `aux_packet_received = pyqtSignal(float, dict)` (`pc_timestamp`, `aux_dict`).
     * Parser: Standalone/static parser `parse_ascii_line(line: bytes)` delegating to `quality.parse_aux_line()`.
     * Watchdogs: Emits `serial_fault` if flow packet interval $> 3.0\text{ s}$ or fresh SHT21 (`FRESH=1`) interval $> 6.0\text{ s}$.
     * Actuation Uplink: `send_pwm_command(duty: int) -> None` formats `SET_PWM:<duty>\n` and calls `enqueue_tx()` without clearing the RX input buffer.

4. **`software/readers/smps_adapter.py`**

   * Watcher and parser for TSI-AIM reference SMPS scan exports.

#### D. `/software/pipeline/` — Priming, ZOH Fusion & Mathematical Engine

1. **`software/pipeline/zoh_fusion.py`**

   * `class ZOHCache` (Dataclass): Holds `flow_lpm: float`, `sht_temp_c: float`, `sht_rh_pct: float`, `pwm_duty: int`, `last_flow_ts: float`, `last_sht_ts: float`, `is_valid: bool = False`, and `health_ok: bool = False`. Provides `prime()`, `refresh()`, and `invalidate()`.
   * `class FusionMode(Enum)` / Engine Phase: `DISARMED` (S0/S1/S4), `PRIMING` (S2), `STREAMING` (S3).
   * `class ZOHFusionEngine(QObject)`:
     * Signals: `qcm_tare_ready = pyqtSignal(int, float)`, `aux_zoh_primed = pyqtSignal(dict)`, `s2_join_complete = pyqtSignal(int, float)`, `fused_row_ready = pyqtSignal(dict)` (includes `"aux_health_ok": cache.health_ok`), `staleness_fault = pyqtSignal(str)`.
     * Latches `in_s2_a2` and `in_s2_b2` independently during `PRIMING`. At join completion, freezes baseline in `SauerbreyCalculator` and transitions to `STREAMING`.
     * Evaluates ZOH staleness on every incoming QCM packet ($\text{Age}_{\text{flow}} \le 3.0\text{ s}$, $\text{Age}_{\text{sht}} \le 6.0\text{ s}$) before computing Sauerbrey derivatives.

2. **`software/pipeline/sauerbrey.py`**

   * `class SauerbreyCalculator`: Computes baseline subtraction $\Delta f_{\text{particle}}$, finite-difference derivative $\frac{df}{dt}$, and mass deposition rate $\frac{dm}{dt}$ using calibrated crystal constant $C_{\text{QCM}} = 0.0567\text{ ng}\cdot\text{cm}^{-2}\cdot\text{Hz}^{-1}$.

3. **`software/pipeline/csv_logger.py`**

   * `class CSVLogger`: Opens session CSV (`session_YYYYMMDD_HHMMSS.csv`) inside `DEFAULT_LOG_DIR` (anchored to `code/data/sessions/` via `Path(__file__).resolve().parents[2] / "data" / "sessions"`) on `S1 -> S2`, appends $1\text{ Hz}$ fused rows in `S3`, and safely flushes and closes file handles on `S4: FAULT` or application shutdown.

4. **`software/pipeline/quality.py`**

   * Pure-Python wire validators and regex matchers: `parse_qcm_line()`, `parse_aux_line()`, `is_aux_health_ok()`, `is_valid_pwm_duty()`, and timing invariants (`QCM_TIMEOUT_S = 3.0`, `FLOW_TIMEOUT_S = 3.0`, `SHT_TIMEOUT_S = 6.0`).

5. **`software/pipeline/empirical_fit.py`**

   * Integrates airflow-normalized SMPS dose $D_{\text{SMPS}}(t)$ and solves for empirical scaling factor $K_{\text{empirical}}$.

#### E. `/software/tests/` — Hardware-Decoupled Verification Suite

1. **`software/tests/conftest.py`**

   * Session-scoped `qapp` fixture (`QCoreApplication` / `QApplication` with offscreen platform), `pump(app, seconds)` event processor, and `wait_until(app, predicate, timeout)` helper for robust asynchronous testing.

2. **`software/tests/serial_mock.py`**

   * Synthetic data sources (`SyntheticFPGASource`, `SyntheticESP32Source`) and mock reader classes (`MockQCMReader`, `MockAuxReader`) generating authentic $1\text{ Hz}$ ASCII lines with noise and simulated aerosol deposition ramps.

3. **`software/tests/virtual_loopback.py`**

   * Multi-backend loopback testing framework:
     * `class InProcessLoopbackPair`: Pure in-memory cross-wired byte pipes (`_BytePipe` / `LoopbackSerialEndpoint`) emulating full pyserial stream behavior and disconnect exceptions across all OS platforms without kernel drivers.
     * `class VirtualLoopbackHarness`: End-to-end loopback orchestrator running simulated FPGA and ESP32 device threads against `QCMReader` and `AuxReader`.
     * `class OSSerialPair`: OS-level virtual serial port support (POSIX `openpty` or Windows `com0com`).

4. **`software/tests/test_host_fsm.py`**

   * 37 pure-Python unit tests covering declarative `HostStateMachine(StateChart)` transitions (`boot`, `start_run`, `join_completed`, `fan_pwm_changed`, `fault`, `fault_confirmed`, `shutdown`), guard condition enforcement, `TransitionNotAllowed` assertions, deep history restoration, and action error escalation.

5. **`software/tests/test_pipeline.py`**

   * 56 full-stack integration tests covering wire parsing, ZOH latching, Sauerbrey calculations, virtual loopback communication, `HostController`, and `MainWindow` UI updates.

## 5. Mathematical & Signal Processing Formulations

### 1. Single-Crystal Baseline Tare (`S2 -> S3` Join Action)

When the `S2` join condition `in(S2.A2) && in(S2.B2)` is met at $t = t_0$, `sauerbrey.py` freezes the initial baseline frequency and crystal holder temperature:

$$
f_0 = f_{\text{qcm}}(t_0), \quad T_0 = T_{\text{qcm}}(t_0)
$$

### 2. Net Frequency Shift & Time Derivative (`S3` Active Streaming)

For each subsequent $1\text{ Hz}$ QCM sample at host monotonic timestamp $t_k$:

$$
\Delta f_{\text{particle}}(t_k) = f_{\text{qcm}}(t_k) - f_0
$$


The discrete rate of frequency change is normalized by the exact elapsed host timestamp delta $\Delta t_k = t_k - t_{k-1}$ (nominally $1.0\text{ s}$):

$$
\frac{df}{dt}\bigg\vert{}_{t_k} = \frac{\Delta f_{\text{particle}}(t_k) - \Delta f_{\text{particle}}(t_{k-1})}{t_k - t_{k-1}}
$$

### 3. Sauerbrey Mass Deposition Rate ($\frac{dm}{dt}$)

Using the standard Sauerbrey relation for an AT-cut quartz crystal with fundamental resonant frequency $f_0 \approx 5.0 \times 10^6\text{ Hz}$, active electrode area $A = 0.205\text{ cm}^2$, quartz density $\rho_q = 2.648\text{ g/cm}^3$, and shear modulus $\mu_q = 2.947 \times 10^{11}\text{ g}\cdot\text{cm}^{-1}\cdot\text{s}^{-2}$:

$$
\Delta m(t_k) = -\left(\frac{A\sqrt{\rho_q \mu_q}}{2 f_0^2}\right) \Delta f_{\text{particle}}(t_k) = -C_{\text{QCM}} \cdot \Delta f_{\text{particle}}(t_k)
$$

$$
\frac{dm}{dt}\bigg\vert{}_{t_k} = -C_{\text{QCM}} \cdot \frac{df}{dt}\bigg\vert{}_{t_k} \quad [\text{ng/s}]
$$

### 4. SMPS Reference Dose Alignment (`empirical_fit.py`)

Because the QCM is an integrating cumulative mass transducer ($\Delta f \propto \Delta m$) whereas the reference Scanning Mobility Particle Sizer (SMPS) measures instantaneous volumetric concentration $C_{\text{SMPS}}(t)$ ($\mu\text{g/m}^3$), `empirical_fit.py` aligns the domains via an **Integral Dose Transform** over the SMPS scan window $[t_{\text{start}}, t_{\text{end}}]$:

$$
D_{\text{SMPS}}(t) = \int_{t_{\text{start}}}^{t} Q(\tau) \cdot C_{\text{SMPS}}(\tau)\,d\tau
$$


where $Q(\tau)$ is the ZOH-held volumetric airflow rate from the Omron D6F sensor, solving for $K_{\text{empirical}}$ via linear least squares:

$$
\vert{}\Delta f_{\text{particle}}(t)\vert{} = K_{\text{empirical}} \cdot D_{\text{SMPS}}(t)
$$

### 5. Unified Session CSV Schema (`csv_logger.py`)

Opened upon `S1 -> S2` transition (`session_YYYYMMDD_HHMMSS.csv` inside `data/sessions/` anchored to `code/data/sessions/`) and flushed/closed on `S4: FAULT` or `S0` exit:

```
pc_timestamp_s,qcm_freq_hz,delta_f_hz,df_dt_hz_s,dm_dt_ng_s,qcm_temp_c,flow_lpm,sht_temp_c,sht_rh_pct,pwm_duty,zoh_flow_age_s,zoh_sht_age_s

```

## 6. Hardware-Decoupled Simulation & Verification (`--mock` & Loopback)

Agents and developers must verify all Host PC functionality without physical sensors using three built-in simulation and loopback tiers:

### Tier 1: In-Process `--mock` Mode (`software/tests/serial_mock.py`)

Running `python -m software.gui.app --mock` substitutes physical `pyserial` handles with synthetic `QThread` generators (`MockQCMReader`, `MockAuxReader`) that emit exact $1\text{ Hz}$ ASCII lines:

* **Synthetic FPGA Stream:** Starts at $5,000,000\text{ Hz}$ with $\mathcal{N}(0, 0.8\text{ Hz})$ Gaussian noise, applies a $-3.5\text{ Hz/s}$ deposition ramp between $t = 10\text{ s}$ and $t = 70\text{ s}$, and formats `QCM_FREQ=0005000123,TEMP_X16=000400\r\n`.

* **Synthetic ESP32 Stream:** Alternates `FRESH=1` (even ticks, `E2.1`) and `FRESH=0` (odd ticks, `E2.2`) SHT21 values every $1\text{ s}$, scales simulated D6F flow rate with `current_pwm`, and parses incoming `SET_PWM:<duty>\n` commands.

### Tier 2: Virtual Serial Port Loopback (`software/tests/virtual_loopback.py`)

Tests the full `pyserial` OS kernel buffer stack (`reset_input_buffer()`, port disconnects, `SerialException`):

1. **Cross-Platform In-Process Loopback (`InProcessLoopbackPair` & `VirtualLoopbackHarness`):**
   * Default backend for unit and integration testing.
   * Employs cross-wired thread-safe in-memory byte streams (`_BytePipe` / `LoopbackSerialEndpoint`) that replicate `pyserial` semantics (`readline`, `read`, `write`, `in_waiting`, `reset_input_buffer`, `simulate_disconnect`) on Windows, macOS, and Linux without requiring OS driver installations or administrator privileges.

2. **OS-Level Virtual COM Ports:**
   * **Linux / macOS (`socat` / `pty`):**
     ```bash
     socat -d -d pty,raw,echo=0,link=/tmp/ttyV_QCM_TX pty,raw,echo=0,link=/tmp/ttyV_QCM_RX &
     socat -d -d pty,raw,echo=0,link=/tmp/ttyV_AUX_TX pty,raw,echo=0,link=/tmp/ttyV_AUX_RX &
     python -m software.tests.serial_mock --qcm-tx /tmp/ttyV_QCM_TX --aux-tx /tmp/ttyV_AUX_TX &
     python -m software.gui.app --qcm-port /tmp/ttyV_QCM_RX --aux-port /tmp/ttyV_AUX_RX
     ```
   * **Windows (`com0com`):** Pair `COM10 <-> COM11` (FPGA) and `COM12 <-> COM13` (ESP32).

### Tier 3: Headless CLI Verification (`software.fsm.headless`)

Enables automated testing, CI/CD validation, and long-duration benchmarking without GUI display server requirements:

```bash
python -m software.fsm.headless --mock --duration 30 --pwm 75@12
```

Validates automated connection (`S0 -> S1`), session start (`S1 -> S2`), orthogonal priming join (`S2 -> S3`), streaming row telemetry logging, scheduled Fan PWM actuation (`S5 -> H`), and clean exit on timeout or auto-confirmed fault.

## 7. Agentic Implementation Roadmap & Acceptance Tests

When implementing or modifying the `/software` stack, coding agents must execute tasks in the following dependency order and run the respective test suites after each step:

1. **Step 1 — Pipeline Data Structures & Math (`software/pipeline/`):**
   * Implement `quality.py`, `sauerbrey.py`, `zoh_fusion.py`, and `csv_logger.py`.
   * *Acceptance Test:* `pytest software/tests/test_pipeline.py` verifies that `ZOHFusionEngine` blocks `S3` entry until both `S2.A2` and `S2.B2` latch, accurately computes $\Delta f_{\text{particle}}$ and $\frac{dm}{dt}$, and emits `staleness_fault` when $\text{Age}_{\text{flow}} > 3.0\text{ s}$ or $\text{Age}_{\text{sht}} > 6.0\text{ s}$.

2. **Step 2 — Asynchronous Serial Workers (`software/readers/`):**
   * Implement `serial_worker.py`, `qcm_reader.py`, and `aux_reader.py`.
   * *Acceptance Test:* `pytest` verifies regex parsing of positive and negative `TEMP_X16` values (`-000080` $\to -5.0^\circ\text{C}$), non-blocking worker thread execution, thread-safe TX queuing with `bytes_transmitted` emission, and $3.0\text{ s}$ / $6.0\text{ s}$ watchdog fault emission.

3. **Step 3 — Declarative UML StateChart Control Plane (`software/fsm/`):**
   * Implement `host_fsm.py` (`HostStateMachine` using `statemachine.StateChart`), `host_controller.py` (`HostController`), and `headless.py`.
   * *Acceptance Test:* `pytest software/tests/test_host_fsm.py` (37/37 passing) verifies declarative state transitions, `TransitionNotAllowed` rejection, orthogonal region concurrency, Deep History restoration on `S5_PWM_FAN` return, action delegation via `HostActions(Protocol)`, and fault escalation.

4. **Step 4 — Mock Generators & Virtual Loopback Harness (`software/tests/`):**
   * Implement `conftest.py`, `serial_mock.py`, `virtual_loopback.py` (`InProcessLoopbackPair`, `VirtualLoopbackHarness`), and `test_pipeline.py`.
   * *Acceptance Test:* Full integration test suite passes (56/56 passing) verifying end-to-end loopback data flow, session CSV logging, and fault propagation across virtual ports.

5. **Step 5 — PyQt6 GUI & Presentation Layer (`software/gui/`):**
   * Implement `status_indicator.py`, `time_series_view.py`, `diagnostic_view.py` (without COM port dropdowns), `main_window.py`, and `app.py`.
   * *Acceptance Test:* Run full test suite `pytest` (all 93 tests pass). Verify live GUI smoke test with `python -m software.gui.app --mock` (60 FPS plotting, S1-S3 transitions, S5 Fan PWM slider, modal fault popup recovery) and headless smoke test with `python -m software.fsm.headless --mock --duration 5 --pwm 75@2`.