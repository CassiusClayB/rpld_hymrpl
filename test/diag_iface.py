#!/usr/bin/env python3
"""Diagnóstico: verifica nomes de interface em topologia com 10 nós 6LoWPAN."""
from mn_wifi.net import Mininet_wifi
from mn_wifi.sixLoWPAN.link import LoWPAN
from mininet.log import setLogLevel
import time

setLogLevel('info')
net = Mininet_wifi()
sensors = []
for i in range(10):
    name = 'sensor{}'.format(i + 1)
    ip6 = 'fe80::{:x}/64'.format(i + 1)
    params = {'ip6': ip6, 'panid': '0xbeef'}
    if i == 0:
        params['dodag_root'] = True
    s = net.addSensor(name, **params)
    sensors.append(s)

net.configureNodes()
for i in range(9):
    net.addLink(sensors[i], sensors[i+1], cls=LoWPAN)
net.build()
time.sleep(3)

print("\n=== INTERFACE NAMES ===")
for s in sensors:
    out = s.cmd('ip link show 2>/dev/null')
    ifaces = []
    for line in out.split('\n'):
        if ':' in line and 'lo' not in line and line.strip():
            parts = line.strip().split(':')
            if len(parts) >= 2:
                name_part = parts[1].strip().split('@')[0].strip()
                ifaces.append(name_part)
    print('{}: {}'.format(s.name, ifaces))

net.stop()
