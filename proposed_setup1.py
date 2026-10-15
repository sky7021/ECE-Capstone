import numpy as np
import pandas as pd

def generate_mock_sensor_data():
    """
    Generates mock time-series data to perfectly match the numerical example.
    - QCM & Flow operate at 10 Hz (0.1s intervals).
    - Env (Temp/RH) operates at 0.5 Hz (2.0s intervals).
    """
    # 1. QCM & Flow Data (10 Hz) from 0 to 190 seconds
    time_10hz = np.arange(0.0, 190.1, 0.1)
    
    # Mocking QCM frequency to match the exact delta f's at specific time boundaries
    # t=4.0s (f=-2.1), t=64.0s (f=-20.3), t=124.0s (f=-88.6), t=184.0s (f=-116.0)
    qcm_freq = np.interp(
        time_10hz,
        [4.0, 64.0, 124.0, 184.0],
        [-2.1, -20.3, -88.6, -116.0] 
    )
    
    # Mocking Flow rate (m^3/s). 1.2 L/min = 0.00002 m^3/s
    flow_m3_s = np.full_like(time_10hz, 0.00002)
    
    df_fast = pd.DataFrame({
        'time_s': time_10hz,
        'qcm_freq_hz': qcm_freq,
        'flow_rate_m3_s': flow_m3_s
    })

    # 2. ESP32 Environmental Data (0.5 Hz) from 0 to 190 seconds
    time_05hz = np.arange(0.0, 190.1, 2.0)
    df_env = pd.DataFrame({
        'time_s': time_05hz,
        'temp_c': np.full_like(time_05hz, 24.5), # Constant 24.5 C
        'rh_pct': np.full_like(time_05hz, 42.1)  # Constant 42.1 %
    })

    return df_fast, df_env

def run_smps_qcm_validation():
    """
    Executes the validation pipeline: aligns SMPS scans with transport-shifted 
    QCM data, integrates branch flow, and extracts the empirical calibration constant.
    """
    # --- Experimental Parameters ---
    transport_lag = 4.0   # seconds (aerosol delay in QCM branch)
    dt_fast = 0.1         # seconds (10 Hz FPGA sample rate)
    
    # --- SMPS Reference Data (60-second active scans) ---
    smps_scans = [
        {"scan_id": 1, "start_t": 0.0, "end_t": 60.0, "concentration_ug_m3": 40.0},
        {"scan_id": 2, "start_t": 60.0, "end_t": 120.0, "concentration_ug_m3": 150.0},
        {"scan_id": 3, "start_t": 120.0, "end_t": 180.0, "concentration_ug_m3": 60.0},
    ]

    # Load the synthetic high-speed and low-speed telemetry
    df_fast, df_env = generate_mock_sensor_data()
    
    print("=== QCM vs. SMPS Validation Pipeline ===")
    
    for scan in smps_scans:
        print(f"\nProcessing Scan {scan['scan_id']} ({scan['start_t']}s to {scan['end_t']}s)")
        
        # ---------------------------------------------------------
        # STEP 1: Calculate SMPS Dose
        # The SMPS outputs a single concentration value for the 60s scan.
        # We integrate this over the scan duration to get the total dose.
        # ---------------------------------------------------------
        duration = scan['end_t'] - scan['start_t']
        smps_dose = scan['concentration_ug_m3'] * duration
        print(f"  > SMPS Concentration: {scan['concentration_ug_m3']} ug/m^3")
        print(f"  > Calculated SMPS Dose: {smps_dose:.1f} ug*s/m^3")
        
        # ---------------------------------------------------------
        # STEP 2: Apply Transport-Corrected Source-Time Interval
        # Shift the SMPS observation window forward by the transport lag
        # to find the exact moment that same smoke hit the QCM.
        # ---------------------------------------------------------
        w_start = scan['start_t'] + transport_lag
        w_end = scan['end_t'] + transport_lag
        
        # Isolate the 10 Hz data for this specific shifted window
        # (Excluding retrace time, which in this mock is 0 seconds)
        window_mask = (df_fast['time_s'] >= w_start) & (df_fast['time_s'] <= w_end)
        df_window = df_fast.loc[window_mask]
        
        # ---------------------------------------------------------
        # STEP 3: Estimate Native QCM Mass Increment
        # Extract the net frequency shift over the shifted window.
        # ---------------------------------------------------------
        # We use .iloc[0] and .iloc[-1] to get the frequency at the start and end of the window
        f_start = df_window['qcm_freq_hz'].iloc[0]
        f_end = df_window['qcm_freq_hz'].iloc[-1]
        delta_f_net = f_end - f_start
        print(f"  > Shifted QCM Window: {w_start}s to {w_end}s")
        print(f"  > QCM Net Freq Shift: {delta_f_net:.1f} Hz (Start: {f_start:.1f}, End: {f_end:.1f})")
        
        # ---------------------------------------------------------
        # STEP 4: Integrate QCM Branch Flow
        # Use a discrete Riemann sum to calculate total sampled volume.
        # Volume = Sum(Flow(t) * dt)
        # ---------------------------------------------------------
        sampled_volume_m3 = np.sum(df_window['flow_rate_m3_s'] * dt_fast)
        print(f"  > Integrated Branch Volume: {sampled_volume_m3:.4f} m^3")
        
        # ---------------------------------------------------------
        # STEP 5: Summarize Environmental Context (Zero-Order Hold logic)
        # Find the environmental data that occurred during this window.
        # ---------------------------------------------------------
        env_mask = (df_env['time_s'] >= w_start) & (df_env['time_s'] <= w_end)
        df_env_window = df_env.loc[env_mask]
        mean_temp = df_env_window['temp_c'].mean()
        mean_rh = df_env_window['rh_pct'].mean()
        print(f"  > Avg Env Conditions: {mean_temp:.1f} C, {mean_rh:.1f}% RH")
        
        # ---------------------------------------------------------
        # STEP 6: Create Paired Record & Calculate K_empirical
        # Map the QCM's native response directly to the SMPS dose to 
        # establish the empirical calibration constant (K).
        # K = |Delta f| / SMPS Dose
        # ---------------------------------------------------------
        k_empirical = abs(delta_f_net) / smps_dose
        print(f"  => Calculated K_empirical: {k_empirical:.5f} Hz / (ug*s/m^3)")

if __name__ == "__main__":
    run_smps_qcm_validation()