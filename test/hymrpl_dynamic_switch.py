#!/usr/bin/env python3
"""
HyMRPL — Dynamic class switch experiment via FIFO.

Demonstrates that HyMRPL adapts forwarding at runtime:
  1. sensor5 starts as Class N (non-storing-like)
  2. Measures latency root→sensor5 and local sensor4→sensor5
  3. Sends "CLASS_S" to sensor5's FIFO
  4. Waits for reconvergence
  5. Measures latency again (behavior should change)
  6. Switches back to CLASS_N and measures again

Topology:
    sensor1 (Root, S)
       /        \
  sensor2(N)   sensor3(S)
                  |
               sensor4(S)
                  |
               sensor5(N → S → N)

Usage: sudo python3 hymrpl_dynamic_switch.py [--runs 3]
"""

import time, re, csv, os, statistics
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
FIFO_PATH = "/tmp/hymrpl_cmd"

HYBRID_CLASSES = {
    'sensor1': 'S',
    'sensor2': 'N',
    'sensor3': 'S',
    'sensor4': 'S',
    'sensor5': 'N',  # starts as N, will switch
}


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


def gen_config(node, mode, node_class="S"):
    iface = get_iface_name(node)
    is_root = node.params.get('dodag_root', False)
    mop = 6  # always hybrid
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
    root = sensors[0]
    cls = HYBRID_CLASSES.get(root.name, 'S')
    conf = gen_config(root, 'hybrid', cls)
    root.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, root.name))
    time.sleep(3)
    for s in [sensors[1], sensors[2]]:
        cls = HYBRID_CLASSES.get(s.name, 'S')
        conf = gen_config(s, 'hybrid', cls)
        s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))
    time.sleep(2)
    for s in [sensors[3], sensors[4]]:
        cls = HYBRID_CLASSES.get(s.name, 'S')
        conf = gen_config(s, 'hybrid', cls)
        s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))
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


def percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def measure_pdr_latency(src, dst_addr, count=30):
    result = src.cmd('ping6 -c {} -i 0.2 -W 2 {}'.format(count, dst_addr))
    match = re.search(r'(\d+) packets transmitted, (\d+) received', result)
    if not match:
        return {"pdr": 0, "lat_avg": 0, "lat_p95": 0}
    tx, rx = int(match.group(1)), int(match.group(2))
    pdr = (rx / tx) * 100.0 if tx > 0 else 0
    lat_match = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', result)
    lat_avg = float(lat_match.group(2)) if lat_match else 0
    lat_values = [float(m.group(1)) for m in re.finditer(r'time=([\d.]+)', result)]
    return {"pdr": pdr, "lat_avg": lat_avg, "lat_p95": percentile(lat_values, 95)}


def send_fifo_cmd(sensor, cmd_str):
    """Sends command to the rpld FIFO running in the sensor's namespace."""
    sensor.cmd('echo "{}" > {} 2>/dev/null'.format(cmd_str, FIFO_PATH))
    info("  FIFO: sent '{}' to {}\n".format(cmd_str, sensor.name))


def count_routes(sensor):
    output = sensor.cmd('ip -6 route show')
    return {"srh": output.count('encap rpl'), "via": output.count('via fe80')}


def run_experiment(sensors, run_id):
    """
    Phases:
      A: sensor5 as Class N (initial state)
      B: switch sensor5 to Class S via FIFO
      C: switch sensor5 back to Class N via FIFO
    """
    info("\n=== DYNAMIC SWITCH | Run {} ===\n".format(run_id))
    results = {"run": run_id}

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    start_time = time.time()
    start_rpld(sensors)

    # Wait convergence
    info("  Waiting for convergence...\n")
    addr5 = wait_for_global_addr(sensors[4])
    if not addr5:
        info("  FAIL: sensor5 no address\n")
        results["convergence_s"] = -1
        return results

    addr4 = wait_for_global_addr(sensors[3])
    conv = wait_for_convergence(sensors[0], addr5)
    if conv < 0:
        info("  FAIL: no convergence\n")
        results["convergence_s"] = -1
        return results

    results["convergence_s"] = round(time.time() - start_time, 2)
    info("  Convergence: {}s\n".format(results["convergence_s"]))
    time.sleep(10)

    # ============================================================
    # PHASE A: sensor5 = Class N (initial state)
    # ============================================================
    info("\n  --- PHASE A: sensor5 = Class N (initial) ---\n")

    # root → sensor5
    m = measure_pdr_latency(sensors[0], addr5, count=50)
    results["A_root_s5_lat"] = round(m["lat_avg"], 3)
    results["A_root_s5_p95"] = round(m["lat_p95"], 3)
    results["A_root_s5_pdr"] = round(m["pdr"], 1)
    info("    root→s5: lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # sensor4 → sensor5 (local)
    if addr5:
        m = measure_pdr_latency(sensors[3], addr5, count=50)
        results["A_s4s5_lat"] = round(m["lat_avg"], 3)
        results["A_s4s5_p95"] = round(m["lat_p95"], 3)
        results["A_s4s5_pdr"] = round(m["pdr"], 1)
        info("    s4→s5:   lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # sensor5 → root (upward)
    root_addr = get_global_addr(sensors[0])
    if root_addr:
        m = measure_pdr_latency(sensors[4], root_addr, count=50)
        results["A_s5root_lat"] = round(m["lat_avg"], 3)
        results["A_s5root_p95"] = round(m["lat_p95"], 3)
        results["A_s5root_pdr"] = round(m["pdr"], 1)
        info("    s5→root: lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # Routes before switch
    r4 = count_routes(sensors[3])
    r5 = count_routes(sensors[4])
    results["A_s4_via"] = r4["via"]
    results["A_s5_via"] = r5["via"]
    results["A_s5_srh"] = r5["srh"]
    info("    Routes: s4 via={} | s5 via={} srh={}\n".format(r4["via"], r5["via"], r5["srh"]))

    # ============================================================
    # PHASE B: Switch sensor5 to Class S
    # ============================================================
    info("\n  --- PHASE B: Switching sensor5 N → S ---\n")
    send_fifo_cmd(sensors[4], "CLASS_S")
    time.sleep(2)

    # Trigger DAO re-send: sensor5 needs to re-announce itself
    # The rpld should handle this internally after class change,
    # but we wait for routes to update
    info("  Waiting for route update (15s)...\n")
    time.sleep(15)

    # root → sensor5
    m = measure_pdr_latency(sensors[0], addr5, count=50)
    results["B_root_s5_lat"] = round(m["lat_avg"], 3)
    results["B_root_s5_p95"] = round(m["lat_p95"], 3)
    results["B_root_s5_pdr"] = round(m["pdr"], 1)
    info("    root→s5: lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # sensor4 → sensor5 (local — should now use local route if S)
    if addr5:
        m = measure_pdr_latency(sensors[3], addr5, count=50)
        results["B_s4s5_lat"] = round(m["lat_avg"], 3)
        results["B_s4s5_p95"] = round(m["lat_p95"], 3)
        results["B_s4s5_pdr"] = round(m["pdr"], 1)
        info("    s4→s5:   lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # sensor5 → root
    if root_addr:
        m = measure_pdr_latency(sensors[4], root_addr, count=50)
        results["B_s5root_lat"] = round(m["lat_avg"], 3)
        results["B_s5root_p95"] = round(m["lat_p95"], 3)
        results["B_s5root_pdr"] = round(m["pdr"], 1)
        info("    s5→root: lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # Routes after switch to S
    r4 = count_routes(sensors[3])
    r5 = count_routes(sensors[4])
    results["B_s4_via"] = r4["via"]
    results["B_s5_via"] = r5["via"]
    results["B_s5_srh"] = r5["srh"]
    info("    Routes: s4 via={} | s5 via={} srh={}\n".format(r4["via"], r5["via"], r5["srh"]))

    # ============================================================
    # PHASE C: Switch sensor5 back to Class N
    # ============================================================
    info("\n  --- PHASE C: Switching sensor5 S → N ---\n")
    send_fifo_cmd(sensors[4], "CLASS_N")
    time.sleep(2)
    info("  Waiting for route update (15s)...\n")
    time.sleep(15)

    # root → sensor5
    m = measure_pdr_latency(sensors[0], addr5, count=50)
    results["C_root_s5_lat"] = round(m["lat_avg"], 3)
    results["C_root_s5_p95"] = round(m["lat_p95"], 3)
    results["C_root_s5_pdr"] = round(m["pdr"], 1)
    info("    root→s5: lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # sensor4 → sensor5
    if addr5:
        m = measure_pdr_latency(sensors[3], addr5, count=50)
        results["C_s4s5_lat"] = round(m["lat_avg"], 3)
        results["C_s4s5_p95"] = round(m["lat_p95"], 3)
        results["C_s4s5_pdr"] = round(m["pdr"], 1)
        info("    s4→s5:   lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # sensor5 → root
    if root_addr:
        m = measure_pdr_latency(sensors[4], root_addr, count=50)
        results["C_s5root_lat"] = round(m["lat_avg"], 3)
        results["C_s5root_p95"] = round(m["lat_p95"], 3)
        results["C_s5root_pdr"] = round(m["pdr"], 1)
        info("    s5→root: lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

    # Routes after switch back to N
    r4 = count_routes(sensors[3])
    r5 = count_routes(sensors[4])
    results["C_s4_via"] = r4["via"]
    results["C_s5_via"] = r5["via"]
    results["C_s5_srh"] = r5["srh"]
    info("    Routes: s4 via={} | s5 via={} srh={}\n".format(r4["via"], r5["via"], r5["srh"]))

    return results


def save_csv(results, path):
    if not results:
        return
    keys = sorted(set().union(*(r.keys() for r in results)))
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(results)


def print_summary(all_results):
    ok = [r for r in all_results if r.get("convergence_s", -1) != -1]
    if not ok:
        print("No successful runs.")
        return

    def avg(key):
        vals = [r.get(key, -1) for r in ok if r.get(key, -1) not in (-1, None)]
        return statistics.mean(vals) if vals else None

    print("\n" + "=" * 65)
    print("DYNAMIC CLASS SWITCH — SUMMARY ({} runs)".format(len(ok)))
    print("=" * 65)
    print("Convergence: {:.2f}s\n".format(avg("convergence_s")))

    phases = [
        ("A", "sensor5 = N (initial)"),
        ("B", "sensor5 = S (after switch)"),
        ("C", "sensor5 = N (reverted)"),
    ]
    print("{:<12} {:>12} {:>12} {:>12} {:>8} {:>8}".format(
        "Phase", "root→s5", "s4→s5", "s5→root", "s4 via", "s5 via"))
    print("-" * 65)
    for phase, desc in phases:
        rs5 = avg("{}_root_s5_lat".format(phase))
        s4s5 = avg("{}_s4s5_lat".format(phase))
        s5r = avg("{}_s5root_lat".format(phase))
        s4v = avg("{}_s4_via".format(phase))
        s5v = avg("{}_s5_via".format(phase))
        print("{:<12} {:>10.3f}ms {:>10.3f}ms {:>10.3f}ms {:>8.0f} {:>8.0f}  {}".format(
            phase,
            rs5 if rs5 else 0,
            s4s5 if s4s5 else 0,
            s5r if s5r else 0,
            s4v if s4v else 0,
            s5v if s5v else 0,
            desc))

    print("\nExpected behavior:")
    print("  Phase A (N): s4→s5 traffic goes via root (higher latency)")
    print("  Phase B (S): s4→s5 traffic uses local route (lower latency)")
    print("  Phase C (N): s4→s5 traffic returns to via root")

    b_lat = avg("B_s4s5_lat")
    a_lat = avg("A_s4s5_lat")
    if b_lat and a_lat and a_lat > 0:
        change = ((a_lat - b_lat) / a_lat) * 100
        print("\n  Latency change s4→s5 after N→S: {:.1f}%".format(change))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    all_results = []

    info("*** Creating topology\n")
    net, sensors = create_topology()

    for run_id in range(1, args.runs + 1):
        try:
            r = run_experiment(sensors, run_id)
            all_results.append(r)
        except Exception as e:
            import traceback
            info("ERROR: {}\n".format(e))
            info(traceback.format_exc() + "\n")
            all_results.append({"run": run_id, "convergence_s": -1})

    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "dynamic_switch_{}.csv".format(ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults: {}".format(csv_path))

    info("\n*** Stopping network...\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
