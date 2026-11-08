import time
import threading
import numpy as np
import serial

def emulate_fpga_qcm(port_name: str, baud: int = 115200):
    """
    Simulates the Basys 3 FPGA (F1 -> F2 -> F3 loop):
    Free-running 1 Hz TX-only ASCII stream of 5 MHz QCM + MCP9808 temp (C * 16).
    """
    ser = serial.Serial(port_name, baud, timeout=1)
    f0_hz = 5_000_000.0
    temp_c = 25.0
    tick = 0

    while True:
        t_start = time.monotonic()
        # Simulate aerosol mass deposition drop (-3.5 Hz/s) between t=10s and t=70s
        if 10 <= tick <= 70:
            f0_hz -= 3.5
        
        # Add 1 Hz Gaussian noise and convert to integer counts
        f_measured = int(round(f0_hz + np.random.normal(0, 0.8)))
        temp_x16 = int(round((temp_c + np.random.normal(0, 0.05)) * 16))

        # Format exact FPGA ASCII string: QCM_FREQ=<10d>,TEMP_X16=<6d>\r\n
        sign = "-" if temp_x16 < 0 else ""
        line = f"QCM_FREQ={f_measured:010d},TEMP_X16={sign}{abs(temp_x16):06d}\r\n"
        ser.write(line.encode("ascii"))

        tick += 1
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t_start)))


def emulate_esp32_aux(port_name: str, baud: int = 115200):
    """
    Simulates the ESP32 Dual-Core FSM (E1 UART Listener + E2.1/E2.2 Multi-Rate Stream):
    1 Hz ASCII telemetry with 0.5 Hz SHT21 caching and SET_PWM:<duty> RX handling.
    """
    ser = serial.Serial(port_name, baud, timeout=0.05)
    current_pwm = 0
    sht_temp_cache = 24.5
    sht_rh_cache = 42.0
    tick = 0

    while True:
        t_start = time.monotonic()

        # Core 1 (E1: UART_CMD_LISTEN): Check for incoming SET_PWM:<duty>\n
        if ser.in_waiting:
            cmd = ser.readline().decode("ascii", errors="ignore").strip()
            if cmd.startswith("SET_PWM:"):
                duty = int(cmd.split(":")[1])
                if 0 <= duty <= 100:
                    current_pwm = duty

        # Core 0 (E2.1 Even Tick = Fresh SHT21; E2.2 Odd Tick = Held SHT21)
        if tick % 2 == 0:
            sht_temp_cache = round(24.5 + np.random.normal(0, 0.1), 2)
            sht_rh_cache = round(42.0 + np.random.normal(0, 0.3), 2)
            fresh_flag = 1
        else:
            fresh_flag = 0

        # Simulate Omron D6F flow rate scaling with fan PWM + turbulence jitter
        flow_lpm = round(0.5 + (current_pwm / 100.0) * 1.5 + np.random.normal(0, 0.02), 3)

        # Send 1 Hz AUX ASCII line
        aux_line = (
            f"FLOW={flow_lpm:.3f},TEMP={sht_temp_cache:.2f},"
            f"RH={sht_rh_cache:.2f},PWM={current_pwm},FRESH={fresh_flag},OK=1\r\n"
        )
        ser.write(aux_line.encode("ascii"))

        tick += 1
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t_start)))