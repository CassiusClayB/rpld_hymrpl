#!/usr/bin/env python3
"""Diagnóstico: verifica interfaces em topologia de 20 nós."""
from mn_wifi.net import Mininet_wifi
from mn_wifi.sixLoWPAN.link import LoWPAN
from mininet.log import setLogLevel
import time

setLogLevel('info')

LINKS = [
    (0, 1), (0, 2), (0, 3), (0, 4),
    (1, 5), (2, 6), (3, 7), (4, 8),
    (5, 9), (6, 10), (7, 11), (8, 12),
    (9, 13), (10, 14), (11, 15), (12, 16),
    (13, 17), (14, 18), (15, 19),
]

net = Mininet_wifi()
sensors = []
for i in range(20):
    name = 'sensor{}'.format(i + 1)
    ip6 = 'fe80::{:x}/64'.format(i + 1)
    params = {'ip6': ip6, 'panid': '0xbeef'}
    if i == 0:
        params['dodag_root'] = True
    s = net.addSensor(name, **params)
    sensors.append(s)

net.configureNodes()
for p, c in LINKS:
    net.addLink(sensors[p], sensors[c], cls=LoWPAN)
net.build()

print("\n*** Waiting 10s after build...")
time.sleep(10)

print("\n=== INTERFACE CHECK ===")
for s in sensors:
    out = s.cmd('ip link show 2>/dev/null')
    lines = []
    for line in out.split('\n'):
        line = line.strip()
        if line and 'lo:' not in line and 'link/' not in line:
            # Extract interface name
            if ':' in line and line[0].isdigit():
                parts = line.split(':')
                if len(parts) >= 2:
                    iname = parts[1].strip().split('@')[0].strip()
                    lines.append(iname)
    has_pan0 = any('pan0' in l for l in lines)
    print('{}: {} {}'.format(s.name, lines, 'OK' if has_pan0 else 'NO-PAN0'))

# Also check if sensor6 can see its interface
print("\n=== RAW ip link show for sensor6 ===")
print(sensors[5].cmd('ip link show'))

print("\n=== RAW ip link show for sensor10 ===")
print(sensors[9].cmd('ip link show'))

net.stop()
