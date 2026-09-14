#!/usr/bin/env python3
"""
Num Lock Power Consumption Indicator (Ethernet / RJ45 style)
============================================================
Visualizes power consumption (Watts) on the physical Num Lock LED:
- Within expected power (normal / idle): Solid ON (quiet, steady light).
- High power consumption (heavy drain / intensive tasks):
  Rapid micro-flickers ("ruídos no brilho") just like Ethernet link/activity LEDs.
- Calibrated for optimized Ryzen 5500U with RyzenAdj power caps:
  * Battery mode ceiling: 4.5W (threshold: 3.2W, idle: ~2.4W)
  * AC mode ceiling: 20W (threshold: 8.0W, idle: ~3.5W)
- Uses real-time hardware RAPL MSR energy counters (<150ms latency).
- Restores original keyboard state immediately on exit.
"""

import os
import sys
import time
import glob
import signal
import atexit
import random
import argparse
import subprocess
from typing import Dict, List, Optional, Tuple

CONFIG_FILE = "/etc/numlock-power.conf"
SERVICE_NAME = "numlock-power.service"
SYSTEMD_DIR = "/etc/systemd/system"
SCRIPT_PATH = os.path.abspath(__file__)

# Defaults calibrated for Ryzen 5 5500U power-tuning (4.5W battery cap)
DEFAULT_BATTERY_THRESHOLD = 3.2  # Watts (idle is ~2.3-2.5W, ceiling is 4.5W)
DEFAULT_AC_THRESHOLD = 8.0       # Watts (idle is ~3.5W, ceiling is 20W)
DEFAULT_SENSITIVITY = 1.0        # Sensitivity multiplier
DEFAULT_PULSE_MS = 25            # 25ms per flicker pulse
DEFAULT_IDLE_STATE = "on"        # Default ON (Ethernet link style)


def load_config() -> dict:
    """Loads configuration from /etc/numlock-power.conf if present."""
    cfg = {
        "threshold_battery": DEFAULT_BATTERY_THRESHOLD,
        "threshold_ac": DEFAULT_AC_THRESHOLD,
        "sensitivity": DEFAULT_SENSITIVITY,
        "pulse_ms": DEFAULT_PULSE_MS,
        "idle_state": DEFAULT_IDLE_STATE,
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip().lower()
                        v = v.strip()
                        if k in ("threshold_battery", "threshold"):
                            cfg["threshold_battery"] = float(v)
                        elif k == "threshold_ac":
                            cfg["threshold_ac"] = float(v)
                        elif k == "sensitivity":
                            cfg["sensitivity"] = float(v)
                        elif k == "pulse_ms":
                            cfg["pulse_ms"] = int(v)
                        elif k == "idle_state":
                            cfg["idle_state"] = v.lower()
        except Exception as e:
            print(f"Warning: could not parse {CONFIG_FILE}: {e}")
    return cfg


def save_config(threshold_battery: float, threshold_ac: float, sensitivity: float, pulse_ms: int, idle_state: str):
    """Saves configuration to /etc/numlock-power.conf."""
    content = f"""# Num Lock Power Indicator Configuration
# Calibrated for Ryzen 5500U (RyzenAdj battery limit = 4.5W, AC limit = 20W)
# Threshold in Watts on Battery (idle is ~2.4W, ceiling is 4.5W, default: 3.2W)
THRESHOLD_BATTERY={threshold_battery:.1f}

# Threshold in Watts on AC charger (idle is ~3.5W, ceiling is 20W, default: 8.0W)
THRESHOLD_AC={threshold_ac:.1f}

# Sensitivity multiplier (default: 1.0)
SENSITIVITY={sensitivity:.2f}

# Flicker pulse duration in milliseconds (default: 25)
PULSE_MS={pulse_ms}

# Base idle state: 'on' (default, Ethernet style) or 'off'
IDLE_STATE={idle_state}
"""
    with open(CONFIG_FILE, "w") as f:
        f.write(content)
    print(f"Configuration saved to {CONFIG_FILE}")


class HardwarePowerReader:
    """Reads instantaneous power in Watts from RAPL MSR hardware counter."""
    def __init__(self):
        self.rapl_path = "/sys/class/powercap/intel-rapl:0/energy_uj"
        self.ac_online_path = "/sys/class/power_supply/ADP0/online"
        self.bat_status_path = "/sys/class/power_supply/BAT0/status"
        self.rapl_fd = -1
        self.last_energy = None
        self.last_time = None
        self.current_watts = 2.4

        if os.path.exists(self.rapl_path):
            try:
                self.rapl_fd = os.open(self.rapl_path, os.O_RDONLY)
            except OSError:
                pass

    def is_on_battery(self) -> bool:
        """Determines if the laptop is currently running on battery."""
        try:
            if os.path.exists(self.ac_online_path):
                with open(self.ac_online_path, "r") as f:
                    if f.read().strip() == "1":
                        return False
            if os.path.exists(self.bat_status_path):
                with open(self.bat_status_path, "r") as f:
                    if f.read().strip() == "Discharging":
                        return True
        except Exception:
            pass
        return True

    def read_watts(self) -> Tuple[float, str, bool]:
        """Calculates instantaneous power in Watts and returns (watts, source_name, is_battery)."""
        is_battery = self.is_on_battery()
        now = time.time()

        if self.rapl_fd >= 0:
            try:
                os.lseek(self.rapl_fd, 0, os.SEEK_SET)
                data = os.read(self.rapl_fd, 32).decode("ascii", errors="ignore").strip()
                if data:
                    energy = int(data)
                    if self.last_energy is not None and self.last_time is not None:
                        dt = now - self.last_time
                        if dt >= 0.08:  # Minimum 80ms sampling window for clean delta
                            de = energy - self.last_energy
                            if de >= 0:
                                self.current_watts = (de / dt) / 1e6
                            self.last_energy = energy
                            self.last_time = now
                    else:
                        self.last_energy = energy
                        self.last_time = now
                    return self.current_watts, "AMD APU RAPL Package Power", is_battery
            except Exception:
                pass

        # Fallback to hwmon amdgpu PPT
        try:
            with open("/sys/class/hwmon/hwmon4/power1_input", "r") as f:
                val = float(f.read().strip())
                return (val / 1e6), "amdgpu PPT", is_battery
        except Exception:
            pass

        return self.current_watts, "Estimated", is_battery

    def close(self):
        if self.rapl_fd >= 0:
            try:
                os.close(self.rapl_fd)
            except OSError:
                pass
            self.rapl_fd = -1


class NumLockPowerMonitor:
    def __init__(
        self,
        threshold_battery: float = DEFAULT_BATTERY_THRESHOLD,
        threshold_ac: float = DEFAULT_AC_THRESHOLD,
        sensitivity: float = DEFAULT_SENSITIVITY,
        pulse_ms: int = DEFAULT_PULSE_MS,
        idle_state: str = DEFAULT_IDLE_STATE,
        verbose: bool = False,
    ):
        self.threshold_battery = max(1.0, threshold_battery)
        self.threshold_ac = max(1.0, threshold_ac)
        self.sensitivity = max(0.1, sensitivity)
        self.pulse_extra_ticks = max(0, round(pulse_ms / 25) - 1)
        self.idle_on = (idle_state.lower() == "on")
        self.verbose = verbose
        self.running = True

        self.power_reader = HardwarePowerReader()
        self.led_dirs: List[str] = []
        self.original_triggers: Dict[str, str] = {}
        self.brightness_fds: Dict[str, int] = {}
        self.last_rescan = 0.0

        # Signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_hup)
        atexit.register(self.cleanup)

        self.rescan_leds()

    def _handle_signal(self, signum, frame):
        if self.verbose:
            print(f"\nCaught signal {signum}, restoring original state...")
        self.running = False

    def _handle_hup(self, signum, frame):
        """Reload configuration on SIGHUP without restarting."""
        cfg = load_config()
        self.threshold_battery = max(1.0, cfg["threshold_battery"])
        self.threshold_ac = max(1.0, cfg["threshold_ac"])
        self.sensitivity = max(0.1, cfg["sensitivity"])
        self.idle_on = (cfg["idle_state"] == "on")
        if self.verbose:
            print(f"Reloaded config: Battery Threshold={self.threshold_battery:.1f}W, AC Threshold={self.threshold_ac:.1f}W")

    def rescan_leds(self):
        """Finds all Num Lock LEDs and configures them."""
        current_dirs = set(glob.glob("/sys/class/leds/*::numlock"))
        existing_dirs = set(self.led_dirs)

        # Removed keyboards
        for d in existing_dirs - current_dirs:
            if d in self.brightness_fds:
                try:
                    os.close(self.brightness_fds[d])
                except OSError:
                    pass
                del self.brightness_fds[d]

        # Newly discovered keyboards
        for d in current_dirs - existing_dirs:
            trig_file = os.path.join(d, "trigger")
            if not os.path.exists(trig_file):
                continue

            # Detect current trigger to restore on exit
            try:
                with open(trig_file, "r") as f:
                    content = f.read()
                    selected = "kbd-numlock"
                    for word in content.split():
                        if word.startswith("[") and word.endswith("]"):
                            selected = word[1:-1]
                            break
                    self.original_triggers[d] = selected
            except OSError as e:
                if self.verbose:
                    print(f"Warning: could not read trigger for {d}: {e}")
                self.original_triggers[d] = "kbd-numlock"

            # Set trigger to none so we control brightness directly
            try:
                with open(trig_file, "w") as f:
                    f.write("none\n")
            except OSError as e:
                if self.verbose:
                    print(f"Warning: could not set trigger to none for {d}: {e}")
                continue

            # Open brightness file
            b_file = os.path.join(d, "brightness")
            try:
                fd = os.open(b_file, os.O_WRONLY)
                self.brightness_fds[d] = fd
            except OSError as e:
                if self.verbose:
                    print(f"Warning: could not open brightness for {d}: {e}")

        self.led_dirs = sorted(list(self.brightness_fds.keys()))
        self.last_rescan = time.time()
        if self.verbose and (current_dirs != existing_dirs):
            print(f"Active Num Lock LEDs ({len(self.led_dirs)}): {self.led_dirs}")

    def set_leds(self, state: int):
        """Sets brightness (0 or 1) on all active Num Lock LEDs."""
        val = b"1\n" if state else b"0\n"
        dead_keys = []
        for d, fd in self.brightness_fds.items():
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, val)
            except OSError:
                dead_keys.append(d)

        for d in dead_keys:
            try:
                os.close(self.brightness_fds[d])
            except OSError:
                pass
            del self.brightness_fds[d]
            if d in self.led_dirs:
                self.led_dirs.remove(d)

    def run(self, max_duration: Optional[float] = None):
        """Main monitoring loop with power-based flicker."""
        if not self.brightness_fds:
            print("No accessible Num Lock LEDs found! Are you running with root/sudo?")
            return

        start_time = time.time()

        # Initial state: solid ON if idle_on is True
        current_state = 1 if self.idle_on else 0
        self.set_leds(current_state)

        pulse_remaining = 0
        consecutive_idle = 0
        
        # Prime power reader
        self.power_reader.read_watts()
        time.sleep(0.1)
        current_watts, _, is_battery = self.power_reader.read_watts()
        last_power_poll = time.time()

        try:
            while self.running:
                now = time.time()
                if max_duration and (now - start_time >= max_duration):
                    break

                # Rescan keyboards periodically
                if now - self.last_rescan > 5.0:
                    self.rescan_leds()

                # Poll power sensor periodically (~every 100ms)
                if now - last_power_poll >= 0.10:
                    w, _, is_battery = self.power_reader.read_watts()
                    if w > 0:
                        current_watts = w
                    last_power_poll = now

                # Determine active threshold and ceiling based on mode
                if is_battery:
                    active_threshold = self.threshold_battery
                    ceiling = 4.5  # RyzenAdj STAPM/Slow limit on battery
                else:
                    active_threshold = self.threshold_ac
                    ceiling = 20.0  # RyzenAdj STAPM/Slow limit on AC

                # Determine LED state (flicker only when power exceeds threshold)
                if pulse_remaining > 0:
                    pulse_remaining -= 1
                    # In pulse: micro-flicker OFF
                    new_state = 0 if self.idle_on else 1
                else:
                    if current_watts >= active_threshold:
                        consecutive_idle = 0
                        # Calculate excess power factor [0.0, 1.0] toward ceiling
                        scale = max(0.5, ceiling - active_threshold)
                        excess = min(1.0, max(0.0, (current_watts - active_threshold) / scale))
                        
                        # Probability of a flicker pulse in this 25ms tick:
                        # At threshold (e.g. 3.2W): ~15% chance (light ruídos)
                        # Approaching ceiling (3.8W - 4.5W): 60% - 90% chance (pisca bastante!)
                        prob = min(0.92, max(0.15, excess * 0.95 * self.sensitivity))
                        if random.random() < prob:
                            # 1 tick (25ms) mostly, sometimes 2 ticks (50ms)
                            pulse_remaining = self.pulse_extra_ticks if random.random() < 0.8 else (self.pulse_extra_ticks + 1)
                            new_state = 0 if self.idle_on else 1
                        else:
                            new_state = 1 if self.idle_on else 0
                    else:
                        consecutive_idle += 1
                        new_state = 1 if self.idle_on else 0

                if new_state != current_state:
                    current_state = new_state
                    self.set_leds(current_state)

                # Adaptive sleep:
                # When power is comfortably below threshold, sleep 60ms
                # When power is high / flickering, tick at 25ms (40Hz)
                if consecutive_idle > 6 and pulse_remaining == 0 and current_watts < (active_threshold * 0.9):
                    time.sleep(0.06)
                else:
                    time.sleep(0.025)

        finally:
            self.cleanup()

    def cleanup(self):
        """Restores original triggers and closes open files."""
        if not self.original_triggers:
            return

        # Restore default state (1)
        self.set_leds(1)

        for fd in list(self.brightness_fds.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        self.brightness_fds.clear()

        # Restore kernel triggers
        for d, trig in list(self.original_triggers.items()):
            trig_file = os.path.join(d, "trigger")
            try:
                with open(trig_file, "w") as f:
                    f.write(f"{trig}\n")
            except OSError:
                pass
        self.original_triggers.clear()

        self.power_reader.close()


def check_root():
    if os.geteuid() != 0:
        print("This operation requires root privileges. Please run with 'sudo'.")
        sys.exit(1)


def install_service():
    check_root()
    # Save default config if not present
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_BATTERY_THRESHOLD, DEFAULT_AC_THRESHOLD, DEFAULT_SENSITIVITY, DEFAULT_PULSE_MS, DEFAULT_IDLE_STATE)

    service_content = f"""[Unit]
Description=Num Lock Power Consumption Indicator (Ethernet-style LED Visualizer)
After=multi-user.target ryzen-power-tune.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 {SCRIPT_PATH} --foreground
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=3
KillMode=process

[Install]
WantedBy=multi-user.target
"""
    service_dest = os.path.join(SYSTEMD_DIR, SERVICE_NAME)
    print(f"Creating systemd service at {service_dest}...")
    with open(service_dest, "w") as f:
        f.write(service_content)

    print("Reloading systemd daemon...")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Enabling and starting service...")
    subprocess.run(["systemctl", "enable", "--now", SERVICE_NAME], check=True)
    print("\nService successfully installed and running!")
    print(f"Status check: sudo systemctl status {SERVICE_NAME}")


def uninstall_service():
    check_root()
    service_dest = os.path.join(SYSTEMD_DIR, SERVICE_NAME)
    print(f"Stopping and disabling {SERVICE_NAME}...")
    subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
    subprocess.run(["systemctl", "disable", SERVICE_NAME], check=False)

    if os.path.exists(service_dest):
        print(f"Removing {service_dest}...")
        os.remove(service_dest)

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Service uninstalled. Num Lock returned to default keyboard behavior.")


def show_status():
    cfg = load_config()
    reader = HardwarePowerReader()
    reader.read_watts()
    time.sleep(0.1)
    watts, src, is_battery = reader.read_watts()
    reader.close()

    threshold = cfg["threshold_battery"] if is_battery else cfg["threshold_ac"]
    ceiling = 4.5 if is_battery else 20.0

    print("=== Configuration (/etc/numlock-power.conf) ===")
    print(f"* Battery Threshold: {cfg['threshold_battery']:.1f} W (APU idle is ~2.4W, cap ceiling is 4.5W)")
    print(f"* AC Threshold:      {cfg['threshold_ac']:.1f} W (APU idle is ~3.5W, cap ceiling is 20W)")
    print(f"* Sensitivity:       {cfg['sensitivity']:.2f}")
    print(f"* Pulse width:       {cfg['pulse_ms']} ms")
    print(f"* Idle state:        {cfg['idle_state']}")

    print("\n=== Real-Time Power Status ===")
    mode_str = "Battery (Cap: 4.5W via RyzenAdj)" if is_battery else "AC Power (Cap: 20W)"
    print(f"* Power Mode:        {mode_str}")
    print(f"* Current Draw:      {watts:.2f} W ({src})")
    print(f"* Active Threshold:  {threshold:.1f} W (Ceiling: {ceiling:.1f} W)")
    if watts >= threshold:
        pct = min(100.0, (watts / ceiling) * 100)
        print(f"* LED Activity:      [ALERT] FLICKERING (Power {watts:.2f}W is above threshold! Utilizando {pct:.0f}% do teto)")
    else:
        print(f"* LED Activity:      [OK] SOLID ON (Power {watts:.2f}W is within normal expected range)")

    print("\n=== Num Lock LED Devices ===")
    leds = glob.glob("/sys/class/leds/*::numlock")
    if not leds:
        print("No Num Lock LEDs found in /sys/class/leds/")
    for l in leds:
        try:
            with open(f"{l}/trigger", "r") as f:
                trig = f.read().strip()
            with open(f"{l}/brightness", "r") as f:
                br = f.read().strip()
            print(f"* {os.path.basename(l)}: brightness={br}, trigger={trig}")
        except Exception as e:
            print(f"* {os.path.basename(l)}: error reading ({e})")

    print("\n=== Systemd Service Status ===")
    res = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
    active = res.stdout.strip()
    print(f"Service status: {active}")
    if active == "active":
        subprocess.run(["systemctl", "status", SERVICE_NAME, "--no-pager"])


def main():
    cfg = load_config()

    parser = argparse.ArgumentParser(
        description="Num Lock Power Consumption Indicator (Ethernet RJ45-style flicker)"
    )
    parser.add_argument(
        "--test",
        nargs="?",
        const=10,
        type=int,
        metavar="SECONDS",
        help="Run an interactive test for N seconds (default: 10s)",
    )
    parser.add_argument(
        "-f", "--foreground",
        action="store_true",
        help="Run continuously in foreground",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install and start as a systemd service",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Stop and remove the systemd service",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show active service, real-time power draw (Watts), and LED status",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        help="Set threshold in Watts for both Battery and AC",
    )
    parser.add_argument(
        "--set-threshold",
        type=float,
        metavar="WATTS",
        help="Persist new threshold in Watts on Battery to /etc/numlock-power.conf and reload service",
    )
    parser.add_argument(
        "--sensitivity",
        type=float,
        default=cfg["sensitivity"],
        help=f"Flicker sensitivity multiplier (default: {cfg['sensitivity']})",
    )
    parser.add_argument(
        "--pulse-ms",
        type=int,
        default=cfg["pulse_ms"],
        help=f"Duration of a single flicker pulse in ms (default: {cfg['pulse_ms']}ms)",
    )
    parser.add_argument(
        "--idle-state",
        choices=["on", "off"],
        default=cfg["idle_state"],
        help=f"Base idle state of LED: 'on' (Ethernet style, default) or 'off'",
    )

    args = parser.parse_args()

    if args.set_threshold is not None:
        check_root()
        t_bat = args.set_threshold
        t_ac = cfg["threshold_ac"]
        save_config(t_bat, t_ac, args.sensitivity, args.pulse_ms, args.idle_state)
        # Reload service if running
        res = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
        if res.stdout.strip() == "active":
            print(f"Reloading {SERVICE_NAME} with new threshold {t_bat:.1f}W...")
            subprocess.run(["systemctl", "kill", "-s", "HUP", SERVICE_NAME])
        print("Done!")
        return

    if args.install:
        install_service()
        return

    if args.uninstall:
        uninstall_service()
        return

    if args.status:
        show_status()
        return

    check_root()

    t_bat = args.threshold if args.threshold is not None else cfg["threshold_battery"]
    t_ac = args.threshold if args.threshold is not None else cfg["threshold_ac"]

    if args.test is not None:
        duration = args.test
        reader = HardwarePowerReader()
        reader.read_watts()
        time.sleep(0.1)
        w, src, is_battery = reader.read_watts()
        reader.close()
        active_t = t_bat if is_battery else t_ac
        ceiling = 4.5 if is_battery else 20.0

        print(f"[*] Starting Num Lock Power visualizer test for {duration} seconds...")
        print(f"[*] Current Power: {w:.2f} W ({src})")
        print(f"[*] Expected Threshold: {active_t:.1f} W | Hardware Ceiling: {ceiling:.1f} W")
        print(f"[*] Below {active_t:.1f} W: SOLID ON (normal). Above: FLICKERING (high power draw).")
        print("[*] Generating a quick 3-second load burst at t+3s to push APU toward ceiling...")

        # Schedule a 3-second load burst at t+3s
        subprocess.Popen(
            ["python3", "-c", "import time, hashlib; time.sleep(3); t0=time.time(); [hashlib.sha256(b'x'*1000).digest() for _ in iter(lambda: time.time()-t0 > 3, True)]"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        monitor = NumLockPowerMonitor(
            threshold_battery=t_bat,
            threshold_ac=t_ac,
            sensitivity=args.sensitivity,
            pulse_ms=args.pulse_ms,
            idle_state=args.idle_state,
            verbose=True,
        )
        monitor.run(max_duration=float(duration))
        print("[*] Test complete. Original LED triggers restored.")
        return

    # Default or foreground
    monitor = NumLockPowerMonitor(
        threshold_battery=t_bat,
        threshold_ac=t_ac,
        sensitivity=args.sensitivity,
        pulse_ms=args.pulse_ms,
        idle_state=args.idle_state,
        verbose=True,
    )
    print(f"Num Lock Power Monitor running (Battery: {t_bat:.1f}W / Ceiling 4.5W). Press Ctrl+C to stop.")
    monitor.run()


if __name__ == "__main__":
    main()
