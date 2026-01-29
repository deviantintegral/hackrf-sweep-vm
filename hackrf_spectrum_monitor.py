#!/usr/bin/env python3
"""
HackRF 2.4GHz Spectrum Monitor for VictoriaMetrics

Continuously monitors the 2.4GHz ISM band and pushes metrics to VictoriaMetrics:
- Raw frequency bins (~1MHz resolution) with peak (max) and noise floor (min) values
- Pre-aggregated Zigbee channel metrics for alerting

Usage:
    python hackrf_spectrum_monitor.py [--vm-url http://localhost:8428] [--batch-interval 0.5] [--averaging-period 1.0]
"""

import subprocess
import sys
import time
import argparse
import signal
import os
from collections import defaultdict
from datetime import datetime
from typing import Optional
import urllib.request
import urllib.error

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


class SpectrumMonitor:
    def __init__(self, vm_url: str, batch_interval: float = 0.5, 
                 lna_gain: int = 32, vga_gain: int = 20, bin_width: int = 1000000,
                 averaging_period: float = 1.0):
        self.vm_url = vm_url.rstrip('/') + '/write'
        self.batch_interval = batch_interval
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.bin_width = bin_width  # 1MHz default
        
        # Validate averaging_period
        if averaging_period <= 0:
            raise ValueError(f"averaging_period must be positive, got {averaging_period}")
        self.averaging_period = averaging_period  # Period to average dB values
        
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.metrics_buffer: list[str] = []
        self.last_flush = time.time()
        
        # Averaging buffer for raw dB values
        self.power_samples: dict[int, list[float]] = defaultdict(list)
        self.last_averaging = time.time()
        
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
        cmd = [
            'hackrf_sweep',
            '-f', '2400:2485',      # Full 2.4GHz ISM band
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
        # Generate final aggregated metrics from any remaining buffered samples
        if self.power_samples:
            timestamp_ns = int(time.time() * 1e9)
            self.generate_aggregated_metrics(timestamp_ns)
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
        """Generate aggregated raw bin metrics (max/min) from buffered power samples."""
        for freq_mhz, samples in self.power_samples.items():
            if not samples:
                continue
            max_power = max(samples)  # Peak interference detection
            min_power = min(samples)  # Noise floor baseline
            
            # Emit both max (for interference) and min (for noise floor)
            self.metrics_buffer.extend([
                f"hackrf_power_dbm_max,freq_mhz={freq_mhz} value={max_power:.2f} {timestamp_ns}",
                f"hackrf_noise_floor,freq_mhz={freq_mhz} value={min_power:.2f} {timestamp_ns}",
            ])
        
        # Clear the power samples buffer
        self.power_samples.clear()
        
    def process_sweep(self, timestamp_ns: int, hz_low: int, hz_high: int, 
                      bin_width: int, power_values: list[float]):
        """Process a single sweep's worth of data."""
        
        # Buffer raw bin values for averaging
        for i, power_dbm in enumerate(power_values):
            freq_mhz = self.freq_to_mhz_bin(hz_low, bin_width, i)
            
            # Skip if outside our target range
            if freq_mhz < 2400 or freq_mhz > 2485:
                continue
            
            # Buffer the power value for averaging
            self.power_samples[freq_mhz].append(power_dbm)
            
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
        
        # Check if it's time to generate aggregated metrics
        now = time.time()
        if now - self.last_averaging >= self.averaging_period:
            self.generate_aggregated_metrics(timestamp_ns)
            self.last_averaging = now
        
        # Check if it's time to flush
        if now - self.last_flush >= self.batch_interval:
            self.flush_metrics()
            self.generate_channel_aggregates(timestamp_ns)
            self.last_flush = now
            
    def generate_channel_aggregates(self, timestamp_ns: int):
        """Generate aggregated metrics for Zigbee and WiFi channels."""
        
        # Zigbee channel metrics
        for ch, samples in self.channel_samples.items():
            if not samples:
                continue
            avg_power = sum(samples) / len(samples)
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
            avg_power = sum(samples) / len(samples)
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
            self.metrics_buffer.extend([
                f"hackrf_band_power_avg value={sum(all_samples)/len(all_samples):.2f} {timestamp_ns}",
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
        help='Seconds between metric flushes (default: 0.5)'
    )
    
    # Get default from environment variable with error handling
    default_averaging_period = 1.0
    if 'AVERAGING_PERIOD' in os.environ:
        try:
            default_averaging_period = float(os.environ['AVERAGING_PERIOD'])
            if default_averaging_period <= 0:
                print(f"Warning: AVERAGING_PERIOD env var must be positive, using default 1.0", 
                      file=sys.stderr)
                default_averaging_period = 1.0
        except ValueError:
            print(f"Warning: Invalid AVERAGING_PERIOD env var '{os.environ['AVERAGING_PERIOD']}', "
                  f"must be a number. Using default 1.0", file=sys.stderr)
            default_averaging_period = 1.0
    
    parser.add_argument(
        '--averaging-period',
        type=float,
        default=default_averaging_period,
        help='Period in seconds to aggregate dB values (emits max and min), must be > 0 (default: 1.0, can be set via AVERAGING_PERIOD env var)'
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
    
    args = parser.parse_args()
    
    # Handle signals gracefully
    monitor = SpectrumMonitor(
        vm_url=args.vm_url,
        batch_interval=args.batch_interval,
        lna_gain=args.lna_gain,
        vga_gain=args.vga_gain,
        bin_width=args.bin_width,
        averaging_period=args.averaging_period
    )
    
    signal.signal(signal.SIGTERM, lambda s, f: monitor.stop())
    
    monitor.run()


if __name__ == '__main__':
    main()
