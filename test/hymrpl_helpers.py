"""
HyMRPL Test Helpers — Compatibility with rpld + adaptive engine + secure FIFO.

Utility functions to ensure the tests work with the current rpld:
  - set_battery_for_class(): sets /tmp/hymrpl_battery_<iface> so the adaptive
    engine agrees with the configured class (per-interface)
  - send_authenticated_cmd(): sends a command via hymrpl_cmd (authenticated)
  - ensure_token(): ensures the authentication token exists
"""

import os
import re

TOKEN_PATH = "/etc/hymrpl/fifo.token"


def get_iface_name(node):
    """Get the 6LoWPAN interface name for a sensor node."""
    output = node.cmd('ip link show {}-pan0 2>/dev/null'.format(node.name))
    if 'does not exist' not in output and output.strip() and 'pan0' in output:
        return '{}-pan0'.format(node.name)
    output = node.cmd('ip link show 2>/dev/null')
    for line in output.split('\n'):
        m = re.match(r'\d+:\s+(lowpan\d+|wpan\d+):', line)
        if m:
            return m.group(1)
    return '{}-pan0'.format(node.name)


def get_battery_path(sensor):
    """Get the per-interface battery file path for a sensor."""
    iface = get_iface_name(sensor)
    return '/tmp/hymrpl_battery_{}'.format(iface)


def set_battery_for_class(sensor, node_class):
    """
    Sets the simulated PER-INTERFACE battery so the rpld adaptive engine
    agrees with the desired class.

    Class S: battery=100 (high score → adaptive keeps S)
    Class N: battery=10  (low score → adaptive keeps N)
    """
    level = 100 if node_class == 'S' else 10
    path = get_battery_path(sensor)
    sensor.cmd('echo {} > {}'.format(level, path))


def set_battery_level(sensor, level):
    """Set specific battery level (0-100) for a sensor."""
    path = get_battery_path(sensor)
    sensor.cmd('echo {} > {}'.format(level, path))


def send_authenticated_cmd(sensor, cmd_str):
    """
    Sends an authenticated command via hymrpl_cmd.
    Returns the command output.
    """
    output = sensor.cmd('hymrpl_cmd {} 2>&1'.format(cmd_str))
    return output.strip()


def ensure_token():
    """Ensures the FIFO authentication token exists."""
    if not os.path.exists(TOKEN_PATH):
        os.system('mkdir -p /etc/hymrpl')
        os.system('hymrpl_cmd --gen-token')


def setup_battery_for_topology(sensors, hybrid_classes):
    """
    Configures each sensor's battery so the adaptive engine
    agrees with the class defined in HYBRID_CLASSES.

    Must be called BEFORE start_rpld().
    """
    for s in sensors:
        cls = hybrid_classes.get(s.name, 'S')
        # Root has no adaptive engine, but we set it anyway
        set_battery_for_class(s, cls)


def force_class_n_nodes(sensors, hybrid_classes):
    """
    After convergence, forces nodes that must be Class N via authenticated FIFO.
    Must be called AFTER start_rpld() and convergence.
    Waits for the rate limit between each command.
    """
    import time
    for s in sensors:
        cls = hybrid_classes.get(s.name, 'S')
        if cls == 'N' and not s.params.get('dodag_root', False):
            s.cmd('hymrpl_cmd CLASS_N 2>/dev/null')
            time.sleep(2)
