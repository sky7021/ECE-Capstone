ece490-qcm-platform/
│
├── .gitignore                          # Ignores build artifacts, IDE configs, raw test sessions
├── README.md                           # System overview, quickstart, pinout map, flashing guide
├── requirements.txt                    # Pinned Python dependencies for GUI and data pipeline
│
├── software/                           # [Kevin Wu] Host PC Integration & Real-Time Analytics
│   ├── gui/                            # Presentation Layer (PyQt6 / PySide6 + pyqtgraph)
│   │   ├── __init__.py
│   │   ├── app.py                      # Application entry point, QEventLoop lifecycle
│   │   ├── main_window.py              # Main dashboard window, docking layouts, menu actions
│   │   ├── views/                      # Specialized UI views
│   │   │   ├── time_series_view.py     # Hardware-accelerated pyqtgraph streaming charts
│   │   │   ├── spectrum_view.py        # Real-time FFT / peak-tracking display
│   │   │   └── diagnostic_view.py      # Fan PWM sliders, serial port selectors, sensor health
│   │   └── widgets/                    # Modular PyQt widgets
│   │       ├── dynamic_ribbon.py       # Mean trajectory with ±1σ confidence ribbon
│   │       └── status_indicator.py     # Port connection states, dropped frame counters
│   │
│   ├── readers/                        # Asynchronous Hardware Ingestion Layer
│   │   ├── __init__.py
│   │   ├── serial_worker.py            # Base QThread / QRunnable serial reader template
│   │   ├── qcm_reader.py               # FPGA packet consumer, binary unpacker (0xAA 0x55)
│   │   ├── aux_reader.py               # ESP32 telemetry parser (ADC flow, SHT21, CRC verify)
│   │   └── smps_adapter.py             # Delimited TSI-AIM / SMPS file watcher & scan parser
│   │
│   ├── pipeline/                       # Signal Processing & Domain Transformation
│   │   ├── __init__.py
│   │   ├── packet_definitions.py       # C-struct byte alignment definitions (`struct` format)
│   │   ├── clock_mapper.py             # Affine time mapping (t_PC = a * t_device + b), drift
│   │   ├── zoh_fusion.py               # Zero-Order Hold state buffer for multi-rate merging
│   │   ├── sauerbrey.py                # Differential subtraction & static/derivative Sauerbrey
│   │   ├── empirical_fit.py            # Scan-aware dose integration & K_empirical derivation
│   │   ├── quality.py                  # CRC verification, bounds check, stale hold detection
│   │   └── raw_logger.py               # Immutable CSV / Parquet session logger by date & ID
│   │
│   └── tests/                          # Hardware-Decoupled Prototyping & Emulation
│       ├── __init__.py
│       ├── serial_mock.py              # Synthetic dual-QCM & ESP32 telemetry generator
│       ├── virtual_loopback.py         # Virtual COM port pair manager (socat / com0com)
│       └── test_pipeline.py            # Unit tests for Sauerbrey, ZOH, and packet framing
│
├── firmware/                           # Embedded Hardware Logic & Microcontroller Code
│   ├── fpga/                           # [Ashraf Illiyas] Basys 3 AMD Artix-7 (XC7A35T) Vivado
│   │   ├── constraints/
│   │   │   └── basys3_master.xdc       # Pin constraints: 100MHz clock, Pmod JA/JB, UART TX/RX
│   │   ├── rtl/                        # Synthesizable VHDL / Verilog source files
│   │   │   ├── top_qcm_reader.v        # Top-level integration wrapper
│   │   │   ├── reciprocal_counter.v    # High-resolution frequency counter (<1 Hz resolution)
│   │   │   ├── cdc_synchronizer.v      # Multi-stage synchronizer for clock domain crossing
│   │   │   ├── dual_clock_fifo.v       # Asynchronous BRAM FIFO (100 MHz count to UART baud)
│   │   │   ├── packet_encoder.v        # Binary frame assembler (sync header, sequence, CRC)
│   │   │   └── uart_tx.v               # 115,200 / 921,600 baud serial transmitter
│   │   └── ip/                         # Vivado IP configurations (Clocking Wizard, XADC, FFT)
│   │
│   └── esp32/                          # [Ian Howie] ESP-IDF / Arduino C++ Firmware
│       ├── src/
│       │   ├── main.cpp                # FreeRTOS task initialization & timer scheduling
│       │   ├── adc_flow.cpp            # Omron D6F-V03A1 12-bit ADC acquisition & rolling average
│       │   ├── sht21_driver.cpp        # SHT21 I2C driver (0.5 Hz polling to stop self-heating)
│       │   ├── fan_pwm.cpp             # 5V fan PWM modulation via ESP32 LEDC / MCPWM
│       │   ├── packet_builder.cpp      # Binary telemetry framing & CRC16-CCITT calculation
│       │   └── cmd_parser.cpp          # Inbound serial parser for host GUI PWM commands
│       └── include/
│           ├── pin_mapping.h           # ESP32 GPIO assignments (ADC, I2C, PWM, UART)
│           └── telemetry_schema.h      # Shared binary struct definitions matching PC Python
│
├── hardware/                           # [Adria Trilla Castello] PCB & Mechanical Design
│   ├── schematics/                     # Altium Designer project files (.PrjPcb, .SchDoc)
│   │   ├── QCM_Analog_Front_End.SchDoc # Transimpedance amplifier, bandpass filter, comparator
│   │   └── MCU_Sensor_Interface.SchDoc # ESP32 level shifting, fan FET driver, pull-ups
│   ├── pcb/                            # Altium board layouts (.PcbDoc), Gerbers, drill tables
│   │   ├── QCM_Board.PcbDoc            # Controlled 50-ohm RF traces, SMA connectors
│   │   └── Manufacturing_Outputs/      # Gerbers (RS-274X), BOM, Pick-and-Place (XYRS)
│   └── 3d_models/                      # Mechanical Housing & Fixtures (STEP / STL)
│       ├── testing_duct.step           # Aerodynamic flow duct CAD model
│       ├── sensor_fixture.stl          # 3D printable dual-crystal chamber insert
│       └── fan_guard_adapter.stl       # Protective fan mount and airflow collimator
│
└── docs/                               # Project Traceability, Calibration & Specifications
    ├── research/                       # Research Run Reports (Runs 1, 2, 3)
    ├── datasheets/                     # Vendor component references (Basys 3, ESP32, SHT21)
    ├── icd/
    │   └── interface_control_doc.md    # Strict hardware-software packet framing contract
    ├── calibration/
    │   └── calibration_manifest.json   # SMPS cross-calibration constants & chamber volumes
    └── meeting_minutes/                # Weekly supervisor & client agendas (Dr. Zhang)
