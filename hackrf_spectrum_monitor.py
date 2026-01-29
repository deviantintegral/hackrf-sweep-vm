#!/usr/bin/env python3
"""
HackRF 2.4GHz Spectrum Monitor for VictoriaMetrics

Continuously monitors the 2.4GHz ISM band and pushes metrics to VictoriaMetrics:
- Raw frequency bins (~1MHz resolution) with configurable averaging modes
- Pre-aggregated Zigbee channel metrics for alerting

Usage:
    python hackrf_spectrum_monitor.py [--vm-url http://localhost:8428] [--batch-interval 0.5] [--averaging-period 1.0] [--averaging-mode ema]
"""

import subprocess
import sys
import time
import argparse
import signal
import os
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional
import urllib.request
import urllib.error
import numpy as np

# Zigbee 802.15.4 channel definitions (2.4GHz band)
# Channel N has center frequency 2405 + 5*(N-11) MHz, bandwidth ~2MHz
ZIGBEE_CHANNELS = {
    11: (2405, 2403, 2407),  # (center, low, high)
    12: (2410, 2408, 2412),
    13: (2415, 2413, 2417),
    14: (2420, 2418, 2422),
    15: (2425, 2423, 2427),  # Safe from WiFi
    16: (2430, 2428, 2432),
    17: (2435, 2433, 2437),
    18: (2440, 2438, 2442),
    19: (2445, 2443, 2447),
    20: (2450, 2448, 2452),  # Safe from WiFi
    21: (2455, 2453, 2457),
    22: (2460, 2458, 2462),
    23: (2465, 2463, 2467),
    24: (2470, 2468, 2472),
    25: (2475, 2473, 2477),  # Safe from WiFi
    26: (2480, 2478, 2482),  # Safe from WiFi
}

# WiFi channel definitions (for correlation)
WIFI_CHANNELS = {
    1: (2412, 2401, 2423),   # center, low, high (22MHz wide)
    6: (2437, 2426, 2448),
    11: (2462, 2451, 2473),
}


class SpectrumAverager:
    """
    Handles averaging of spectrum data with multiple modes.
    Correctly averages in linear domain (not dB) for mathematical accuracy.
    """
    def __init__(self, mode='ema', alpha=0.3, window_size=10):
        """
        Initialize the spectrum averager.
        
        Args:
            mode: Averaging mode - 'ema', 'sma', 'peak', 'min', or 'none'
            alpha: EMA smoothing factor (0.1=very smooth, 0.9=very responsive)
            window_size: Number of frames for SMA
        """
        self.mode = mode
        self.alpha = alpha
        self.window_size = window_size
        self.ema_state = None
        self.sma_buffer = deque(maxlen=window_size)
        self.peak_hold = None
        self.min_hold = None
    
    def update(self, db_values):
        """
        Update the averager with new dB values.
        
        Args:
            db_values: Single dB value or array of dB values
            
        Returns:
            Averaged value(s) in dB
        """
        # Convert to numpy array and then to linear for correct averaging
        db_array = np.array(db_values) if not isinstance(db_values, np.ndarray) else db_values
        linear = 10 ** (db_array / 10)
        
        # Update all trackers
        # EMA
        if self.ema_state is None:
            self.ema_state = linear
        else:
            self.ema_state = self.alpha * linear + (1 - self.alpha) * self.ema_state
        
        # SMA
        self.sma_buffer.append(linear)
        
        # Peak/Min
        if self.peak_hold is None:
            self.peak_hold = linear.copy() if hasattr(linear, 'copy') else linear
            self.min_hold = linear.copy() if hasattr(linear, 'copy') else linear
        else:
            self.peak_hold = np.maximum(self.peak_hold, linear)
            self.min_hold = np.minimum(self.min_hold, linear)
        
        # Return based on mode
        if self.mode == 'ema':
            result = self.ema_state
        elif self.mode == 'sma':
            result = np.mean(self.sma_buffer, axis=0)
        elif self.mode == 'peak':
            result = self.peak_hold
        elif self.mode == 'min':
            result = self.min_hold
        elif self.mode == 'none':
            result = linear
        else:
            raise ValueError(f"Unknown averaging mode: {self.mode}")
        
        # Convert back to dB (avoid log(0))
        return 10 * np.log10(result + 1e-10)
    
    def reset(self):
        """Reset all averaging state."""
        self.ema_state = None
        self.sma_buffer.clear()
        self.peak_hold = None
        self.min_hold = None


class SpectrumMonitor:
    def __init__(self, vm_url: str, batch_interval: float = 0.5, 
                 lna_gain: int = 32, vga_gain: int = 20, bin_width: int = 1000000,
                 frequency_range: str = '2400:2485',
                 averaging_mode: str = 'ema',
                 ema_alpha: float = 0.3,
                 sma_window: int = 10):
        self.vm_url = vm_url.rstrip('/') + '/write'
        self.batch_interval = batch_interval
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.bin_width = bin_width  # 1MHz default
        self.frequency_range = frequency_range  # Frequency range in MHz (min:max)
        
        # Initialize spectrum averager with configurable parameters
        self.averaging_mode = averaging_mode
        self.averagers: dict[int, SpectrumAverager] = {}  # One averager per frequency bin
        self.ema_alpha = ema_alpha
        self.sma_window = sma_window
        
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.metrics_buffer: list[str] = []
        self.last_flush = time.time()
        
        # Aggregation state for channel metrics
        self.channel_samples: dict[int, list[float]] = defaultdict(list)
        self.wifi_samples: dict[int, list[float]] = defaultdict(list)
        self.sweep_count = 0
        
        # Stats
        self.total_sweeps = 0
        self.total_points_sent = 0
        self.errors = 0
        
    def start_hackrf(self):
        """Start hackrf_sweep in continuous mode."""
        # Validate frequency range format
        if ':' not in self.frequency_range:
            raise ValueError(
                f"Invalid frequency range format: '{self.frequency_range}'. "
                f"Expected format is 'min_freq:max_freq' (e.g., '2400:2485')"
            )
        
        # Validate that we have exactly two numeric values
        parts = self.frequency_range.split(':')
        if len(parts) != 2:
            raise ValueError(
                f"Invalid frequency range format: '{self.frequency_range}'. "
                f"Expected exactly one colon separating min and max frequencies (e.g., '2400:2485')"
            )
        
        try:
            min_freq = float(parts[0])
            max_freq = float(parts[1])
            if min_freq >= max_freq:
                raise ValueError(
                    f"Invalid frequency range: minimum ({min_freq}) must be less than maximum ({max_freq})"
                )
        except ValueError as e:
            if "invalid literal" in str(e):
                raise ValueError(
                    f"Invalid frequency range format: '{self.frequency_range}'. "
                    f"Both min and max must be numeric values (e.g., '2400:2485')"
                )
            raise
        
        cmd = [
            'hackrf_sweep',
            '-f', self.frequency_range,      # Frequency range (e.g., 2400:2485 for full 2.4GHz ISM band)
            '-w', str(self.bin_width),
            '-l', str(self.lna_gain),
            '-g', str(self.vga_gain),
            # No -N flag = continuous mode
        ]
        
        print(f"Starting: {' '.join(cmd)}")
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )
        self.running = True
        
    def stop(self):
        """Gracefully stop the monitor."""
        self.running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        # Flush remaining metrics
        self.flush_metrics()
        
    def parse_sweep_line(self, line: str) -> Optional[tuple]:
        """
        Parse a hackrf_sweep CSV line.
        Format: date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, ...
        Returns: (timestamp_ns, hz_low, hz_high, bin_width, [power_values])
        """
        try:
            parts = line.strip().split(', ')
            if len(parts) < 7:
                return None
                
            # Parse timestamp
            date_str = parts[0]
            time_str = parts[1]
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S.%f")
            timestamp_ns = int(dt.timestamp() * 1e9)
            
            hz_low = int(parts[2])
            hz_high = int(parts[3])
            bin_width = int(float(parts[4]))  # Convert to float first in case of decimal format
            # parts[5] is num_samples, we don't need it
            power_values = [float(p) for p in parts[6:]]
            
            return (timestamp_ns, hz_low, hz_high, bin_width, power_values)
        except (ValueError, IndexError) as e:
            return None
            
    def freq_to_mhz_bin(self, hz_low: int, bin_width: int, index: int) -> int:
        """Get the center frequency in MHz for a bin."""
        return (hz_low + bin_width * index + bin_width // 2) // 1_000_000
    
    def generate_aggregated_metrics(self, timestamp_ns: int):
        """Emit current state of all averagers as metrics."""
        for freq_mhz, averager in self.averagers.items():
            # Get current averaged value from the averager
            # We need to query its current state without updating it
            # For now, we'll emit based on the last update
            if self.averaging_mode == 'ema':
                if averager.ema_state is not None:
                    result = 10 * np.log10(averager.ema_state + 1e-10)
                else:
                    continue
            elif self.averaging_mode == 'sma':
                if len(averager.sma_buffer) > 0:
                    result = 10 * np.log10(np.mean(averager.sma_buffer, axis=0) + 1e-10)
                else:
                    continue
            elif self.averaging_mode == 'peak':
                if averager.peak_hold is not None:
                    result = 10 * np.log10(averager.peak_hold + 1e-10)
                else:
                    continue
            elif self.averaging_mode == 'min':
                if averager.min_hold is not None:
                    result = 10 * np.log10(averager.min_hold + 1e-10)
                else:
                    continue
            elif self.averaging_mode == 'none':
                # For 'none' mode, we don't have persistent state
                # Skip emission as we'll emit immediately when samples arrive
                continue
            else:
                continue
            
            # Emit the averaged value with mode-specific metric name
            metric_name = f"hackrf_power_dbm_{self.averaging_mode}"
            self.metrics_buffer.append(
                f"{metric_name},freq_mhz={freq_mhz} value={result:.2f} {timestamp_ns}"
            )
        
    def process_sweep(self, timestamp_ns: int, hz_low: int, hz_high: int, 
                      bin_width: int, power_values: list[float]):
        """Process a single sweep's worth of data."""
        
        # Process each raw bin value immediately
        for i, power_dbm in enumerate(power_values):
            freq_mhz = self.freq_to_mhz_bin(hz_low, bin_width, i)
            
            # Skip if outside our target range
            if freq_mhz < 2400 or freq_mhz > 2485:
                continue
            
            # Get or create averager for this frequency bin
            if freq_mhz not in self.averagers:
                self.averagers[freq_mhz] = SpectrumAverager(
                    mode=self.averaging_mode,
                    alpha=self.ema_alpha,
                    window_size=self.sma_window
                )
            
            # Update averager immediately with this sample
            self.averagers[freq_mhz].update(power_dbm)
            
            # Accumulate for Zigbee channel aggregation
            for ch, (center, low, high) in ZIGBEE_CHANNELS.items():
                if low <= freq_mhz <= high:
                    self.channel_samples[ch].append(power_dbm)
                    
            # Accumulate for WiFi channel aggregation  
            for ch, (center, low, high) in WIFI_CHANNELS.items():
                if low <= freq_mhz <= high:
                    self.wifi_samples[ch].append(power_dbm)
        
        self.sweep_count += 1
        self.total_sweeps += 1
        
        # Check if it's time to flush and emit metrics
        now = time.time()
        if now - self.last_flush >= self.batch_interval:
            self.generate_aggregated_metrics(timestamp_ns)
            self.flush_metrics()
            self.generate_channel_aggregates(timestamp_ns)
            self.last_flush = now
            
    def generate_channel_aggregates(self, timestamp_ns: int):
        """Generate aggregated metrics for Zigbee and WiFi channels."""
        
        # Zigbee channel metrics
        for ch, samples in self.channel_samples.items():
            if not samples:
                continue
            
            # Convert to linear domain for correct averaging
            linear_samples = 10 ** (np.array(samples) / 10)
            avg_linear = np.mean(linear_samples)
            avg_power = 10 * np.log10(avg_linear + 1e-10)
            
            # Max and min in dB domain (these are fine as-is)
            max_power = max(samples)
            min_power = min(samples)
            
            # Calculate "utilization" - % of samples above CCA threshold (-75 dBm)
            above_threshold = sum(1 for s in samples if s > -75)
            utilization = (above_threshold / len(samples)) * 100
            
            self.metrics_buffer.extend([
                f"zigbee_channel_power_avg,channel={ch} value={avg_power:.2f} {timestamp_ns}",
                f"zigbee_channel_power_max,channel={ch} value={max_power:.2f} {timestamp_ns}",
                f"zigbee_channel_power_min,channel={ch} value={min_power:.2f} {timestamp_ns}",
                f"zigbee_channel_utilization,channel={ch} value={utilization:.2f} {timestamp_ns}",
            ])
            
        # WiFi channel metrics
        for ch, samples in self.wifi_samples.items():
            if not samples:
                continue
            
            # Convert to linear domain for correct averaging
            linear_samples = 10 ** (np.array(samples) / 10)
            avg_linear = np.mean(linear_samples)
            avg_power = 10 * np.log10(avg_linear + 1e-10)
            
            max_power = max(samples)
            
            self.metrics_buffer.extend([
                f"wifi_channel_power_avg,channel={ch} value={avg_power:.2f} {timestamp_ns}",
                f"wifi_channel_power_max,channel={ch} value={max_power:.2f} {timestamp_ns}",
            ])
            
        # Overall band metrics
        all_samples = []
        for samples in self.channel_samples.values():
            all_samples.extend(samples)
        if all_samples:
            # Convert to linear domain for correct averaging
            linear_samples = 10 ** (np.array(all_samples) / 10)
            avg_linear = np.mean(linear_samples)
            avg_power = 10 * np.log10(avg_linear + 1e-10)
            
            self.metrics_buffer.extend([
                f"hackrf_band_power_avg value={avg_power:.2f} {timestamp_ns}",
                f"hackrf_band_power_max value={max(all_samples):.2f} {timestamp_ns}",
                f"hackrf_sweeps_per_interval value={self.sweep_count} {timestamp_ns}",
            ])
            
        # Reset aggregation state
        self.channel_samples.clear()
        self.wifi_samples.clear()
        self.sweep_count = 0
            
    def flush_metrics(self):
        """Send buffered metrics to VictoriaMetrics."""
        if not self.metrics_buffer:
            return
            
        payload = '\n'.join(self.metrics_buffer)
        self.total_points_sent += len(self.metrics_buffer)
        self.metrics_buffer.clear()
        
        try:
            req = urllib.request.Request(
                self.vm_url,
                data=payload.encode('utf-8'),
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass  # Success
        except urllib.error.URLError as e:
            self.errors += 1
            if self.errors % 100 == 1:  # Don't spam errors
                print(f"Error sending to VictoriaMetrics: {e}", file=sys.stderr)
                
    def print_stats(self):
        """Print current statistics."""
        print(f"\rSweeps: {self.total_sweeps:,} | "
              f"Points sent: {self.total_points_sent:,} | "
              f"Errors: {self.errors}", end='', flush=True)
              
    def run(self):
        """Main run loop."""
        self.start_hackrf()
        
        stats_interval = 5.0
        last_stats = time.time()
        
        print("Monitoring started. Press Ctrl+C to stop.")
        
        try:
            while self.running and self.process.poll() is None:
                line = self.process.stdout.readline()
                if not line:
                    continue
                    
                parsed = self.parse_sweep_line(line)
                if parsed:
                    self.process_sweep(*parsed)
                    
                # Periodic stats
                now = time.time()
                if now - last_stats >= stats_interval:
                    self.print_stats()
                    last_stats = now
                    
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            self.stop()
            print(f"\nFinal stats - Sweeps: {self.total_sweeps:,} | "
                  f"Points: {self.total_points_sent:,} | Errors: {self.errors}")


def main():
    parser = argparse.ArgumentParser(
        description='HackRF 2.4GHz Spectrum Monitor for VictoriaMetrics'
    )
    parser.add_argument(
        '--vm-url', 
        default='http://localhost:8428',
        help='VictoriaMetrics URL (default: http://localhost:8428)'
    )
    parser.add_argument(
        '--batch-interval',
        type=float,
        default=0.5,
        help='Seconds between metric emissions and flushes (default: 0.5)'
    )
    
    # Get default averaging mode from environment variable
    default_averaging_mode = os.environ.get('AVERAGING_MODE', 'ema')
    parser.add_argument(
        '--averaging-mode',
        type=str,
        default=default_averaging_mode,
        choices=['ema', 'sma', 'peak', 'min', 'none'],
        help='Averaging mode: ema (Exponential Moving Average), sma (Simple Moving Average), '
             'peak (Max Hold), min (Min Hold), none (no averaging). '
             'Default: ema (can be set via AVERAGING_MODE env var)'
    )
    
    # Get EMA alpha from environment variable
    default_ema_alpha = float(os.environ.get('EMA_ALPHA', '0.3'))
    parser.add_argument(
        '--ema-alpha',
        type=float,
        default=default_ema_alpha,
        help='EMA smoothing factor: 0.05-0.1 (very smooth), 0.2-0.4 (balanced), '
             '0.5-0.7 (responsive), 0.9-1.0 (nearly raw). Default: 0.3 '
             '(can be set via EMA_ALPHA env var)'
    )
    
    # Get SMA window from environment variable
    default_sma_window = int(os.environ.get('SMA_WINDOW', '10'))
    parser.add_argument(
        '--sma-window',
        type=int,
        default=default_sma_window,
        help='Number of frames to average for SMA mode (default: 10, can be set via SMA_WINDOW env var)'
    )
    
    parser.add_argument(
        '--lna-gain',
        type=int,
        default=32,
        help='LNA gain 0-40 dB (default: 32)'
    )
    parser.add_argument(
        '--vga-gain',
        type=int,
        default=20,
        help='VGA gain 0-62 dB (default: 20)'
    )
    parser.add_argument(
        '--bin-width',
        type=int,
        default=1000000,
        help='Frequency bin width in Hz (default: 1000000 = 1MHz)'
    )
    parser.add_argument(
        '--frequency-range',
        type=str,
        default='2400:2485',
        help='Frequency range in MHz as min:max (default: 2400:2485 for full 2.4GHz ISM band)'
    )
    
    args = parser.parse_args()
    
    # Handle signals gracefully
    monitor = SpectrumMonitor(
        vm_url=args.vm_url,
        batch_interval=args.batch_interval,
        lna_gain=args.lna_gain,
        vga_gain=args.vga_gain,
        bin_width=args.bin_width,
        frequency_range=args.frequency_range,
        averaging_mode=args.averaging_mode,
        ema_alpha=args.ema_alpha,
        sma_window=args.sma_window
    )
    
    signal.signal(signal.SIGTERM, lambda s, f: monitor.stop())
    
    monitor.run()


if __name__ == '__main__':
    main()
