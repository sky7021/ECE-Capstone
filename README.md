Repository Structure

```text
ece490-qcm-platform/
│
├── .gitignore                          # Ignores build artifacts, logs, and CSV dumps
├── README.md                           # Project documentation and setup guide
├── requirements.txt                    # Python dependencies (PyQt6, pyqtgraph, etc.)
│
├── software/                           # [Kevin Wu] Host PC GUI & Analytics Pipeline
│   ├── gui/                            # PyQt6 + pyqtgraph presentation dashboard
│   │   ├── app.py                      # Application entry point and event loop
│   │   ├── main_window.py              # Parent window and docking layout
│   │   ├── views/                      # Real-time streaming charts and controls
│   │   └── widgets/                    # Diagnostic status and confidence ribbons
│   ├── readers/                        # Dedicated asynchronous serial worker threads
│   │   ├── qcm_reader.py               # FPGA 10 Hz binary packet parser
│   │   ├── aux_reader.py               # ESP32 telemetry parser and CRC validator
│   │   └── smps_adapter.py             # Delimited SMPS file watcher and scan parser
│   ├── pipeline/                       # Signal processing and calibration math
│   │   ├── packet_definitions.py       # C-struct byte alignment schemas
│   │   ├── zoh_fusion.py               # Zero-Order Hold multi-rate data fusion
│   │   ├── sauerbrey.py                # Differential subtraction & Sauerbrey model
│   │   ├── empirical_fit.py            # Dose-domain integral transform (K_empirical)
│   │   └── raw_logger.py               # Immutable CSV session logger
│   └── tests/                          # Hardware-decoupled emulation
│       ├── serial_mock.py              # Synthetic QCM & ESP32 telemetry generator
│       └── virtual_loopback.py         # Virtual COM port pair manager
│
├── firmware/                           # Embedded Firmware & Digital Logic
│   ├── fpga/                           # [Ashraf Illiyas] Basys 3 AMD Artix-7 Vivado
│   │   ├── constraints/                # Basys 3 master pin constraints (.xdc)
│   │   ├── rtl/                        # Synthesizable Verilog (counter, CDC, FIFO, UART)
│   │   └── ip/                         # Vivado IP configurations
│   └── esp32/                          # [Ian Howie] ESP-IDF / Arduino C++ Firmware
│       ├── src/                        # FreeRTOS tasks (ADC, SHT21 I2C, PWM, CRC)
│       └── include/                    # Pin mapping and shared telemetry headers
│
├── hardware/                           # [Adria Trilla Castello] PCB & Chamber Design
│   ├── schematics/                     # Altium Designer schematics (.SchDoc)
│   ├── pcb/                            # Altium board layouts and Gerber outputs
│   └── 3d_models/                      # Flow duct and dual-resonator chamber STLs
│
└── docs/                               # Engineering Documentation & Specs
    ├── icd/                            # Interface Control Document (packet framing)
    ├── calibration/                    # Chamber volumes and calibration manifests
    └── research/                       # Research Run Reports and sensor datasheets
```

---

## Team Roles & Responsibilities

| Name | Role | Primary Directory | Core Deliverables |
| :--- | :--- | :--- | :--- |
| **Ashraf Illiyas** | FPGA & Resonator Lead | `/firmware/fpga/` | Reciprocal counter RTL (<1 Hz res), CDC synchronizer, dual-clock FIFO, UART packetizer. |
| **Ian Howie** | Embedded Firmware Lead | `/firmware/esp32/` | ESP32 ADC flow driver, SHT21 I2C driver (0.5 Hz duty), fan PWM modulation, CRC16 checking. |
| **Adria Trilla Castello** | Hardware & Mechanics Lead | `/hardware/` | Altium analog front-end schematic/PCB, flow duct 3D model, dual-crystal PTFE chamber fixture. |
| **Kevin Wu** | Software & Integration Lead | `/software/` | PyQt6/pyqtgraph real-time GUI, threaded serial ingestion, ZOH data fusion, empirical SMPS calibration. |

---

## Software Quickstart

### Prerequisites

* Python 3.10+ installed
* Git installed

### Installation

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/sky7021/ECE-Capstone.git](https://github.com/sky7021/ECE-Capstone.git)
   cd ECE-Capstone
   ```

2. **Create and activate a virtual environment:**
   ```bash
   # Windows
   python -m venv .venv
   .venv\Scripts\activate

   # Linux / macOS
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Launch the GUI with simulated hardware (Mock Mode):**
   ```bash
   python software/gui/app.py --mock
   ```

---
