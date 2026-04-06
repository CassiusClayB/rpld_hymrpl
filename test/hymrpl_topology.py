#!/usr/bin/env python3
"""
HyMRPL — Topologia de teste com Mininet-WiFi + 6LoWPAN
4 nós estáticos + 1 nó móvel (sensor5)

Topologia:
    sensor1 (Root, Classe S)
       |         \
    sensor2(N)   sensor3(S)
                    |
                 sensor4(N)
                    |
                 sensor5(móvel, N)

Uso: sudo python3 hymrpl_topology.py
"""

import sys
import time
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.cli import CLI
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = "fd3c:be8a:173f:8e80::1"


def gen_rpld_config(node, node_class):
    """Generate rpld config file for a HyMRPL node (MOP=6)"""
    iface = '{}-pan0'.format(node.name)
    is_root = node.params.get('dodag_root', False)

    cmd = 'ifaces = { {\n'
    cmd += '        ifname = "{}",\n'.format(iface)

    if is_root:
        cmd += '        dodag_root = true,\n'
    else:
        cmd += '        dodag_root = false,\n'

    cmd += '        mode_of_operation = 6,\n'
    cmd += '        node_class = "{}",\n'.format(node_class)
    cmd += '        trickle_t = 1,\n'

    if is_root:
        cmd += '        rpls = { {\n'
        cmd += '               instance = 1,\n'
        cmd += '               dags = { {\n'
        cmd += '                       mode_of_operation = 6,\n'
        cmd += '                       node_class = "{}",\n'.format(node_class)
        cmd += '                       dest_prefix = "{}/64",\n'.format(DODAGID[:-1])
        cmd += '                       dodagid = "{}",\n'.format(DODAGID)
        cmd += '               }, }\n'
        cmd += '        }, }\n'

    cmd += '}, }'

    conf_name = 'lowpan-{}.conf'.format(node.name)
    node.pexec("echo '{}' > {}".format(cmd, conf_name), shell=True)
    return conf_name


def topology():
    "Create a network."
    net = Mininet_wifi()

    info("*** Creating nodes\n")
    sensor1 = net.addSensor('sensor1', ip6='fe80::1/64', panid='0xbeef',
                            dodag_root=True)
    sensor2 = net.addSensor('sensor2', ip6='fe80::2/64', panid='0xbeef')
    sensor3 = net.addSensor('sensor3', ip6='fe80::3/64', panid='0xbeef')
    sensor4 = net.addSensor('sensor4', ip6='fe80::4/64', panid='0xbeef')
    sensor5 = net.addSensor('sensor5', ip6='fe80::5/64', panid='0xbeef')

    info("*** Configuring nodes\n")
    net.configureNodes()

    info("*** Adding links\n")
    net.addLink(sensor1, sensor2, cls=LoWPAN)
    net.addLink(sensor1, sensor3, cls=LoWPAN)
    net.addLink(sensor3, sensor4, cls=LoWPAN)
    net.addLink(sensor4, sensor5, cls=LoWPAN)

    info("*** Starting network\n")
    net.build()

    info("*** Configuring HyMRPL (MOP=6)\n")
    # Node class assignments: S=storing-like, N=non-storing-like
    node_classes = {
        sensor1: 'S',  # Root, always Classe S
        sensor2: 'N',
        sensor3: 'S',
        sensor4: 'N',
        sensor5: 'N',  # mobile node
    }

    for node, cls in node_classes.items():
        conf = gen_rpld_config(node, cls)
        info("  {}: class={}, conf={}\n".format(node.name, cls, conf))
        node.cmd('nohup rpld -C {} -m stderr -d 5 > /tmp/rpld_{}.log 2>&1 &'.format(
            conf, node.name))
        if node.params.get('dodag_root', False):
            time.sleep(2)  # let root start first

    info("*** Waiting for DODAG convergence (20s)\n")
    time.sleep(20)

    info("*** Testing connectivity\n")
    for i, s in enumerate([sensor2, sensor3, sensor4, sensor5], start=2):
        result = sensor1.cmd('ping6 -c 3 -W 2 fe80::%d%%sensor1-pan0' % i)
        ok = '0% packet loss' in result
        info("  sensor1->sensor{}: {}\n".format(i, 'OK' if ok else 'FAIL'))

    info("*** Running CLI\n")
    CLI(net)

    info("*** Stopping network\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    topology()
