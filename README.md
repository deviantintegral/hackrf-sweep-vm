# HackRF 2.4GHz Spectrum Monitor for VictoriaMetrics

Monitor the 2.4GHz ISM band for interference affecting Zigbee networks. Pushes metrics to VictoriaMetrics for visualization in Grafana and alerting via vmalert.

## Features

- **Continuous monitoring** - No process respawn overhead
- **Raw spectrum data** - ~85 frequency bins at 1MHz resolution for waterfall visualization
- **Zigbee channel aggregates** - Pre-computed avg/max/min/utilization for channels 11-26
- **WiFi correlation** - Tracks WiFi channels 1, 6, 11 to correlate interference sources
- **Sub-second updates** - Configurable batch interval (default 500ms)
- **Low overhead** - Pure Python, pushes via InfluxDB line protocol

## Metrics Exposed

### Raw Spectrum (for waterfall plots)
```
hackrf_power_dbm{freq_mhz="2405"} -82.5
hackrf_power_dbm{freq_mhz="2406"} -84.2
...
```

### Zigbee Channel Aggregates (for alerting)
```
zigbee_channel_power_avg{channel="15"} -78.3
zigbee_channel_power_max{channel="15"} -62.1
zigbee_channel_power_min{channel="15"} -92.4
zigbee_channel_utilization{channel="15"} 23.5   # % above -75dBm
```

### WiFi Channels (for correlation)
```
wifi_channel_power_avg{channel="1"} -65.2
wifi_channel_power_max{channel="1"} -52.8
```

### Monitor Health
```
hackrf_sweeps_per_interval 47
hackrf_band_power_avg -81.2
hackrf_band_power_max -58.4
```

## Prerequisites

1. **HackRF One** with appropriate 2.4GHz antenna
2. **hackrf tools** installed (`apt install hackrf` or build from source)
3. **VictoriaMetrics** running and accessible
4. **Python 3.7+** (no external dependencies!)

### Important: Antenna Selection

The standard HackRF antennas (ANT500/ANT700) are only rated to ~1GHz. For accurate 2.4GHz measurements, use:
- A dedicated 2.4GHz antenna (e.g., WiFi antenna with SMA connector)
- Or a wideband antenna rated for 2.4GHz

### USB 3.0 Interference Warning

USB 3.0 ports radiate significant noise in the 2.4GHz band. For best results:
- Use a USB 2.0 port, OR
- Use a shielded USB cable
- Position HackRF away from USB 3.0 devices

## Installation

```bash
# Clone/copy files
sudo mkdir -p /opt/hackrf-monitor
sudo cp hackrf_spectrum_monitor.py /opt/hackrf-monitor/
sudo chmod +x /opt/hackrf-monitor/hackrf_spectrum_monitor.py

# Create service user (optional but recommended)
sudo useradd -r -s /bin/false hackrf
sudo usermod -a -G plugdev hackrf

# Install systemd service
sudo cp hackrf-spectrum-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hackrf-spectrum-monitor
sudo systemctl start hackrf-spectrum-monitor
```

## Usage

### Direct execution
```bash
python3 hackrf_spectrum_monitor.py \
    --vm-url http://localhost:8428 \
    --batch-interval 0.5 \
    --lna-gain 32 \
    --vga-gain 20
```

### Command-line options
```
--vm-url          VictoriaMetrics URL (default: http://localhost:8428)
--batch-interval  Seconds between metric flushes (default: 0.5)
--lna-gain        LNA gain 0-40 dB (default: 32)
--vga-gain        VGA gain 0-62 dB (default: 20)
--bin-width       Frequency bin width in Hz (default: 1000000 = 1MHz)
```

### Check it's working
```bash
# View logs
journalctl -u hackrf-spectrum-monitor -f

# Query VictoriaMetrics directly
curl -s 'http://localhost:8428/api/v1/query?query=zigbee_channel_power_avg'
```

## Grafana Setup

1. Import `grafana_dashboard.json` via Grafana UI
2. Select your VictoriaMetrics data source
3. Dashboard auto-refreshes every 5 seconds

### Key Panels
- **Spectrum Waterfall** - Heatmap showing power vs frequency vs time
- **Zigbee Channel Power** - Time series of recommended channels (15, 20, 25, 26)
- **Channel Utilization** - Gauge showing % busy time per channel
- **Best Channel** - Shows the cleanest Zigbee channel right now

## vmalert Setup

```bash
# Copy alert rules
sudo cp vmalert_rules.yml /etc/vmalert/rules/zigbee_interference.yml

# Restart vmalert to pick up new rules
sudo systemctl restart vmalert
```

### Alert Thresholds

| Alert | Condition | Severity |
|-------|-----------|----------|
| InterferenceWarning | avg > -75 dBm for 2min | warning |
| InterferenceCritical | avg > -65 dBm for 1min | critical |
| BurstInterference | peak > -50 dBm for 30s | warning |
| HighUtilization | >50% busy for 5min | warning |
| ChannelSaturated | >80% busy for 2min | critical |

## Zigbee Channel Selection Guide

**Best channels** (gaps between WiFi 1, 6, 11):
- **Channel 15** (2425 MHz) - Between WiFi 1 and 6
- **Channel 20** (2450 MHz) - Between WiFi 6 and 11
- **Channel 25** (2475 MHz) - Above WiFi 11
- **Channel 26** (2480 MHz) - Highest, least overlap

**Avoid** channels 11-14 (overlap WiFi 1) and 21-24 (partial WiFi 11 overlap).

## Troubleshooting

### "No data" / sweep rate is 0
- Check HackRF is connected: `hackrf_info`
- Check permissions: user must be in `plugdev` group
- Check USB: try USB 2.0 port

### Readings seem wrong
- Verify antenna is connected and appropriate for 2.4GHz
- Check gain settings (default 32/20 is reasonable)
- Move HackRF away from USB 3.0 ports/hubs

### High error count in stats
- VictoriaMetrics might be down or unreachable
- Check `--vm-url` is correct
- Check VictoriaMetrics logs

## Data Retention Considerations

At default settings (~85 bins × 2 sweeps/sec × 0.5s batches), you're generating roughly:
- **Raw bins**: ~170 samples/second = ~14.7M samples/day
- **Aggregates**: ~50 samples per batch = ~8.6K samples/day

VictoriaMetrics handles this easily. For a 90-day retention at ~0.8 bytes/sample:
- Storage estimate: ~1.1 GB for raw data + negligible for aggregates

Consider using VictoriaMetrics' downsampling if you need longer retention with lower resolution.
