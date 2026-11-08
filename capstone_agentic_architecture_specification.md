# ECE 490 Capstone: System Architecture & Agentic Development Specification

> **Project:** FPGA-Based Resonant Mass Sensing Platform for Ultrafine Particulate Matter Detection ($< 0.25\ \mu\text{m}$)
>
> **Architecture Revision:** (3-2) Always-On Dual-UART ASCII Streaming with Host-Gated Logging
>
> **Target Environment:** Autonomous Coding Agents (Claude Code, Google Antigravity, OpenAI Codex) & Human Developers

## 1. System Topology & Core Engineering Invariants

The platform operates as three physically decoupled, communicating Finite State Machines (CFSMs)—the **Host PC**, the **Espressif ESP32**, and the **Digilent Basys 3 Artix-7 FPGA**—connected via two independent USB-over-UART physical links at $115,200\text{ baud}$ (`8N1`).

```
+-----------------------------------------------------------------------------------+
|                        AEROSOL MEASUREMENT CHAMBER / DUCT                         |
|                                                                                   |
|  +-----------------------------+               +-------------------------------+  |
|  | 5 MHz QCM Crystal (openQCM) |               | Omron D6F-V03A1 Airflow (ADC) |  |
|  | + MCP9808 Temp Sensor (I2C) |               | Sensirion SHT21 Temp/RH (I2C) |  |
|  +--------------+--------------+               | 5V Chamber Fan (25 kHz PWM)   |  |
|                 |                              +---------------+---------------+  |
+-----------------|----------------------------------------------|------------------+
                  | Pmod JA (CMOS + I2C)                         | ADC1 / I2C / LEDC
                  v                                              v
     +--------------------------+                   +--------------------------+
     |      BASYS 3 FPGA        |                   |    ESP32 MICROCONTROLLER |
     |  Free-Running TX-Only    |                   |   Dual-Core Always-On    |
     |  1 Hz Gate & Serializer  |                   |  Core 0: 1 Hz Multi-Rate |
     |  (F0 -> F1 -> F2 -> F3)  |                   |  Core 1: RX PWM Listener |
     +------------+-------------+                   +------------+-------------+
                  |                                              |
                  | USB-UART Link A                              | USB-UART Link B
                  | 115,200 baud, 8N1 (TX-Only)                  | 115,200 baud, 8N1 (Bidir)
                  | 1 Hz ASCII: QCM_FREQ, TEMP_X16               | 1 Hz ASCII: FLOW, TEMP, RH, PWM
                  |                                              | Uplink: SET_PWM:<duty>\n
                  v                                              v
+-----------------------------------------------------------------------------------+
|                       HOST PC (MASTER ORCHESTRATOR & GUI)                         |
|                                                                                   |
|  +----------------------------+                 +------------------------------+  |
|  | QCMReader (QThread Worker) |                 | AuxReader (QThread Worker)   |  |
|  | Regex Parser & 3s Watchdog |                 | Regex Parser, 3s/6s Watchdog |  |
|  +--------------+-------------+                 +--------------+---------------+  |
|                 | pyqtSignal(ts, freq, temp)                   | pyqtSignal(ts, dict)
|                 +----------------------+-----------------------+                  |
|                                        v                                          |
|                 +----------------------------------------------+                  |
|                 | ZOHFusionEngine & SauerbreyCalculator        |                  |
|                 | - S2: Orthogonal Tare & ZOH Prime Join       |                  |
|                 | - S3: 1 Hz ZOH Fusion, delta_f, dm/dt        |                  |
|                 +----------------------+-----------------------+                  |
|                                        | pyqtSignal(fused_dict)                   |
|                         +--------------+--------------+                           |
|                         v                             v                           |
|          +-----------------------------+ +---------------------------+            |
|          | MainWindow (PyQt6/pyqtgraph)| | CSVLogger (Session Store) |            |
|          | 60 FPS Plots & S0-S5 FSM    | | Flush/Close on S4 or Exit |            |
|          +-----------------------------+ +---------------------------+            |
+-----------------------------------------------------------------------------------+

```

### Strict Architectural Guardrails for Coding Agents

1. **No Start/Stop Handshakes on Serial Links:** Both the Basys 3 FPGA and the ESP32 are **always-on** devices that autonomously stream $1\text{ Hz}$ newline-delimited ASCII records from power-on. Do **not** implement `0x01`/`0x02`, `START\n`/`STOP\n`, or `0x80` (`LAST_FRAME`) draining states (`S4: DRAINING` is deprecated and removed).

2. **Single-Crystal QCM Compensation:** The FPGA counts a single $5\text{ MHz}$ QCM oscillator (`QCM_FREQ`) over a $1\text{ s}$ gate and polls an on-holder MCP9808 temperature sensor (`TEMP_X16`) over $\text{I}^2\text{C}$. Do **not** expect dual-crystal (`f_sense`, `f_ref`) binary packets or 20-byte/23-byte binary struct framing.

3. **Non-Blocking GUI Thread:** All `pyserial` operations (`readline()`, `write()`, `reset_input_buffer()`) **must** execute inside dedicated `QThread` workers (`qcm_reader.py`, `aux_reader.py`). Communication with the GUI (`main_window.py`) and pipeline (`zoh_fusion.py`) must use thread-safe `pyqtSignal` and `pyqtSlot` connections.

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

### I. Host PC Finite State Machine (Master Orchestrator)

The Host PC state machine uses hierarchical **OR-Superstates** (`HS_CONNECTED: SERIAL_CONNECTED`), **AND-Superstates** (`S2: TARE_AND_PRIME` with orthogonal regions `S2.1` and `S2.2`), and a **History Pseudostate (`H`)** for non-intrusive PWM fan actuation.

```
                        +-------------------------------+
                        |   S0: DISCONNECTED / BOOT     |
                        +-------+---------------+-------+
                                |               ^
                   program boot |               | program shutoff /
            [ports healthy]     |               | [confirm GUI fault pop-up]
                                v               |
+-------------------------------+---------------+-----------------------------------+
| HS_CONNECTED: SERIAL_CONNECTED (OR-SUPERSTATE)                                    |
|                                                                                   |
|  +-------+            +-------------------------------+                           |
|  |  (H)  |            |      S1: IDLE / STANDBY       |                           |
|  +---+---+            +---------------+---------------+                           |
|      ^                                |                                           |
|      |                                | user clicks Start [healthy ports] /       |
|      |                                | reset_input_buffer(), open CSV,           |
|      |                                | start serial readers                      |
|      |                                v                                           |
|      |        +-------------------------------------------------------+           |
|      |        | S2: TARE_AND_PRIME (AND-SUPERSTATE) / open CSV        |           |
|      |        |---------------------------+---------------------------|           |
|      |        | S2.1: R_AUX_PRIME (1 Hz)  | S2.2: R_QCM_TARE (1 Hz)   |           |
|      |        |                           |                           |           |
|      |        |  +---------------------+  |  +---------------------+  |           |
|      |        |  | S2.B1:              |  |  | S2.A1:              |  |           |
|      |        |  | WAIT_AUX_FIRST      |  |  | WAIT_QCM_FIRST      |  |           |
|      |        |  +----------+----------+  |  +----------+----------+  |           |
|      |        |             | Valid AUX   |             | Valid QCM   |           |
|      |        |             | [Regex &    |             | [Regex OK]  |           |
|      |        |             |  Health OK] |             v             |           |
|      |        |             v             |  +---------------------+  |           |
|      |        |  +---------------------+  |  | S2.A2:              |  |           |
|      |        |  | S2.B2:              |  |  | QCM_TARE_READY      |  |           |
|      |        |  | AUX_ZOH_PRIMED      |  |  | (init baseline freq |  |           |
|      |        |  | (init flow, temp,RH)|  |  |  & QCM temp)        |  |           |
|      |        |  +---------------------+  |  +---------------------+  |           |
|      |        +---------------------------+---------------------------+           |
|      |                                |                                           |
|      |                                | join completion [in(S2.A2) && in(S2.B2)] /|
|      |                                | freeze baseline QCM freq & temp,          |
|      |                                | prime ZOH_cache                           |
|      |                                v                                           |
|      |                +-------------------------------+                           |
|      |                | S3: ACTIVE_STREAMING &        |<--+ Valid QCM [Regex OK & |
|      |                |     ZOH_FUSION                |---+ Age<=3s Flow, <=6s SHT|
|      |                +-------------------------------+<--+ / compute & log       |
|      |                                                  --+ Valid AUX [Regex OK]  |
|      |                                                      / refresh ZOH_cache   |
+------+----------------------------------------------------------------------------+
       |                        |                               |
       |                        | user inputs FAN pwm slider    | [SerialException,
       |                        | [0 <= duty <= 100]            |  >3s QCM/Flow or
       |                        v                               |  >6s SHT21]
       |        +-------------------------------+               v
       +--------+ S5: PWM FAN /                 |       +-----------------------+
                | TX: SET_PWM:<duty>\n          |       |       S4: FAULT       |
                +-------------------------------+       +-----------------------+

```

#### Host PC State Transition Table

| Current State | Trigger / Event | Guard Condition | Next State | Output / Action | 
| ----- | ----- | ----- | ----- | ----- | 
| **`S0: DISCONNECTED / BOOT`** | App initializes | Both COM ports available (or `--mock` enabled) | `HS_CONNECTED` $\to$ `S1: IDLE / STANDBY` | Open `pyserial` ports ($115,200\text{ baud}$); spawn `QThread` workers (`QCMReader`, `AuxReader`). | 
| **`HS_CONNECTED (S1/S2/S3)`** | UI Fan Slider Changed | $0 \le \text{duty} \le 100$ | `S5: PWM FAN` $\to$ `(H)` | Transmit `SET_PWM:<duty>\n` over ESP32 UART uplink; immediately return to active substate (`S1`, `S2`, or `S3`) via History `(H)`. | 
| **`S1: IDLE / STANDBY`** | User clicks "Start Run" | Ports healthy | `S2: TARE & PRIME` (`S2.A1`, `S2.B1`) | Call `reset_input_buffer()` on both ports; open session CSV via `csv_logger`; start `aux_reader` and `qcm_reader` priming ingestors. | 
| **`S2.A1: WAIT_QCM_FIRST`** | Valid QCM ASCII line | Regex `pat.search(line)` matches | `S2.A2: QCM_TARE_READY` | Parse ASCII line; store initial QCM baseline frequency $f_0$ and crystal temperature $T_{\text{qcm}}(0)$. | 
| **`S2.B1: WAIT_AUX_FIRST`** | Valid AUX ASCII line | Regex `pat.search(line)` matches & sensor health flags nominal | `S2.B2: AUX_ZOH_PRIMED` | Parse ASCII line; set initial AUX flow rate, SHT21 temperature, and relative humidity in ZOH buffer. | 
| **`S2: TARE & PRIME`** | Join Completion | `in(S2.A2)` $\land$ `in(S2.B2)` | `S3: ACTIVE_STREAMING & ZOH_FUSION` | Freeze baseline QCM frequency $f_0$ and temperature $T_{\text{qcm}}(0)$; prime `ZOH_cache`; begin real-time Sauerbrey fusion and CSV logging. | 
| **`S3: ACTIVE_STREAMING & ZOH_FUSION`** | Valid QCM ASCII line | Regex OK $\land$ ZOH fresh ($\text{Age}_{\text{flow}} \le 3\text{ s}$, $\text{Age}_{\text{sht}} \le 6\text{ s}$) | `S3: ACTIVE_STREAMING & ZOH_FUSION` | Parse ASCII; capture PC Python timestamp; fuse with `ZOH_cache`; compute Sauerbrey $\Delta f_{\text{particle}}$ and $\frac{dm}{dt}$; update plots; write CSV row. | 
| **`S3: ACTIVE_STREAMING & ZOH_FUSION`** | Valid AUX ASCII line | Regex OK | `S3: ACTIVE_STREAMING & ZOH_FUSION` | Parse ASCII; refresh `ZOH_cache` (flow rate, SHT21 temp, humidity, PWM duty) and update cache timestamps. | 
| **`HS_CONNECTED (S1/S2/S3)`** | Devices Unplugged (`SerialException`) / No-Data Timeout | Disconnected ports, no QCM/Flow ASCII for $> 3\text{ s}$, or no SHT21 for $> 6\text{ s}$ | `S4: FAULT` | **Group Preemptive Exit:** Invalidate `ZOH_cache`; halt derivative math; flush/close CSV; stop `QThread` workers & close `pyserial` ports; show modal GUI fault alert. | 
| **`S4: FAULT`** | User confirms fault on GUI | User clicks modal confirmation | `S0: DISCONNECTED / BOOT` | Clear modal fault alert and reset GUI to disconnected state (hardware and ports already safely parked on `S4` entry). | 
| **`HS_CONNECTED (S1/S2/S3)`** | User closes application | Application exit requested | `S0: DISCONNECTED / BOOT` | Direct termination: flush and close CSV, stop `QThread` workers, close `pyserial` ports, exit GUI. | 

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
├── ARCHITECTURE.md                     # This specification file for agentic development
├── requirements.txt                    # PyQt6, pyqtgraph, pyserial, numpy, pandas, scipy, pytest
│
├── software/                           # Host PC Master Orchestrator & Analytics Stack
│   ├── __init__.py
│   ├── gui/                            # Presentation Layer & Top-Level FSM Controller
│   │   ├── __init__.py
│   │   ├── app.py                      # Entry point, CLI parser, QThread lifecycle & signal wiring
│   │   ├── main_window.py              # Master GUI window, Host FSM state manager (S0-S5), fault modals
│   │   ├── views/
│   │   │   ├── __init__.py
│   │   │   ├── time_series_view.py     # 60 FPS pyqtgraph waveforms (f_qcm, delta_f, dm/dt, Temp)
│   │   │   └── diagnostic_view.py      # COM port health, ZOH indicators, S5 Fan PWM slider (0-100%)
│   │   └── widgets/
│   │       ├── __init__.py
│   │       ├── status_indicator.py     # LED state badges (S0-S5, S2.A2/S2.B2 locks, ZOH health)
│   │       └── dynamic_ribbon.py       # Mean trajectory curve with +/-1 sigma confidence envelope
│   │
│   ├── readers/                        # Asynchronous UART-over-USB Ingestion Workers
│   │   ├── __init__.py
│   │   ├── serial_worker.py            # Base QThread pyserial manager, buffer purge & fault handler
│   │   ├── qcm_reader.py               # 1 Hz Basys 3 FPGA ASCII line parser & 3.0s watchdog
│   │   ├── aux_reader.py               # 1 Hz ESP32 ASCII line parser, 3.0s/6.0s watchdogs & SET_PWM TX
│   │   └── smps_adapter.py             # Reference TSI-AIM / SMPS delimited file watcher & scan parser
│   │
│   ├── pipeline/                       # Priming, Multi-Rate Data Fusion, Math & Storage Engine
│   │   ├── __init__.py
│   │   ├── zoh_fusion.py               # S2 AND-superstate priming latch & S3 ZOH cache manager
│   │   ├── sauerbrey.py                # Baseline tare freeze, delta_f & Sauerbrey dm/dt derivative math
│   │   ├── csv_logger.py               # Session CSV writer (open on S1->S2, log in S3, flush on S4/S0)
│   │   ├── quality.py                  # Regex validators, health flag checks, ZOH age evaluators
│   │   └── empirical_fit.py            # Integral dose transform (D_SMPS) & K_empirical solver
│   │
│   └── tests/                          # Hardware-Decoupled Emulation & Verification Suite
│       ├── __init__.py
│       ├── serial_mock.py              # Synthetic 1 Hz FPGA & ESP32 ASCII generators (--mock mode)
│       ├── virtual_loopback.py         # Virtual COM port pair orchestrator (pty / com0com)
│       └── test_pipeline.py            # Unit tests for regex, S2 join completion, ZOH expiry & math
│
├── firmware/
│   ├── fpga/                           # Basys 3 Artix-7 Verilog RTL & XDC constraints
│   └── esp32/                          # ESP-IDF / Arduino Dual-Core C++ firmware
├── hardware/                           # Altium schematics/PCB & 3D chamber/fixture CAD models
└── docs/                               # Interface Control Docs & calibration manifests

```

### Detailed Host PC Module & Class Specifications

#### A. `/software/gui/` — Presentation & FSM Orchestration

1. **`software/gui/app.py`**

   * `parse_arguments() -> argparse.Namespace`: Parses `--mock` (enables in-process synthetic `QThread` generators), `--qcm-port`, `--aux-port`, `--baud` (default `115200`), and `--log-dir` (default `"data/sessions"`).

   * `configure_signal_handling(app: QApplication, controller: ApplicationController) -> None`: Traps `SIGINT` and `SIGTERM` to invoke `controller.shutdown()`.

   * `class ApplicationController(QObject)`:

     * `init_workers(self) -> None`: Executes `S0: DISCONNECTED -> HS_CONNECTED (S1: IDLE)` guard check; opens `pyserial` ports (or spawns `MockQCMReader` / `MockAuxReader` if `--mock` is active) and starts worker threads.

     * `bind_signals(self) -> None`: Connects `qcm_reader.qcm_packet_received` and `aux_reader.aux_packet_received` to `zoh_fusion`, wires `zoh_fusion.fused_row_ready` to `csv_logger.log_fused_row` and `main_window.update_plots`, and connects fault signals (`serial_fault`, `staleness_fault`) to `main_window.on_fault_triggered`.

     * `shutdown(self) -> None`: Executes `HS_CONNECTED -> S0: DISCONNECTED` direct termination (`csv_logger.flush_and_close()`, `qcm_reader.stop_and_close()`, `aux_reader.stop_and_close()`).

2. **`software/gui/main_window.py`**

   * `class HostState(Enum)`: `S0_DISCONNECTED`, `S1_IDLE_STANDBY`, `S2_TARE_AND_PRIME`, `S3_ACTIVE_STREAMING`, `S4_FAULT`, `S5_PWM_FAN`.

   * `class MainWindow(QMainWindow)`:

     * Attributes: `current_state: HostState`, `history_state: HostState` (tracks `H` inside `HS_CONNECTED`).

     * `on_ports_connected(self) -> None`: Transitions `S0 -> S1: IDLE / STANDBY`.

     * `on_start_run_clicked(self) -> None`: Transitions `S1 -> S2: TARE_AND_PRIME`; calls `purge_input_buffer()` on both readers, opens a new session CSV via `csv_logger.open_session_csv()`, and calls `zoh_fusion.reset_for_tare()`.

     * `on_join_completed(self, f0_hz: int, t0_c: float) -> None`: Transitions `S2 -> S3: ACTIVE_STREAMING & ZOH_FUSION`.

     * `on_fan_slider_changed(self, duty: int) -> None`: Validates $0 \le \text{duty} \le 100$ while in `HS_CONNECTED (S1/S2/S3)`; saves `self.history_state = self.current_state`; transitions to `S5_PWM_FAN`; emits `SET_PWM:<duty>\n` to `aux_reader.send_pwm_command(duty)`; restores `self.current_state = self.history_state`.

     * `on_fault_triggered(self, error_msg: str) -> None`: Executes Group Preemptive Exit (`HS_CONNECTED -> S4: FAULT`); invalidates `ZOH_cache`, halts Sauerbrey derivative math, flushes/closes CSV, stops `QThread` workers, closes serial ports, and opens blocking `QMessageBox.critical` modal alert.

     * `on_fault_modal_confirmed(self) -> None`: Transitions `S4: FAULT -> S0: DISCONNECTED` upon modal confirmation and resets UI controls.

3. **`software/gui/views/time_series_view.py` & `diagnostic_view.py`**

   * `class TimeSeriesView(QWidget)`: Manages 4 synchronized `pyqtgraph.PlotWidget` subplots:

     1. Raw QCM Frequency $f_{\text{qcm}}(t)$ ($\text{Hz}$)

     2. Tared Frequency Shift $\Delta f_{\text{particle}}(t)$ ($\text{Hz}$)

     3. Sauerbrey Mass Deposition Rate $\frac{dm}{dt}$ ($\text{ng/s}$)

     4. Crystal Holder Temperature $T_{\text{qcm}}(t)$ vs. SHT21 Chamber Temperature $T_{\text{sht}}(t)$ ($^\circ\text{C}$)

   * `class DiagnosticView(QWidget)`: Renders COM port selectors, "Start Run" button, 0–100% Fan PWM `QSlider`, and digital readouts for D6F flow rate ($\text{L/min}$), SHT21 RH ($\%$), and ZOH cache freshness badges.

#### B. `/software/readers/` — Asynchronous Serial Ingestion Workers

1. **`software/readers/serial_worker.py`**

   * `class BaseSerialWorker(QThread)`:

     * Signal: `serial_fault = pyqtSignal(str)`

     * Methods: `open_port() -> bool`, `purge_input_buffer() -> None` (wraps `self.ser.reset_input_buffer()`), `stop_and_close() -> None`.

2. **`software/readers/qcm_reader.py`**

   * `class QCMReader(BaseSerialWorker)`:

     * Signal: `qcm_packet_received = pyqtSignal(float, int, float)` (`pc_timestamp`, `qcm_freq_hz`, `qcm_temp_c`).

     * Watchdog: Emits `serial_fault("QCM timeout > 3.0s")` if `time.monotonic() - last_valid_ts > 3.0`.

     * `parse_ascii_line(self, line: bytes) -> Optional[tuple[int, float]]`: Matches `rb"QCM_FREQ=(\d+),TEMP_X16=(-?\d+)"` and returns `(int(m.group(1)), int(m.group(2)) / 16.0)`.

3. **`software/readers/aux_reader.py`**

   * `class AuxReader(BaseSerialWorker)`:

     * Signal: `aux_packet_received = pyqtSignal(float, dict)` (`pc_timestamp`, `aux_dict`).

     * Watchdogs: Emits `serial_fault` if flow packet interval $> 3.0\text{ s}$ or fresh SHT21 (`FRESH=1`) interval $> 6.0\text{ s}$.

     * `parse_ascii_line(self, line: bytes) -> Optional[dict]`: Extracts `flow_lpm`, `sht_temp_c`, `sht_rh_pct`, `pwm_duty`, `fresh_flag`, and `health_ok`.

     * `send_pwm_command(self, duty: int) -> None`: Thread-safe slot that enqueues `f"SET_PWM:{duty}\n".encode("ascii")` for immediate transmission on the ESP32 serial port.

#### C. `/software/pipeline/` — Priming, ZOH Fusion & Mathematical Engine

1. **`software/pipeline/zoh_fusion.py`**

   * `class ZOHCache` (Dataclass): Holds `flow_lpm: float`, `sht_temp_c: float`, `sht_rh_pct: float`, `pwm_duty: int`, `last_flow_ts: float`, `last_sht_ts: float`, `is_valid: bool = False`.

   * `class ZOHFusionEngine(QObject)`:

     * Signals: `qcm_tare_ready = pyqtSignal(int, float)`, `aux_zoh_primed = pyqtSignal(dict)`, `s2_join_complete = pyqtSignal(int, float)`, `fused_row_ready = pyqtSignal(dict)`, `staleness_fault = pyqtSignal(str)`.

     * Orthogonal State Flags: `in_s2_a2: bool` (`QCM_TARE_READY`) and `in_s2_b2: bool` (`AUX_ZOH_PRIMED`).

     * `ingest_qcm(self, ts: float, freq_hz: int, qcm_temp_c: float) -> None`: Handles `S2.A1 -> S2.A2` latching during tare, or executes `S3` ZOH staleness verification ($\text{ts} - \text{last\_flow\_ts} \le 3.0\text{ s}$ and $\text{ts} - \text{last\_sht\_ts} \le 6.0\text{ s}$), Sauerbrey computation, and `fused_row_ready` emission during streaming.

     * `ingest_aux(self, ts: float, aux_data: dict) -> None`: Handles `S2.B1 -> S2.B2` ZOH priming (when `health_ok == True`) and `S3` ZOH cache refresh (updating `last_flow_ts` on every valid AUX packet and `last_sht_ts` when `fresh_flag == True`).

     * `check_s2_join(self) -> None`: Evaluates `if self.in_s2_a2 and self.in_s2_b2:`; freezes baseline in `SauerbreyCalculator` and emits `s2_join_complete`.

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

Opened upon `S1 -> S2` transition (`data/sessions/session_YYYYMMDD_HHMMSS.csv`) and flushed/closed on `S4: FAULT` or `S0` exit:

```
pc_timestamp_s,qcm_freq_hz,delta_f_hz,df_dt_hz_s,dm_dt_ng_s,qcm_temp_c,flow_lpm,sht_temp_c,sht_rh_pct,pwm_duty,zoh_flow_age_s,zoh_sht_age_s

```

## 6. Hardware-Decoupled Simulation & Verification (`--mock` & Loopback)

Agents and developers must verify all Host PC functionality without physical sensors using two built-in simulation tiers:

### Tier 1: In-Process `--mock` Mode (`software/tests/serial_mock.py`)

Running `python -m software.gui.app --mock` substitutes physical `pyserial` handles with synthetic `QThread` generators (`MockQCMReader`, `MockAuxReader`) that emit exact $1\text{ Hz}$ ASCII lines:

* **Synthetic FPGA Stream:** Starts at $5,000,000\text{ Hz}$ with $\mathcal{N}(0, 0.8\text{ Hz})$ Gaussian noise, applies a $-3.5\text{ Hz/s}$ deposition ramp between $t = 10\text{ s}$ and $t = 70\text{ s}$, and formats `QCM_FREQ=0005000123,TEMP_X16=000400\r\n`.

* **Synthetic ESP32 Stream:** Alternates `FRESH=1` (even ticks, `E2.1`) and `FRESH=0` (odd ticks, `E2.2`) SHT21 values every $1\text{ s}$, scales simulated D6F flow rate with `current_pwm`, and parses incoming `SET_PWM:<duty>\n` commands.

### Tier 2: OS Virtual Serial Port Loopback (`software/tests/virtual_loopback.py`)

Tests the full `pyserial` OS kernel buffer stack (`reset_input_buffer()`, port disconnects, `SerialException`):

* **Linux / macOS (`socat` / `pty`):**

  ```
  socat -d -d pty,raw,echo=0,link=/tmp/ttyV_QCM_TX pty,raw,echo=0,link=/tmp/ttyV_QCM_RX &
  socat -d -d pty,raw,echo=0,link=/tmp/ttyV_AUX_TX pty,raw,echo=0,link=/tmp/ttyV_AUX_RX &
  python -m software.tests.serial_mock --qcm-tx /tmp/ttyV_QCM_TX --aux-tx /tmp/ttyV_AUX_TX &
  python -m software.gui.app --qcm-port /tmp/ttyV_QCM_RX --aux-port /tmp/ttyV_AUX_RX
  
  ```

* **Windows (`com0com`):** Pair `COM10 <-> COM11` (FPGA) and `COM12 <-> COM13` (ESP32).

## 7. Agentic Implementation Roadmap & Acceptance Tests

When implementing or modifying the `/software` stack, coding agents must execute tasks in the following dependency order and run `pytest software/tests/test_pipeline.py` after each step:

1. **Step 1 — Pipeline Data Structures & Math (`software/pipeline/`):**

   * Implement `quality.py`, `sauerbrey.py`, `zoh_fusion.py`, and `csv_logger.py`.

   * *Acceptance Test:* `pytest` verifies that `ZOHFusionEngine` blocks `S3` entry until both `S2.A2` and `S2.B2` latch, accurately computes $\Delta f_{\text{particle}}$ and $\frac{dm}{dt}$, and emits `staleness_fault` when $\text{Age}_{\text{flow}} > 3.0\text{ s}$ or $\text{Age}_{\text{sht}} > 6.0\text{ s}$.

2. **Step 2 — Asynchronous Serial Workers (`software/readers/`):**

   * Implement `serial_worker.py`, `qcm_reader.py`, and `aux_reader.py`.

   * *Acceptance Test:* `pytest` verifies regex parsing of positive and negative `TEMP_X16` values (`-000080` $\to -5.0^\circ\text{C}$), rejection of malformed ASCII lines without crashing, and $3.0\text{ s}$ / $6.0\text{ s}$ watchdog fault emission.

3. **Step 3 — Mock Generators & Loopback Harness (`software/tests/`):**

   * Implement `serial_mock.py`, `virtual_loopback.py`, and `test_pipeline.py`.

   * *Acceptance Test:* Full headless integration test passes `S0 -> S1 -> S2 -> S3 -> S5 -> S3 -> S4 -> S0` state transitions.

4. **Step 4 — PyQt6 GUI & FSM Controller (`software/gui/`):**

   * Implement `status_indicator.py`, `dynamic_ribbon.py`, `time_series_view.py`, `diagnostic_view.py`, `main_window.py`, and `app.py`.

   * *Acceptance Test:* Launch `python -m software.gui.app --mock`, verify 60 FPS live plotting across `S1`–`S3`, verify `SET_PWM:<duty>\n` dispatch and History `(H)` restoration on slider drag, and confirm modal fault recovery on simulated cable disconnect.