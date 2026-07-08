#!/usr/bin/env python3
"""
HyMRPL — PCAP Capture for 50 nodes (all 3 modes)

Captures traffic at root for Wireshark analysis.
Generates pcap files: /tmp/hymrpl_50nodes_{storing,nonstoring,hybrid}.pcap

Usage: sudo python3 hymrpl_pcap_50nodes.py
"""

import time
import sys
sys.path.insert(0, '/home/wifi/rpld_hymrpl/test')

from mininet.log import setLogLevel, info
from hymrpl_scalability_50 import (
    create_topology, start_rpld, stop_rpld, clean_state,
    get_iface_name, get_global_addr, NUM_NODES
)


def run_capture(sensors, mode, duration=30):
    """Run one mode and capture pcap at root."""
    print('\n=== CAPTURING {} ==='.format(mode.upper()))

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(5)

    start_rpld(sensors, mode)

    # Wait for full convergence
    info("  Waiting 70s for convergence...\n")
    time.sleep(70)

    # Start capture on root
    iface = get_iface_name(sensors[0])
    pcap = '/tmp/hymrpl_50nodes_{}.pcap'.format(mode)
    sensors[0].cmd('tcpdump -i {} -w {} 2>/dev/null &'.format(iface, pcap))
    info("  Capturing on {} for {}s...\n".format(iface, duration))

    # Generate traffic during capture
    time.sleep(3)
    targets = [1, 5, 9, 17, 24, 34, 44, 49]
    for idx in targets:
        if idx < NUM_NODES:
            addr = get_global_addr(sensors[idx])
            if addr:
                sensors[0].cmd('ping6 -c 10 -i 0.3 {} 2>/dev/null &'.format(addr))
                info("  ping6 -> sensor{} ({})\n".format(idx + 1, addr))

    time.sleep(duration)

    # Stop capture
    sensors[0].cmd('killall tcpdump 2>/dev/null')
    time.sleep(2)

    print('  Saved: {}'.format(pcap))


def main():
    setLogLevel('info')

    info("*** Creating 50-node topology for PCAP capture...\n")
    net, sensors = create_topology()

    for mode in ['storing', 'nonstoring', 'hybrid']:
        run_capture(sensors, mode, duration=30)

    stop_rpld(sensors)

    print("\n" + "=" * 50)
    print("PCAP files saved:")
    print("  /tmp/hymrpl_50nodes_storing.pcap")
    print("  /tmp/hymrpl_50nodes_nonstoring.pcap")
    print("  /tmp/hymrpl_50nodes_hybrid.pcap")
    print("=" * 50)

    info("\n*** Stopping network...\n")
    try:
        net.stop()
    except Exception:
        pass


if __name__ == '__main__':
    main()
