#!/usr/bin/env python3
"""
HyMRPL — Adaptive profile monitoring daemon (Class S / Class N)

Collects local node metrics and decides the functional profile:
  - Class S (storing): stable node with available resources
  - Class N (non-storing): overloaded node, low battery, or mobile

Metrics used:
  - CPU usage (%)
  - Available memory (MB)
  - Simulated battery (%)
  - Mobility index (via RSSI variance, if available)

The decision is written to the FIFO /tmp/hymrpl_cmd, which rpld reads
to switch the profile at runtime.

Usage: sudo python3 hymrpl_monitor.py [--interval 5] [--battery-file /tmp/battery]
"""

import os
import sys
import time
import argparse

# --- Decision thresholds ---
CPU_THRESHOLD_HIGH = 70.0      # above this -> Class N
CPU_THRESHOLD_LOW = 40.0       # below this -> Class S
MEM_THRESHOLD_LOW = 20.0       # free MB; below -> Class N
BATTERY_THRESHOLD_LOW = 20.0   # battery %; below -> Class N
BATTERY_THRESHOLD_HIGH = 50.0  # above this -> can be Class S
HYSTERESIS_CYCLES = 3          # consecutive cycles before switching

FIFO_PATH = "/tmp/hymrpl_cmd"
LOG_PATH = "/tmp/hymrpl_monitor.log"


def get_cpu_usage():
    """Returns CPU usage (%) via /proc/stat"""
    with open('/proc/stat', 'r') as f:
        line = f.readline()
    parts = line.split()
    idle = int(parts[4])
    total = sum(int(x) for x in parts[1:])
    return idle, total


def get_mem_available_mb():
    """Returns available memory in MB"""
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024.0
    return 999.0


def get_battery(battery_file):
    """
    Reads simulated battery level from a file.
    In a real environment, would read from /sys/class/power_supply/.
    Returns 100.0 if the file does not exist.
    """
    if battery_file and os.path.exists(battery_file):
        with open(battery_file, 'r') as f:
            try:
                return float(f.read().strip())
            except ValueError:
                return 100.0
    return 100.0


def write_fifo(profile):
    """Writes profile switch command to the FIFO"""
    try:
        if not os.path.exists(FIFO_PATH):
            os.mkfifo(FIFO_PATH)
        fd = os.open(FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, f"CLASS_{profile}\n".encode())
        os.close(fd)
    except (OSError, BrokenPipeError):
        pass  # rpld is not reading the FIFO right now, that's fine


def log_event(timestamp, cpu, mem, battery, current, decision, reason):
    """Logs event to file"""
    with open(LOG_PATH, 'a') as f:
        f.write(f"{timestamp},{cpu:.1f},{mem:.1f},{battery:.1f},"
                f"{current},{decision},{reason}\n")


def decide_profile(cpu_pct, mem_mb, battery_pct, current_class, counter):
    """
    Decides the functional profile based on metrics.
    Returns (new_class, new_counter, reason)

    Logic:
    - If CPU high OR memory low OR battery low -> suggest Class N
    - If CPU low AND memory ok AND battery ok -> suggest Class S
    - Hysteresis: only switches after HYSTERESIS_CYCLES consecutive cycles
    """
    suggest_n = False
    reason = "stable"

    if cpu_pct > CPU_THRESHOLD_HIGH:
        suggest_n = True
        reason = f"cpu_high({cpu_pct:.0f}%)"
    elif mem_mb < MEM_THRESHOLD_LOW:
        suggest_n = True
        reason = f"mem_low({mem_mb:.0f}MB)"
    elif battery_pct < BATTERY_THRESHOLD_LOW:
        suggest_n = True
        reason = f"battery_low({battery_pct:.0f}%)"

    if suggest_n and current_class == "S":
        counter += 1
        if counter >= HYSTERESIS_CYCLES:
            return "N", 0, reason
        return "S", counter, f"pending_N({counter}/{HYSTERESIS_CYCLES})"

    if not suggest_n and current_class == "N":
        # Only switch back to S if everything is comfortable
        if (cpu_pct < CPU_THRESHOLD_LOW and
            mem_mb > MEM_THRESHOLD_LOW * 2 and
            battery_pct > BATTERY_THRESHOLD_HIGH):
            counter += 1
            if counter >= HYSTERESIS_CYCLES:
                return "S", 0, "resources_recovered"
            return "N", counter, f"pending_S({counter}/{HYSTERESIS_CYCLES})"

    # No change
    return current_class, 0 if not suggest_n else counter, reason


def main():
    parser = argparse.ArgumentParser(description='HyMRPL adaptive profile monitor')
    parser.add_argument('--interval', type=int, default=5,
                        help='Monitoring interval in seconds (default: 5)')
    parser.add_argument('--battery-file', type=str, default='/tmp/hymrpl_battery',
                        help='Path to simulated battery level file')
    parser.add_argument('--initial-class', type=str, default='S',
                        choices=['S', 'N'],
                        help='Initial node class (default: S)')
    args = parser.parse_args()

    current_class = args.initial_class
    counter = 0

    # Initialize log
    with open(LOG_PATH, 'w') as f:
        f.write("timestamp,cpu_pct,mem_mb,battery_pct,current,decision,reason\n")

    print(f"HyMRPL Monitor started (interval={args.interval}s, "
          f"initial_class={current_class})")
    print(f"FIFO: {FIFO_PATH} | Log: {LOG_PATH}")

    # First CPU reading (needs delta)
    prev_idle, prev_total = get_cpu_usage()
    time.sleep(1)

    while True:
        # Collect metrics
        idle, total = get_cpu_usage()
        d_idle = idle - prev_idle
        d_total = total - prev_total
        cpu_pct = (1.0 - d_idle / max(d_total, 1)) * 100.0
        prev_idle, prev_total = idle, total

        mem_mb = get_mem_available_mb()
        battery_pct = get_battery(args.battery_file)

        # Decide
        new_class, counter, reason = decide_profile(
            cpu_pct, mem_mb, battery_pct, current_class, counter)

        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')

        if new_class != current_class:
            print(f"[{timestamp}] SWITCH: {current_class} -> {new_class} ({reason})")
            write_fifo(new_class)
            current_class = new_class
        else:
            print(f"[{timestamp}] class={current_class} cpu={cpu_pct:.0f}% "
                  f"mem={mem_mb:.0f}MB bat={battery_pct:.0f}% ({reason})")

        log_event(timestamp, cpu_pct, mem_mb, battery_pct,
                  current_class, new_class, reason)

        time.sleep(args.interval)


if __name__ == '__main__':
    main()
