ece490-qcm-platform/
│
├── .gitignore
├── README.md
├── requirements.txt         # PyQt6, pyqtgraph, pyserial, numpy, pandas, scipy
│
├── software/                # Kevin (Host PC Integration)
│   ├── gui/                 # PyQt6 / pyqtgraph application
│   ├── readers/             # Serial workers (qcm_reader, aux_reader)
│   ├── pipeline/            # Sauerbrey, empirical calibration, ZOH logic
│   └── tests/               # Hardware-decoupled serial loopback mocks
│
├── firmware/
│   ├── fpga/                # Ash (Basys 3 Vivado HDL, counter, CDC, UART)
│   └── esp32/               # Ian (Arduino/ESP-IDF: ADC, SHT21 I2C, Fan PWM)
│
├── hardware/                # Adria (Altium Designer files, fixture 3D STLs)
│   ├── schematics/
│   ├── pcb/
│   └── 3d_models/           # Chamber/duct CAD files
│
└── docs/                    # Research notes, datasheets, calibration manifests
