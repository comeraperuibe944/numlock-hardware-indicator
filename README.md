# numlock-hardware-indicator

Hardware activity visualizer repurposing keyboard Lock LEDs (NumLock and CapsLock) on Linux to display real-time CPU bursts, GPU utilization, and battery discharge wattage without interfering with lock state.

## Overview

Modern laptops typically omit dedicated storage and system activity LEDs. numlock-hardware-indicator repurposes standard keyboard lock LEDs to provide glanceable status cues:

1. CPU Activity Visualizer (`cpu_activity_led.py`): Replicates Ethernet RJ45 network jack blink behavior. Stays illuminated during low utilization and flickers rapidly in proportion to CPU core scheduling load.
2. GPU Activity Monitor (`gpu_activity_led.py`): Monitors integrated and discrete GPU engine activity via sysfs and drm interfaces.
3. Power Wattage Indicator (`power_activity_led.py`): Samples power delivery and battery draw from `/sys/class/power_supply` in real time, alerting users to heavy background battery drain.

The daemons interact directly with `/sys/class/leds` triggers without altering X11, Wayland, or kernel keyboard lock state flags.

## Requirements

- Linux kernel with sysfs LED class support
- Python 3.8+
- psutil

```bash
sudo apt install python3-psutil
```

## Installation

1. Copy the scripts to `/usr/local/bin`:

```bash
sudo cp cpu_activity_led.py /usr/local/bin/
sudo cp gpu_activity_led.py /usr/local/bin/
sudo cp power_activity_led.py /usr/local/bin/
sudo chmod +x /usr/local/bin/*_activity_led.py
```

2. Enable the desired systemd service:

```bash
sudo cp systemd/cpu-activity-led.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cpu-activity-led.service
```

## Configuration

Each monitor script accepts configuration arguments:

```bash
sudo python3 cpu_activity_led.py --interval 0.05 --threshold 20
sudo python3 power_activity_led.py --warn-wattage 15.0
```

## License

MIT
