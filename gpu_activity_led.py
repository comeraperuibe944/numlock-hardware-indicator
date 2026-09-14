#!/usr/bin/env python3
"""
Num Lock GPU & Rendering Activity Indicator (Ethernet / RJ45 style)
==================================================================
Visualizes GPU 3D rendering and video hardware load on the physical Num Lock LED:
- Idle / Static state: Solid ON (steady, quiet light).
- GPU rendering / Video playback: Rapid micro-flickers ("ruídos no brilho")
  just like Ethernet link/activity LEDs on network cards.
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

CONFIG_FILE = "/etc/numlock-gpu.conf"
SERVICE_NAME = "numlock-gpu.service"
SYSTEMD_DIR = "/etc/systemd/system"
SCRIPT_PATH = os.path.abspath(__file__)

DEFAULT_THRESHOLD = 25.0    # Only flicker when GPU exceeds 25% load (desktop baseline is ~5-10%)
DEFAULT_SENSITIVITY = 1.0   # Sensitivity multiplier
DEFAULT_PULSE_MS = 25       # 25ms per flicker pulse
DEFAULT_IDLE_STATE = "on"   # Default ON (Ethernet link style)


def load_config() -> dict:
    """Loads configuration from /etc/numlock-gpu.conf if present."""
    cfg = {
        "threshold": DEFAULT_THRESHOLD,
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
                        if k == "threshold":
                            cfg["threshold"] = float(v)
                        elif k == "sensitivity":
                            cfg["sensitivity"] = float(v)
                        elif k == "pulse_ms":
                            cfg["pulse_ms"] = int(v)
                        elif k == "idle_state":
                            cfg["idle_state"] = v.lower()
        except Exception as e:
            print(f"Warning: could not parse {CONFIG_FILE}: {e}")
    return cfg


def save_config(threshold: float, sensitivity: float, pulse_ms: int, idle_state: str):
    """Saves configuration to /etc/numlock-gpu.conf."""
    content = f"""# Num Lock GPU Activity Indicator Configuration
# Threshold percentage (0-100). Only flicker when GPU exceeds this load.
# (Tip: Desktop idle baseline is typically ~5-10%. Default threshold is 25.0)
THRESHOLD={threshold:.1f}

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


class NumLockGPUMonitor:
    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        sensitivity: float = DEFAULT_SENSITIVITY,
        pulse_ms: int = DEFAULT_PULSE_MS,
        idle_state: str = DEFAULT_IDLE_STATE,
        verbose: bool = False,
    ):
        self.threshold = max(1.0, min(99.0, threshold)) / 100.0  # Normalized to 0.0 - 1.0
        self.sensitivity = max(0.1, sensitivity)
        self.pulse_extra_ticks = max(0, round(pulse_ms / 25) - 1)
        self.idle_on = (idle_state.lower() == "on")
        self.verbose = verbose
        self.running = True

        self.led_dirs: List[str] = []
        self.original_triggers: Dict[str, str] = {}
        self.brightness_fds: Dict[str, int] = {}
        self.gpu_fds: List[Tuple[str, int]] = []
        self.last_rescan = 0.0

        # Signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_hup)
        atexit.register(self.cleanup)

        self.rescan_gpus()
        self.rescan_leds()

    def _handle_signal(self, signum, frame):
        if self.verbose:
            print(f"\nCaught signal {signum}, restoring original state...")
        self.running = False

    def _handle_hup(self, signum, frame):
        """Reload configuration on SIGHUP without restarting."""
        cfg = load_config()
        self.threshold = max(1.0, min(99.0, cfg["threshold"])) / 100.0
        self.sensitivity = max(0.1, cfg["sensitivity"])
        self.idle_on = (cfg["idle_state"] == "on")
        if self.verbose:
            print(f"Reloaded config: threshold={self.threshold*100:.1f}%, sensitivity={self.sensitivity}")

    def rescan_gpus(self):
        """Finds all GPU load sysfs files (amdgpu 3D and VCN video codec)."""
        # Close old fds
        for _, fd in self.gpu_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.gpu_fds.clear()

        # Check AMD / DRM cards
        cards = glob.glob("/sys/class/drm/card*")
        for card in cards:
            gpu_busy = os.path.join(card, "device/gpu_busy_percent")
            vcn_busy = os.path.join(card, "device/vcn_busy_percent")

            if os.path.exists(gpu_busy):
                try:
                    fd = os.open(gpu_busy, os.O_RDONLY)
                    self.gpu_fds.append(("gpu_3d", fd))
                except OSError:
                    pass

            if os.path.exists(vcn_busy):
                try:
                    fd = os.open(vcn_busy, os.O_RDONLY)
                    self.gpu_fds.append(("video_vcn", fd))
                except OSError:
                    pass

        if self.verbose:
            print(f"Discovered GPU metric sources: {[name for name, _ in self.gpu_fds]}")

    def read_gpu_load(self) -> float:
        """Reads highest load among active GPU engines (0.0 to 1.0)."""
        if not self.gpu_fds:
            return 0.0

        max_percent = 0.0
        for _, fd in self.gpu_fds:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                val_str = os.read(fd, 32).decode("ascii", errors="ignore").strip()
                if val_str:
                    val = float(val_str)
                    if val > max_percent:
                        max_percent = val
            except Exception:
                continue

        return min(1.0, max(0.0, max_percent / 100.0))

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
        """Main monitoring loop with GPU activity flicker."""
        if not self.brightness_fds:
            print("No accessible Num Lock LEDs found! Are you running with root/sudo?")
            return
        if not self.gpu_fds:
            print("No supported GPU metrics found in /sys/class/drm/card*/device!")
            return

        start_time = time.time()

        # Initial state: solid ON if idle_on is True
        current_state = 1 if self.idle_on else 0
        self.set_leds(current_state)

        pulse_remaining = 0
        consecutive_idle = 0
        smooth_load = self.read_gpu_load()

        try:
            while self.running:
                now = time.time()
                if max_duration and (now - start_time >= max_duration):
                    break

                # Rescan keyboards periodically for hotplugging
                if now - self.last_rescan > 5.0:
                    self.rescan_leds()
                    if not self.gpu_fds:
                        self.rescan_gpus()

                # Read instantaneous GPU load
                instant_load = self.read_gpu_load()
                # Exponential moving average filter
                smooth_load = 0.60 * smooth_load + 0.40 * instant_load

                # Determine LED state (RJ45 activity style only above GPU threshold)
                if pulse_remaining > 0:
                    pulse_remaining -= 1
                    # In pulse: micro-flicker OFF
                    new_state = 0 if self.idle_on else 1
                else:
                    # ONLY flicker if GPU load exceeds threshold
                    if smooth_load >= self.threshold:
                        consecutive_idle = 0
                        # Normalize load between threshold and 1.0
                        scale = 1.0 - self.threshold
                        effective_load = (smooth_load - self.threshold) / scale if scale > 0 else 1.0

                        # Flicker probability: scales up with GPU rendering load
                        prob = min(0.92, effective_load * 0.95 * self.sensitivity)
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
                # When GPU is quiet, sleep 80ms to conserve CPU
                # When GPU is rendering/active, tick at 25ms (40Hz) for crisp responsive flickers
                if consecutive_idle > 6 and pulse_remaining == 0 and smooth_load < (self.threshold * 0.8):
                    time.sleep(0.08)
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

        for _, fd in self.gpu_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.gpu_fds.clear()


def check_root():
    if os.geteuid() != 0:
        print("This operation requires root privileges. Please run with 'sudo'.")
        sys.exit(1)


def install_service():
    check_root()
    # Save default config if not present
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_THRESHOLD, DEFAULT_SENSITIVITY, DEFAULT_PULSE_MS, DEFAULT_IDLE_STATE)

    service_content = f"""[Unit]
Description=Num Lock GPU & Rendering Activity Indicator (Ethernet-style LED Visualizer)
After=multi-user.target

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
    print("=== Configuration (/etc/numlock-gpu.conf) ===")
    print(f"* Threshold:    {cfg['threshold']:.1f}% (only flickers when GPU exceeds this load)")
    print(f"* Sensitivity:  {cfg['sensitivity']:.2f}")
    print(f"* Pulse width:  {cfg['pulse_ms']} ms")
    print(f"* Idle state:   {cfg['idle_state']}")

    print("\n=== GPU Devices ===")
    cards = glob.glob("/sys/class/drm/card*")
    for card in cards:
        gpu_busy = os.path.join(card, "device/gpu_busy_percent")
        vcn_busy = os.path.join(card, "device/vcn_busy_percent")
        g_val = "N/A"
        v_val = "N/A"
        if os.path.exists(gpu_busy):
            try:
                with open(gpu_busy) as f:
                    g_val = f"{f.read().strip()}%"
            except Exception:
                pass
        if os.path.exists(vcn_busy):
            try:
                with open(vcn_busy) as f:
                    v_val = f"{f.read().strip()}%"
            except Exception:
                pass
        print(f"* {os.path.basename(card)}: 3D Render Engine={g_val}, Video VCN={v_val}")

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
        description="Num Lock GPU & Rendering Activity Indicator (Ethernet RJ45-style flicker)"
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
        help="Show active service, GPU metrics, and LED status",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=cfg["threshold"],
        help=f"GPU percentage threshold to start flickering (default: {cfg['threshold']}%%)",
    )
    parser.add_argument(
        "--set-threshold",
        type=float,
        metavar="PERCENT",
        help="Persist new threshold percentage to /etc/numlock-gpu.conf and reload service",
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
        save_config(args.set_threshold, args.sensitivity, args.pulse_ms, args.idle_state)
        # Reload service if running
        res = subprocess.run(["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True)
        if res.stdout.strip() == "active":
            print(f"Reloading {SERVICE_NAME} with new threshold {args.set_threshold:.1f}%...")
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

    if args.test is not None:
        duration = args.test
        print(f"[*] Starting Num Lock GPU visualizer test for {duration} seconds...")
        print(f"[*] Threshold: {args.threshold:.1f}% (Below this: SOLID ON. Above: RJ45-style FLICKER)")
        print("[*] Move windows, scroll pages, or play a video to see the LED respond to rendering!")

        monitor = NumLockGPUMonitor(
            threshold=args.threshold,
            sensitivity=args.sensitivity,
            pulse_ms=args.pulse_ms,
            idle_state=args.idle_state,
            verbose=True,
        )
        monitor.run(max_duration=float(duration))
        print("[*] Test complete. Original LED triggers restored.")
        return

    # Default or foreground
    monitor = NumLockGPUMonitor(
        threshold=args.threshold,
        sensitivity=args.sensitivity,
        pulse_ms=args.pulse_ms,
        idle_state=args.idle_state,
        verbose=True,
    )
    print(f"Num Lock GPU Activity Monitor running (Threshold: {args.threshold:.1f}%). Press Ctrl+C to stop.")
    monitor.run()


if __name__ == "__main__":
    main()
