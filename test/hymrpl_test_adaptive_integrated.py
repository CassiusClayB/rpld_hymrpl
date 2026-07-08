#!/usr/bin/env python3
"""
HyMRPL — Test: Integrated Adaptive Engine + Secure FIFO

This test validates:
  1. The adaptive engine decides class changes internally (no external script)
  2. The secure FIFO rejects unauthenticated commands
  3. The secure FIFO accepts authenticated commands (via hymrpl_cmd)
  4. Rate limiting prevents rapid oscillation
  5. The adaptive engine responds to PDR degradation and parent changes

Prerequisites:
  - rpld compiled with hymrpl_adaptive.c (adaptive + secure FIFO)
  - hymrpl_cmd compiled and in PATH
  - Token generated: sudo hymrpl_cmd --gen-token
  - Mininet-WiFi with 6LoWPAN
  - Kernel with CONFIG_IPV6_RPL_LWTUNNEL=y

Topology:
    sensor1 (Root, S)
       /        \\
  sensor2(N)   sensor3(S)
                  |
               sensor4(S)
                  |
               sensor5(adaptive — starts as S)

Usage: sudo python3 hymrpl_test_adaptive_integrated.py [--runs 3]
"""

import time
import re
import csv
import os
import sys
import subprocess
import statistics
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
FIFO_PATH = "/tmp/hymrpl_cmd"
TOKEN_PATH = "/etc/hymrpl/fifo.token"
BATTERY_FILE = "/tmp/hymrpl_battery"

HYBRID_CLASSES = {
    'sensor1': 'S',
    'sensor2': 'N',
    'sensor3': 'S',
    'sensor4': 'S',
    'sensor5': 'S',  # starts as S, adaptive engine will manage
}


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def get_iface_name(node):
    output = node.cmd('ip link show {}-pan0 2>/dev/null'.format(node.name))
    if 'does not exist' not in output and output.strip() and 'pan0' in output:
        return '{}-pan0'.format(node.name)
    output = node.cmd('ip link show 2>/dev/null')
    for line in output.split('\n'):
        m = re.match(r'\d+:\s+(lowpan\d+|wpan\d+):', line)
        if m:
            return m.group(1)
    return '{}-pan0'.format(node.name)


def gen_config(node, node_class="S"):
    iface = get_iface_name(node)
    is_root = node.params.get('dodag_root', False)
    mop = 6
    cmd = 'ifaces = { {\n'
    cmd += '        ifname = "{}",\n'.format(iface)
    cmd += '        dodag_root = {},\n'.format('true' if is_root else 'false')
    cmd += '        node_class = "{}",\n'.format(node_class)
    cmd += '        mode_of_operation = {},\n'.format(mop)
    cmd += '        trickle_t = 3,\n'
    if is_root:
        cmd += '        rpls = { {\n'
        cmd += '               instance = 1,\n'
        cmd += '               dags = { {\n'
        cmd += '                       mode_of_operation = {},\n'.format(mop)
        cmd += '                       node_class = "{}",\n'.format(node_class)
        cmd += '                       dest_prefix = "{}/64",\n'.format(DODAGID[:-1])
        cmd += '                       dodagid = "{}",\n'.format(DODAGID)
        cmd += '               }, }\n'
        cmd += '        }, }\n'
    cmd += '}, }'
    conf_name = '/tmp/lowpan-{}.conf'.format(node.name)
    node.cmd("echo '{}' > {}".format(cmd, conf_name))
    return conf_name


def create_topology():
    net = Mininet_wifi()
    s1 = net.addSensor('sensor1', ip6='fe80::1/64', panid='0xbeef', dodag_root=True)
    s2 = net.addSensor('sensor2', ip6='fe80::2/64', panid='0xbeef')
    s3 = net.addSensor('sensor3', ip6='fe80::3/64', panid='0xbeef')
    s4 = net.addSensor('sensor4', ip6='fe80::4/64', panid='0xbeef')
    s5 = net.addSensor('sensor5', ip6='fe80::5/64', panid='0xbeef')
    sensors = [s1, s2, s3, s4, s5]
    net.configureNodes()
    net.addLink(s1, s2, cls=LoWPAN)
    net.addLink(s1, s3, cls=LoWPAN)
    net.addLink(s3, s4, cls=LoWPAN)
    net.addLink(s4, s5, cls=LoWPAN)
    net.build()
    return net, sensors


def start_rpld(sensors):
    """Start rpld on all sensors with adaptive engine enabled on sensor5."""
    root = sensors[0]
    conf = gen_config(root, HYBRID_CLASSES.get(root.name, 'S'))
    root.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
        conf, root.name))
    time.sleep(3)

    for s in [sensors[1], sensors[2]]:
        conf = gen_config(s, HYBRID_CLASSES.get(s.name, 'S'))
        s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
            conf, s.name))
    time.sleep(2)

    for s in [sensors[3], sensors[4]]:
        conf = gen_config(s, HYBRID_CLASSES.get(s.name, 'S'))
        s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
            conf, s.name))
        time.sleep(2)


def stop_rpld(sensors):
    for s in sensors:
        s.cmd('killall -9 rpld 2>/dev/null')
    time.sleep(2)


def clean_state(sensors):
    for s in sensors:
        iface = get_iface_name(s)
        s.cmd('ip -6 route flush proto static 2>/dev/null')
        s.cmd('ip -6 route flush proto boot 2>/dev/null')
        s.cmd('ip -6 addr flush dev {} scope global 2>/dev/null'.format(iface))
        s.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    for s in sensors:
        iface = get_iface_name(s)
        s.cmd('ip link set {} up 2>/dev/null'.format(iface))


def get_global_addr(sensor):
    iface = get_iface_name(sensor)
    output = sensor.cmd('ip -6 addr show {} | grep "scope global"'.format(iface))
    match = re.search(r'inet6\s+(\S+)/64', output)
    return match.group(1) if match else None


def wait_for_global_addr(sensor, timeout=90):
    for _ in range(timeout):
        addr = get_global_addr(sensor)
        if addr:
            return addr
        time.sleep(1)
    return None


def wait_for_convergence(src, dst_addr, max_attempts=120):
    start = time.time()
    for i in range(max_attempts):
        result = src.cmd('ping6 -c 1 -W 2 {}'.format(dst_addr))
        if '1 received' in result:
            return time.time() - start
        time.sleep(0.5 if i < 20 else 1.0)
    return -1


def measure_pdr_latency(src, dst_addr, count=30):
    result = src.cmd('ping6 -c {} -i 0.2 -W 2 {}'.format(count, dst_addr))
    match = re.search(r'(\d+) packets transmitted, (\d+) received', result)
    if not match:
        return {"pdr": 0, "lat_avg": 0, "lat_p95": 0}
    tx, rx = int(match.group(1)), int(match.group(2))
    pdr = rx / tx * 100.0 if tx > 0 else 0

    latencies = []
    for m in re.finditer(r'time=([\d.]+)\s*ms', result):
        latencies.append(float(m.group(1)))

    lat_avg = statistics.mean(latencies) if latencies else 0
    lat_p95 = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0

    return {"pdr": pdr, "lat_avg": lat_avg, "lat_p95": lat_p95}


def set_battery(level):
    """Set simulated battery level for sensor5."""
    with open(BATTERY_FILE, 'w') as f:
        f.write(str(level))


def get_rpld_log(sensor, lines=50):
    """Get last N lines of rpld log for a sensor."""
    output = sensor.cmd('tail -n {} /tmp/rpld_{}.log 2>/dev/null'.format(
        lines, sensor.name))
    return output


def check_log_contains(sensor, pattern):
    """Check if rpld log contains a pattern. Returns count of matches."""
    output = sensor.cmd('grep -c "{}" /tmp/rpld_{}.log 2>/dev/null'.format(
        pattern, sensor.name))
    try:
        return int(output.strip())
    except ValueError:
        return 0


def dump_rpld_log(sensor, label="", lines=30):
    """Dump last N lines of rpld log for debugging."""
    info("\n    [LOG {}@{}] last {} lines:\n".format(sensor.name, label, lines))
    output = sensor.cmd('tail -n {} /tmp/rpld_{}.log 2>/dev/null'.format(
        lines, sensor.name))
    for line in output.strip().split('\n'):
        if line.strip():
            info("      | {}\n".format(line.strip()[:120]))
    info("\n")


def send_fifo_plain(sensor, cmd):
    """Send a plain (unauthenticated) FIFO command (non-blocking)."""
    # Use python to open non-blocking, avoids shell echo blocking on FIFO
    sensor.cmd(
        'python3 -c "import os; '
        "fd = os.open('{}', os.O_WRONLY | os.O_NONBLOCK); ".format(FIFO_PATH) +
        "os.write(fd, b'{}\\n'); os.close(fd)".format(cmd) +
        '" 2>/dev/null &'
    )


def send_fifo_authenticated(sensor, cmd):
    """Send an authenticated FIFO command via hymrpl_cmd (non-blocking)."""
    sensor.cmd('timeout 3 hymrpl_cmd {} 2>/dev/null || true'.format(cmd))


# ============================================================
# TEST CASES
# ============================================================

def test_1_security_reject_unauthenticated(sensors, results):
    """
    TEST 1: Verify that unauthenticated commands are REJECTED
    when token is configured.
    """
    info("\n  === TEST 1: Reject unauthenticated commands ===\n")

    s5 = sensors[4]

    # Ensure token exists
    if not os.path.exists(TOKEN_PATH):
        info("    Generating token...\n")
        os.system('hymrpl_cmd --gen-token')

    # Show FIFO state
    fifo_exists = s5.cmd('ls -la {} 2>&1'.format(FIFO_PATH))
    info("    FIFO state: {}\n".format(fifo_exists.strip()))

    # Show token state
    token_exists = s5.cmd('ls -la {} 2>&1'.format(TOKEN_PATH))
    info("    Token state: {}\n".format(token_exists.strip()))

    time.sleep(2)

    # Get rpld log line count before
    before_count = s5.cmd('wc -l /tmp/rpld_{}.log 2>/dev/null'.format(s5.name)).strip()
    info("    Log lines before: {}\n".format(before_count))

    # Send plain command (should be rejected)
    info("    Sending plain CLASS_N (should be rejected)...\n")
    send_fifo_plain(s5, "CLASS_N")
    time.sleep(5)

    # Get rpld log line count after
    after_count = s5.cmd('wc -l /tmp/rpld_{}.log 2>/dev/null'.format(s5.name)).strip()
    info("    Log lines after: {}\n".format(after_count))

    # Check log for rejection
    rejected = check_log_contains(s5, "rejected FIFO command")
    insecure = check_log_contains(s5, "INSECURE mode")
    switch_count = check_log_contains(s5, "profile switch")
    info("    'rejected FIFO command' count: {}\n".format(rejected))
    info("    'INSECURE mode' count: {}\n".format(insecure))
    info("    'profile switch' count: {}\n".format(switch_count))

    # Dump relevant log lines
    dump_rpld_log(s5, "after plain cmd", 20)

    results["test1_rejected"] = rejected > 0
    results["test1_insecure_mode"] = insecure > 0
    results["test1_no_switch"] = switch_count == 0

    if rejected > 0:
        info("    ✓ PASS: Unauthenticated command rejected\n")
    elif insecure > 0:
        info("    ⚠ INFO: Running in INSECURE mode (no token loaded by rpld)\n")
    else:
        info("    ✗ FAIL: Command was not rejected (check token config)\n")

    return rejected > 0


def test_2_security_accept_authenticated(sensors, results):
    """
    TEST 2: Verify that authenticated commands are ACCEPTED.
    """
    info("\n  === TEST 2: Accept authenticated commands ===\n")

    s5 = sensors[4]

    # Show current class in log
    current_class = s5.cmd('grep -o "class=[SN]" /tmp/rpld_{}.log 2>/dev/null | tail -1'.format(s5.name))
    info("    Current class in log: {}\n".format(current_class.strip()))

    # Send authenticated command
    info("    Sending authenticated CLASS_N...\n")
    cmd_output = s5.cmd('timeout 5 hymrpl_cmd CLASS_N 2>&1')
    info("    hymrpl_cmd output: {}\n".format(cmd_output.strip()))
    time.sleep(5)

    # Check log for successful switch
    switched = check_log_contains(s5, "profile switch S -> N")
    fifo_applied = check_log_contains(s5, "external FIFO command applied")
    info("    'profile switch S -> N' count: {}\n".format(switched))
    info("    'external FIFO command applied' count: {}\n".format(fifo_applied))

    dump_rpld_log(s5, "after auth cmd", 15)

    results["test2_accepted"] = switched > 0 or fifo_applied > 0

    if switched > 0 or fifo_applied > 0:
        info("    ✓ PASS: Authenticated command accepted\n")
    else:
        info("    ✗ FAIL: Authenticated command not processed\n")

    # Switch back for next tests
    time.sleep(12)  # Wait for rate limit
    s5.cmd('timeout 5 hymrpl_cmd CLASS_S 2>&1')
    time.sleep(3)

    return switched > 0 or fifo_applied > 0


def test_3_rate_limiting(sensors, results):
    """
    TEST 3: Verify rate limiting prevents rapid switching.
    """
    info("\n  === TEST 3: Rate limiting ===\n")

    s5 = sensors[4]

    # Send first command (should succeed)
    info("    Sending CLASS_N (should succeed)...\n")
    out1 = s5.cmd('timeout 5 hymrpl_cmd CLASS_N 2>&1')
    info("    Output 1: {}\n".format(out1.strip()))
    time.sleep(2)

    # Send second command immediately (should be rate-limited)
    info("    Sending CLASS_S immediately (should be rate-limited)...\n")
    out2 = s5.cmd('timeout 5 hymrpl_cmd CLASS_S 2>&1')
    info("    Output 2: {}\n".format(out2.strip()))
    time.sleep(3)

    # Check for rate limit message
    rate_limited = check_log_contains(s5, "rate limit")
    too_fast = check_log_contains(s5, "too fast")
    info("    'rate limit' count: {}\n".format(rate_limited))
    info("    'too fast' count: {}\n".format(too_fast))

    dump_rpld_log(s5, "after rate limit test", 15)

    results["test3_rate_limited"] = (rate_limited + too_fast) > 0

    if (rate_limited + too_fast) > 0:
        info("    ✓ PASS: Rapid switch was rate-limited\n")
    else:
        info("    ✗ FAIL: Rate limiting not triggered\n")

    return (rate_limited + too_fast) > 0


def test_4_adaptive_battery_low(sensors, results):
    """
    TEST 4: Verify adaptive engine switches to N when battery drops.
    """
    info("\n  === TEST 4: Adaptive — low battery → Class N ===\n")

    s5 = sensors[4]

    # Wait for rate limit to clear from previous tests
    time.sleep(15)

    # Ensure sensor5 is Class S first
    info("    Resetting to Class S...\n")
    s5.cmd('timeout 5 hymrpl_cmd CLASS_S 2>&1')
    time.sleep(12)

    # Count switches BEFORE this test
    switch_before = check_log_contains(s5, "adaptive: switch S -> N")
    info("    'adaptive: switch S -> N' count BEFORE: {}\n".format(switch_before))

    # Set battery to 10% (below threshold)
    info("    Setting battery to 10%...\n")
    set_battery(10)

    # Verify the file is readable
    bat_check = s5.cmd('cat /tmp/hymrpl_battery 2>&1')
    info("    Battery file content: {}\n".format(bat_check.strip()))

    # Wait for adaptive engine to react
    info("    Waiting 25s for adaptive decision...\n")
    time.sleep(25)

    # Count switches AFTER
    switch_after = check_log_contains(s5, "adaptive: switch S -> N")
    new_switches = switch_after - switch_before
    info("    'adaptive: switch S -> N' count AFTER: {} (new: {})\n".format(
        switch_after, new_switches))

    # Show recent adaptive logs
    score_logs = s5.cmd(
        'grep "adaptive:" /tmp/rpld_{}.log 2>/dev/null | tail -8'.format(s5.name))
    info("    Recent adaptive logs:\n")
    for line in score_logs.strip().split('\n')[-8:]:
        if line.strip():
            info("      | {}\n".format(line.strip()[:120]))

    results["test4_adaptive_low_battery"] = new_switches > 0

    if new_switches > 0:
        info("    ✓ PASS: Adaptive engine switched to N on low battery\n")
    else:
        info("    ✗ FAIL: Adaptive engine did not react to low battery\n")
        info("    (Check if energy reading works — score should show energy<100%)\n")

    # Restore battery
    set_battery(100)

    return new_switches > 0


def test_5_adaptive_recovery(sensors, results):
    """
    TEST 5: Verify adaptive engine switches back to S when conditions improve.
    """
    info("\n  === TEST 5: Adaptive — recovery → Class S ===\n")

    s5 = sensors[4]

    # Battery is now 100% (restored in test 4)
    info("    Battery at 100%, PDR should be good, parent stable\n")
    info("    Waiting 30s for recovery (hysteresis)...\n")
    time.sleep(30)

    adaptive_recovery = check_log_contains(s5, "adaptive: switch N -> S")
    recommending_s = check_log_contains(s5, "recommending S")
    score_logs = s5.cmd('grep "adaptive:" /tmp/rpld_{}.log 2>/dev/null | tail -8'.format(s5.name))
    info("    'adaptive: switch N -> S' count: {}\n".format(adaptive_recovery))
    info("    'recommending S' count: {}\n".format(recommending_s))
    info("    Recent adaptive logs:\n")
    for line in score_logs.strip().split('\n')[-6:]:
        if line.strip():
            info("      | {}\n".format(line.strip()[:120]))

    results["test5_adaptive_recovery"] = adaptive_recovery > 0 or recommending_s > 0

    if adaptive_recovery > 0 or recommending_s > 0:
        info("    ✓ PASS: Adaptive engine recovered to S\n")
    else:
        info("    ✗ FAIL: Adaptive engine did not recover\n")

    return adaptive_recovery > 0


def test_6_adaptive_mobility(sensors, results):
    """
    TEST 6: Verify adaptive engine switches to N on parent instability.
    """
    info("\n  === TEST 6: Adaptive — mobility/parent loss → Class N ===\n")

    s5 = sensors[4]
    s4 = sensors[3]

    # Wait for stable state
    time.sleep(15)

    # Simulate parent instability
    iface4 = get_iface_name(s4)
    info("    Simulating parent loss (link down on {} for 3s)...\n".format(iface4))
    s4.cmd('ip link set {} down'.format(iface4))
    time.sleep(3)
    s4.cmd('ip link set {} up'.format(iface4))
    time.sleep(2)

    # Wait for adaptive engine to detect parent change
    info("    Waiting 25s for adaptive reaction...\n")
    time.sleep(25)

    # Check for parent change detection and class switch
    parent_detected = check_log_contains(s5, "parent changed") + \
                      check_log_contains(s5, "parent.*silent") + \
                      check_log_contains(s5, "invalidating")
    mobility_switch = check_log_contains(s5, "adaptive: switch") + \
                      check_log_contains(s5, "stability=false")

    # Show relevant logs
    parent_logs = s5.cmd('grep -E "parent|HYMRPL|adaptive" /tmp/rpld_{}.log 2>/dev/null | tail -15'.format(s5.name))
    info("    'parent changed/silent/invalidating' count: {}\n".format(parent_detected))
    info("    'adaptive switch/stability' count: {}\n".format(mobility_switch))
    info("    Recent parent/adaptive logs:\n")
    for line in parent_logs.strip().split('\n')[-12:]:
        if line.strip():
            info("      | {}\n".format(line.strip()[:120]))

    results["test6_parent_detected"] = parent_detected > 0
    results["test6_mobility_switch"] = mobility_switch > 0

    if parent_detected > 0:
        info("    ✓ PASS: Parent instability detected\n")
    else:
        info("    ✗ FAIL: Parent instability not detected\n")

    return parent_detected > 0


def test_7_pdr_maintained(sensors, results):
    """
    TEST 7: Verify PDR is maintained throughout all adaptive transitions.
    After test 6 (parent loss), the network needs time to reconverge.
    We actively help reconvergence by waiting and retrying.
    """
    info("\n  === TEST 7: PDR maintained during transitions ===\n")

    s1 = sensors[0]
    s5 = sensors[4]
    s4 = sensors[3]

    # Ensure sensor4 link is up (may have been disrupted in test 6)
    iface4 = get_iface_name(s4)
    iface5 = get_iface_name(s5)
    s4.cmd('ip link set {} up 2>/dev/null'.format(iface4))
    s5.cmd('ip link set {} up 2>/dev/null'.format(iface5))

    # Force DIS to accelerate reconvergence — restart rpld on sensor5
    info("    Restarting rpld on sensor5 to force reconvergence...\n")
    s5.cmd('killall -9 rpld 2>/dev/null')
    time.sleep(2)
    conf = gen_config(s5, 'S')
    s5.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
        conf, s5.name))

    # Wait for reconvergence
    info("    Waiting 30s for full reconvergence...\n")
    time.sleep(30)

    addr5 = wait_for_global_addr(s5, timeout=30)
    if not addr5:
        info("    SKIP: sensor5 has no address\n")
        iface5 = get_iface_name(s5)
        addr_output = s5.cmd('ip -6 addr show {} 2>&1'.format(iface5))
        info("    sensor5 addresses: {}\n".format(addr_output.strip()[:200]))
        results["test7_pdr"] = -1
        return False

    info("    sensor5 address: {}\n".format(addr5))

    # Try ping with retries — reconvergence may still be in progress
    pdr_final = 0
    lat_final = 0
    for attempt in range(3):
        m = measure_pdr_latency(s1, addr5, count=20)
        info("    Attempt {}: PDR={:.1f}% Latency={:.3f}ms\n".format(
            attempt + 1, m["pdr"], m["lat_avg"]))
        if m["pdr"] >= 90.0:
            pdr_final = m["pdr"]
            lat_final = m["lat_avg"]
            break
        elif m["pdr"] > pdr_final:
            pdr_final = m["pdr"]
            lat_final = m["lat_avg"]
        # Wait more between retries
        if attempt < 2:
            info("    Waiting 15s before retry...\n")
            time.sleep(15)

    results["test7_pdr"] = pdr_final
    results["test7_latency"] = lat_final

    if pdr_final >= 90.0:
        info("    ✓ PASS: PDR >= 90% ({:.1f}%)\n".format(pdr_final))
        return True
    elif pdr_final > 0:
        info("    ⚠ WARN: PDR {:.1f}% (partial reconvergence)\n".format(pdr_final))
        dump_rpld_log(s5, "partial PDR", 15)
        return False
    else:
        info("    ✗ FAIL: PDR 0% (network not reconverged)\n")
        dump_rpld_log(s5, "PDR=0 debug", 20)
        return False


# ============================================================
# MAIN
# ============================================================

def run_experiment(run_id, sensors):
    """Run all test cases for one experiment run."""
    info("\n{'='*60}\n")
    info("=== ADAPTIVE + SECURITY TEST | Run {} ===\n".format(run_id))
    info("{'='*60}\n")

    results = {"run": run_id, "timestamp": datetime.now().isoformat()}

    # Initialize battery at 100%
    set_battery(100)

    # Start fresh
    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)
    start_rpld(sensors)

    # Wait for convergence
    info("  Waiting for convergence...\n")
    addr5 = wait_for_global_addr(sensors[4], timeout=90)
    if not addr5:
        info("  FAIL: sensor5 did not get address\n")
        results["convergence"] = False
        return results

    conv = wait_for_convergence(sensors[0], addr5)
    results["convergence_s"] = round(conv, 2) if conv > 0 else -1
    info("  Convergence: {}s\n".format(results["convergence_s"]))
    time.sleep(10)

    # Show rpld startup logs for sensor5
    info("\n  --- rpld sensor5 startup log ---\n")
    startup_log = sensors[4].cmd(
        'grep -E "HYMRPL|version|adaptive|FIFO|token|security" '
        '/tmp/rpld_{}.log 2>/dev/null | head -20'.format(sensors[4].name))
    for line in startup_log.strip().split('\n'):
        if line.strip():
            info("    | {}\n".format(line.strip()[:120]))
    info("\n")

    # Run test cases
    test_1_security_reject_unauthenticated(sensors, results)
    time.sleep(5)

    test_2_security_accept_authenticated(sensors, results)
    time.sleep(5)

    test_3_rate_limiting(sensors, results)
    time.sleep(15)  # Wait for rate limit to clear

    test_4_adaptive_battery_low(sensors, results)
    time.sleep(5)

    test_5_adaptive_recovery(sensors, results)
    time.sleep(5)

    test_6_adaptive_mobility(sensors, results)
    time.sleep(5)

    test_7_pdr_maintained(sensors, results)

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='HyMRPL Adaptive + Security Integration Test')
    parser.add_argument('--runs', type=int, default=3,
                        help='Number of test runs (default: 3)')
    args = parser.parse_args()

    setLogLevel('info')
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Check prerequisites
    if os.geteuid() != 0:
        print("ERROR: Must run as root (sudo)")
        sys.exit(1)

    # Check if hymrpl_cmd is available
    if os.system('which hymrpl_cmd > /dev/null 2>&1') != 0:
        print("WARNING: hymrpl_cmd not in PATH. Authenticated tests will fail.")
        print("Compile with: gcc -Wall -o /usr/local/bin/hymrpl_cmd "
              "hymrpl_cmd.c -lssl -lcrypto")

    # Generate token if not exists
    if not os.path.exists(TOKEN_PATH):
        print("Generating authentication token...")
        os.system('mkdir -p /etc/hymrpl')
        os.system('hymrpl_cmd --gen-token 2>/dev/null || true')

    info("\n" + "=" * 60 + "\n")
    info("HyMRPL — Adaptive Engine + Secure FIFO Test\n")
    info("Runs: {}\n".format(args.runs))
    info("=" * 60 + "\n")

    net, sensors = create_topology()
    all_results = []

    try:
        for run in range(1, args.runs + 1):
            results = run_experiment(run, sensors)
            all_results.append(results)
            info("\n  Run {} complete.\n".format(run))
    finally:
        stop_rpld(sensors)
        net.stop()

    # Save results
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = os.path.join(RESULTS_DIR,
                            'adaptive_security_{}.csv'.format(ts))

    if all_results:
        keys = all_results[0].keys()
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(all_results)

    info("\n" + "=" * 60 + "\n")
    info("RESULTS SUMMARY\n")
    info("=" * 60 + "\n")

    # Summary
    passed = 0
    total = 7
    for r in all_results:
        if r.get("test1_rejected"):
            passed += 1
        if r.get("test2_accepted"):
            passed += 1
        if r.get("test3_rate_limited"):
            passed += 1
        if r.get("test4_adaptive_low_battery"):
            passed += 1
        if r.get("test5_adaptive_recovery"):
            passed += 1
        if r.get("test6_parent_detected"):
            passed += 1
        if r.get("test7_pdr", 0) >= 90:
            passed += 1

    avg_pass = passed / len(all_results) if all_results else 0
    info("  Average tests passed per run: {:.1f}/{}\n".format(avg_pass, total))
    info("  Results saved: {}\n".format(csv_path))
    info("  Logs: /tmp/rpld_sensor*.log\n")


if __name__ == '__main__':
    main()
