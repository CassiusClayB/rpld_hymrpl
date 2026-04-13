#!/usr/bin/env python3
"""
Debug script v3: testa restore com retry de rpld (sem kick).
Cada restart do rpld manda um novo DIS.

Uso: sudo python3 debug_restore.py
"""

import time, re
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"

def get_iface(node):
    return '{}-pan0'.format(node.name)

def gen_config(node):
    iface = get_iface(node)
    is_root = node.params.get('dodag_root', False)
    cfg = 'ifaces = { {\n'
    cfg += '        ifname = "{}",\n'.format(iface)
    cfg += '        dodag_root = {},\n'.format('true' if is_root else 'false')
    cfg += '        mode_of_operation = 2,\n'
    cfg += '        trickle_t = 3,\n'
    if is_root:
        cfg += '        rpls = { {\n'
        cfg += '               instance = 1,\n'
        cfg += '               dags = { {\n'
        cfg += '                       mode_of_operation = 2,\n'
        cfg += '                       dest_prefix = "{}/64",\n'.format(DODAGID[:-1])
        cfg += '                       dodagid = "{}",\n'.format(DODAGID)
        cfg += '               }, }\n'
        cfg += '        }, }\n'
    cfg += '}, }'
    path = '/tmp/lowpan-{}.conf'.format(node.name)
    node.cmd("echo '{}' > {}".format(cfg, path))
    return path

def get_global_addr(sensor):
    iface = get_iface(sensor)
    out = sensor.cmd('ip -6 addr show {} | grep "scope global"'.format(iface))
    m = re.search(r'inet6\s+(\S+)/64', out)
    return m.group(1) if m else None

def diag(sensor, label):
    iface = get_iface(sensor)
    print("\n===== DIAG [{}] {} =====".format(sensor.name, label))
    out = sensor.cmd('ip -6 addr show dev {} 2>/dev/null'.format(iface))
    print("[addr] {}".format(out.strip()))
    out = sensor.cmd('ip -6 route show 2>/dev/null')
    print("[route] {}".format(out.strip()))
    out = sensor.cmd('tail -30 /tmp/rpld_{}.log 2>/dev/null'.format(sensor.name))
    print("[rpld log]\n{}".format(out.strip()))
    print("=" * 50)

def main():
    setLogLevel('info')

    print("\n### STEP 1: Create topology (5 nodes) ###")
    net = Mininet_wifi()
    sensors = []
    for i in range(5):
        name = 'sensor{}'.format(i + 1)
        ip6 = 'fe80::{:x}/64'.format(i + 1)
        params = {'ip6': ip6, 'panid': '0xbeef'}
        if i == 0:
            params['dodag_root'] = True
        sensors.append(net.addSensor(name, **params))

    net.configureNodes()
    links = [(0,1), (0,2), (1,3), (2,3), (3,4)]
    for p, c in links:
        net.addLink(sensors[p], sensors[c], cls=LoWPAN)
    net.build()
    time.sleep(10)

    print("\n### STEP 2: Start rpld ###")
    for i, s in enumerate(sensors):
        conf = gen_config(s)
        s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))
        time.sleep(3 if i == 0 else 2)

    print("\n### STEP 3: Wait for convergence (30s) ###")
    time.sleep(30)

    addr5 = get_global_addr(sensors[4])
    if addr5:
        out = sensors[0].cmd('ping6 -c 3 -W 2 {}'.format(addr5))
        print("Baseline root -> sensor5: {}".format(
            'OK' if '0 received' not in out else 'FAIL'))
    else:
        print("ERROR: no global addr at baseline!")
        net.stop()
        return

    # ── Kill sensor5 ──
    print("\n### STEP 4: Kill sensor5 (100% loss, no link down) ###")
    iface5 = get_iface(sensors[4])
    sensors[4].cmd('killall -9 rpld 2>/dev/null')
    sensors[4].cmd('ip -6 route flush proto static 2>/dev/null')
    sensors[4].cmd('ip -6 route flush proto boot 2>/dev/null')
    sensors[4].cmd('ip -6 addr flush dev {} scope global 2>/dev/null'.format(iface5))
    sensors[4].cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface5))
    sensors[4].cmd('tc qdisc add dev {} root netem loss 100%'.format(iface5))
    time.sleep(5)

    # ── Restore: retry rpld every 10s until global addr appears ──
    print("\n### STEP 5: Restore sensor5 (retry rpld, NO neighbor kick) ###")
    sensors[4].cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface5))
    sensors[4].cmd('ip -6 route flush proto static 2>/dev/null')
    sensors[4].cmd('ip -6 route flush proto boot 2>/dev/null')
    sensors[4].cmd('ip -6 addr flush dev {} scope global 2>/dev/null'.format(iface5))

    conf5 = gen_config(sensors[4])
    max_retries = 10
    for attempt in range(1, max_retries + 1):
        sensors[4].cmd('killall -9 rpld 2>/dev/null')
        time.sleep(0.5)
        sensors[4].cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
            conf5, sensors[4].name))
        print("  Attempt {}: rpld started, waiting 15s...".format(attempt))
        time.sleep(15)

        addr = get_global_addr(sensors[4])
        if addr:
            print("  SUCCESS on attempt {}: got addr {}".format(attempt, addr))
            break
        else:
            log = sensors[4].cmd('tail -5 /tmp/rpld_sensor5.log 2>/dev/null')
            print("  No global addr yet. Log: {}".format(log.strip()))
    else:
        print("  FAILED after {} attempts".format(max_retries))

    diag(sensors[4], "AFTER RESTORE")

    addr5 = get_global_addr(sensors[4])
    if addr5:
        out = sensors[0].cmd('ping6 -c 3 -W 2 {}'.format(addr5))
        print("\nroot -> sensor5: {}".format(
            'OK' if '0 received' not in out else 'FAIL'))
    else:
        print("\nFAIL: still no global addr")
        # Check what neighbors are doing
        for i in [2, 3]:  # sensor3, sensor4
            print("\n  {} rpld log (last 10):".format(sensors[i].name))
            out = sensors[i].cmd('tail -10 /tmp/rpld_{}.log 2>/dev/null'.format(
                sensors[i].name))
            print("  {}".format(out.strip()))

    print("\n### DONE ###")
    for s in sensors:
        s.cmd('killall -9 rpld 2>/dev/null')
    net.stop()

if __name__ == '__main__':
    main()
