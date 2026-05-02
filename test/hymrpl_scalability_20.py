#!/usr/bin/env python3
"""
HyMRPL — Scalability Test with 20 nodes

Tree topology with 4 branches, maximum depth of 5 hops:

    sensor1 (Root, S)
    ├── sensor2 (S) ── sensor6 (N) ── sensor10 (N) ── sensor14 (N) ── sensor18 (N)
    ├── sensor3 (S) ── sensor7 (N) ── sensor11 (N) ── sensor15 (N) ── sensor19 (N)
    ├── sensor4 (S) ── sensor8 (N) ── sensor12 (S) ── sensor16 (N) ── sensor20 (N)
    └── sensor5 (N) ── sensor9 (N) ── sensor13 (S) ── sensor17 (N)

Collected metrics:
  - Convergence time (until the farthest node responds)
  - PDR and latency for nodes at 1, 2, 3, 4 and 5 hops
  - Root CPU and memory
  - SRH and hop-by-hop route count
  - DIO messages captured at root (15s)

Usage: sudo python3 hymrpl_scalability_20.py [--runs 3] [--modes storing nonstoring hybrid]
"""

import time, re, csv, os, sys, statistics, subprocess
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
PING_COUNT = 30
NUM_NODES = 20

CONVERGENCE_ADDR_TIMEOUT = 180
CONVERGENCE_PING_TIMEOUT = 300

# Topology: (parent_idx, child_idx) — indices 0-based
# Branch 1: 0-1-5-9-13-17
# Branch 2: 0-2-6-10-14-18
# Branch 3: 0-3-7-11-15-19
# Branch 4: 0-4-8-12-16
LINKS = [
    (0, 1), (0, 2), (0, 3), (0, 4),       # 1-hop
    (1, 5), (2, 6), (3, 7), (4, 8),        # 2-hop
    (5, 9), (6, 10), (7, 11), (8, 12),     # 3-hop
    (9, 13), (10, 14), (11, 15), (12, 16), # 4-hop
    (13, 17), (14, 18), (15, 19),          # 5-hop
]

# Class per node in hybrid mode: intermediate nodes with resources = S, leaf/constrained = N
HYBRID_CLASSES = {}
for i in range(NUM_NODES):
    name = 'sensor{}'.format(i + 1)
    if i == 0:
        HYBRID_CLASSES[name] = 'S'  # root
    elif i in (1, 2, 3, 11, 12):
        HYBRID_CLASSES[name] = 'S'  # intermediate nodes with resources
    else:
        HYBRID_CLASSES[name] = 'N'  # leaf or constrained nodes

# Test pairs: root -> nodes at different depths + upward from farthest
TEST_PAIRS = [
    (0, 1, "1-hop"),   # root -> sensor2
    (0, 5, "2-hop"),   # root -> sensor6
    (0, 9, "3-hop"),   # root -> sensor10
    (0, 13, "4-hop"),  # root -> sensor14
    (0, 17, "5-hop"),  # root -> sensor18
    (17, 0, "5-hop-up"),  # sensor18 -> root
    # Cross-branch
    (0, 19, "5-hop-b3"),  # root -> sensor20 (branch 3)
    (0, 16, "4-hop-b4"),  # root -> sensor17 (branch 4)
]


def get_iface_name(node):
    output = node.cmd('ip link show {}-pan0 2>/dev/null'.format(node.name))
    if 'does not exist' not in output and output.strip() and 'pan0' in output:
        return '{}-pan0'.format(node.name)
    return '{}-pan0'.format(node.name)


def gen_config(node, mode, node_class="S"):
    iface = get_iface_name(node)
    is_root = node.params.get('dodag_root', False)
    mop = {'storing': 2, 'nonstoring': 1, 'hybrid': 6}[mode]
    cmd = 'ifaces = { {\n'
    cmd += '        ifname = "{}",\n'.format(iface)
    cmd += '        dodag_root = {},\n'.format('true' if is_root else 'false')
    if mode == 'hybrid':
        cmd += '        node_class = "{}",\n'.format(node_class)
    cmd += '        mode_of_operation = {},\n'.format(mop)
    cmd += '        trickle_t = 3,\n'
    if is_root:
        cmd += '        rpls = { {\n'
        cmd += '               instance = 1,\n'
        cmd += '               dags = { {\n'
        cmd += '                       mode_of_operation = {},\n'.format(mop)
        if mode == 'hybrid':
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
    """Creates tree topology with 20 nodes and 4 branches."""
    net = Mininet_wifi()
    sensors = []
    for i in range(NUM_NODES):
        name = 'sensor{}'.format(i + 1)
        ip6 = 'fe80::{:x}/64'.format(i + 1)
        params = {'ip6': ip6, 'panid': '0xbeef'}
        if i == 0:
            params['dodag_root'] = True
        s = net.addSensor(name, **params)
        sensors.append(s)

    net.configureNodes()

    for parent_idx, child_idx in LINKS:
        net.addLink(sensors[parent_idx], sensors[child_idx], cls=LoWPAN)

    net.build()

    # Wait for interfaces to be ready (with 20 nodes, needs more time)
    info("  Waiting 15s for all interfaces to come up...\n")
    time.sleep(15)

    # Check if all interfaces exist
    ready = 0
    for s in sensors:
        iface = get_iface_name(s)
        out = s.cmd('ip link show {} 2>/dev/null'.format(iface))
        if iface in out and 'does not exist' not in out:
            ready += 1
        else:
            info("  WARNING: {} interface {} not found\n".format(s.name, iface))
    info("  Interfaces ready: {}/{}\n".format(ready, NUM_NODES))

    return net, sensors


def wait_iface_ready(sensor, timeout=20):
    """Waits for the sensor's pan0 interface to become available."""
    iface = get_iface_name(sensor)
    for _ in range(timeout):
        out = sensor.cmd('ip link show {} 2>/dev/null'.format(iface))
        if iface in out and 'does not exist' not in out:
            return True
        time.sleep(1)
    return False


def start_rpld(sensors, mode):
    """Starts rpld with staggered delays by depth, checking interfaces."""
    # Organize nodes by depth (BFS)
    depth = {0: 0}
    children = {i: [] for i in range(NUM_NODES)}
    for p, c in LINKS:
        children[p].append(c)
        depth[c] = depth[p] + 1

    max_depth = max(depth.values())
    for d in range(max_depth + 1):
        nodes_at_depth = [i for i, dd in depth.items() if dd == d]
        for idx in nodes_at_depth:
            s = sensors[idx]
            # Check if interface exists before starting
            if not wait_iface_ready(s, timeout=10):
                info("  WARNING: {} interface not ready, starting rpld anyway\n".format(s.name))
            cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
            conf = gen_config(s, mode, cls)
            s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
                conf, s.name))
        if d == 0:
            time.sleep(5)  # root needs more time
        else:
            time.sleep(3)  # more time between layers with 20 nodes


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


def get_global_addr(sensor):
    iface = get_iface_name(sensor)
    output = sensor.cmd('ip -6 addr show {} | grep "scope global"'.format(iface))
    match = re.search(r'inet6\s+(\S+)/64', output)
    return match.group(1) if match else None


def wait_for_global_addr(sensor, timeout=CONVERGENCE_ADDR_TIMEOUT):
    for _ in range(timeout):
        addr = get_global_addr(sensor)
        if addr:
            return addr
        time.sleep(1)
    return None


def wait_for_convergence(src, dst_addr, max_attempts=CONVERGENCE_PING_TIMEOUT):
    start = time.time()
    for i in range(max_attempts):
        result = src.cmd('ping6 -c 1 -W 2 {}'.format(dst_addr))
        if '1 received' in result:
            return time.time() - start
        time.sleep(0.5 if i < 30 else 1.0)
    return -1


def percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def measure_pdr_latency(src, dst_addr, count=PING_COUNT):
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


def count_routes(sensor):
    output = sensor.cmd('ip -6 route show')
    return {"routes_total": output.count('proto'),
            "routes_srh": output.count('encap rpl'),
            "routes_via": output.count('via fe80')}


def measure_cpu_mem(sensor):
    output = sensor.cmd('ps -o %cpu,%mem,rss -C rpld --no-headers 2>/dev/null')
    if not output.strip():
        return 0, 0
    parts = output.split()
    try:
        return float(parts[0]), int(parts[2]) / 1024.0
    except (IndexError, ValueError):
        return 0, 0


def count_dio_messages(sensor, duration=15):
    """Captures DIOs at root for N seconds."""
    iface = get_iface_name(sensor)
    pcap = '/tmp/dio_count_{}.pcap'.format(sensor.name)
    sensor.cmd('timeout {} tcpdump -i {} -w {} icmp6 2>/dev/null &'.format(
        duration, iface, pcap))
    time.sleep(duration + 2)
    # Counts DIOs (ICMPv6 code 1 within type 155)
    try:
        out = subprocess.check_output(
            'tshark -r {} -Y "icmpv6.type == 155 && icmpv6.code == 1" '
            '-T fields -e frame.number 2>/dev/null | wc -l'.format(pcap),
            shell=True).decode().strip()
        return int(out)
    except (subprocess.CalledProcessError, ValueError):
        return -1


def check_full_convergence(sensors):
    """Checks how many nodes have a global address (convergence indicator)."""
    count = 0
    for s in sensors:
        if get_global_addr(s):
            count += 1
    return count


def run_single(sensors, mode, run_id, runs_total):
    info("=== {} | Run {}/{} | {} nodes ===\n".format(
        mode.upper(), run_id, runs_total, NUM_NODES))
    results = {"mode": mode, "run": run_id, "num_nodes": NUM_NODES}

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    start_time = time.time()
    start_rpld(sensors, mode)

    # Wait for the farthest node (sensor18, idx=17) to get an address
    info("  Waiting for farthest node (sensor18) global address...\n")
    farthest_addr = wait_for_global_addr(sensors[17])
    if not farthest_addr:
        info("  FAIL: sensor18 never got a global address\n")
        # Check how many converged
        converged = check_full_convergence(sensors)
        info("  Converged nodes: {}/{}\n".format(converged, NUM_NODES))
        results["convergence_s"] = -1
        results["converged_nodes"] = converged
        return results

    info("  sensor18 got address: {}\n".format(farthest_addr))

    # Measure end-to-end convergence
    conv = wait_for_convergence(sensors[0], farthest_addr)
    if conv < 0:
        info("  FAIL: root cannot reach sensor18\n")
        converged = check_full_convergence(sensors)
        results["convergence_s"] = -1
        results["converged_nodes"] = converged
        return results

    conv_time = time.time() - start_time
    results["convergence_s"] = round(conv_time, 2)
    converged = check_full_convergence(sensors)
    results["converged_nodes"] = converged
    info("  Convergence: {}s ({}/{} nodes)\n".format(
        results["convergence_s"], converged, NUM_NODES))

    # Stabilization
    info("  Stabilizing (15s)...\n")
    time.sleep(15)

    # Collect addresses
    addrs = {}
    for s in sensors:
        addrs[s.name] = get_global_addr(s)

    # PDR/Latency by depth
    for src_idx, dst_idx, desc in TEST_PAIRS:
        src = sensors[src_idx]
        dst = sensors[dst_idx]
        dst_addr = addrs.get(dst.name)
        if not dst_addr:
            info("  SKIP {} -> {}: no address\n".format(src.name, dst.name))
            continue
        info("  {} -> {} ({})...\n".format(src.name, dst.name, desc))
        m = measure_pdr_latency(src, dst_addr)
        key = "{}to{}".format(src_idx + 1, dst_idx + 1)
        results["{}_pdr".format(key)] = round(m["pdr"], 1)
        results["{}_lat_avg".format(key)] = round(m["lat_avg"], 3)
        results["{}_lat_p95".format(key)] = round(m["lat_p95"], 3)

    # Aggregate PDR: root -> all nodes
    info("  Measuring aggregate PDR (root -> all nodes)...\n")
    total_tx, total_rx = 0, 0
    for i in range(1, NUM_NODES):
        dst_addr = addrs.get(sensors[i].name)
        if not dst_addr:
            continue
        result = sensors[0].cmd('ping6 -c 10 -i 0.2 -W 2 {}'.format(dst_addr))
        match = re.search(r'(\d+) packets transmitted, (\d+) received', result)
        if match:
            total_tx += int(match.group(1))
            total_rx += int(match.group(2))
    results["aggregate_pdr"] = round((total_rx / total_tx) * 100, 1) if total_tx > 0 else 0
    info("  Aggregate PDR: {:.1f}% ({}/{})\n".format(
        results["aggregate_pdr"], total_rx, total_tx))

    # Routes at root
    routes = count_routes(sensors[0])
    results.update(routes)

    # Root CPU/Mem
    cpu, mem = measure_cpu_mem(sensors[0])
    results["root_cpu"] = cpu
    results["root_mem_mb"] = round(mem, 2)

    # Average CPU/Mem of nodes
    all_cpu, all_mem = [], []
    for s in sensors:
        c, m = measure_cpu_mem(s)
        all_cpu.append(c)
        all_mem.append(m)
    results["avg_cpu"] = round(statistics.mean(all_cpu), 2) if all_cpu else 0
    results["avg_mem_mb"] = round(statistics.mean(all_mem), 2) if all_mem else 0

    # DIO count at root
    info("  Counting DIO messages (15s)...\n")
    dio = count_dio_messages(sensors[0], duration=15)
    results["root_dio_15s"] = dio

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
    modes = {}
    for r in all_results:
        modes.setdefault(r["mode"], []).append(r)

    print("\n" + "=" * 70)
    print("SCALABILITY TEST — {} NODES".format(NUM_NODES))
    print("=" * 70)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        ok = [r for r in rows if r.get("convergence_s", -1) != -1]
        fail = len(rows) - len(ok)
        print("\n--- {} ({} runs, {} ok, {} failed) ---".format(
            mode.upper(), len(rows), len(ok), fail))

        def stat(key):
            vals = [r.get(key, 0) for r in ok if r.get(key, 0) not in (0, -1, None)]
            if not vals:
                return "N/A"
            avg = statistics.mean(vals)
            std = statistics.stdev(vals) if len(vals) > 1 else 0
            return "{:.2f} +/- {:.2f}".format(avg, std)

        print("  Convergence:     {}s".format(stat("convergence_s")))
        print("  Converged nodes: {}".format(stat("converged_nodes")))
        print("  Aggregate PDR:   {}%".format(stat("aggregate_pdr")))
        print("  PDR 1-hop:       {}%".format(stat("1to2_pdr")))
        print("  PDR 3-hop:       {}%".format(stat("1to10_pdr")))
        print("  PDR 5-hop:       {}%".format(stat("1to18_pdr")))
        print("  Lat 1-hop:       {}ms".format(stat("1to2_lat_avg")))
        print("  Lat 3-hop:       {}ms".format(stat("1to10_lat_avg")))
        print("  Lat 5-hop:       {}ms".format(stat("1to18_lat_avg")))
        print("  Root CPU:        {}%".format(stat("root_cpu")))
        print("  Root mem:        {}MB".format(stat("root_mem_mb")))
        print("  Avg CPU:         {}%".format(stat("avg_cpu")))
        print("  Avg mem:         {}MB".format(stat("avg_mem_mb")))
        print("  Root DIO (15s):  {}".format(stat("root_dio_15s")))
        print("  Routes SRH:      {}".format(stat("routes_srh")))
        print("  Routes via:      {}".format(stat("routes_via")))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--modes', nargs='+', default=['storing', 'nonstoring', 'hybrid'],
                        choices=['storing', 'nonstoring', 'hybrid'])
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    all_results = []

    # Create topology ONCE and reuse for all modes
    info("*** Creating topology (persistent for all modes)\n")
    net, sensors = create_topology()

    for mode in args.modes:
        info("\n### SCALABILITY TEST: {} | {} nodes ###\n".format(mode.upper(), NUM_NODES))

        for run_id in range(1, args.runs + 1):
            try:
                r = run_single(sensors, mode, run_id, args.runs)
                all_results.append(r)
                save_csv(all_results, os.path.join(
                    RESULTS_DIR, "scalability_{}_{}.csv".format(NUM_NODES, ts)))
            except Exception as e:
                import traceback
                info("ERROR run {}: {}\n".format(run_id, e))
                info(traceback.format_exc() + "\n")
                all_results.append({"mode": mode, "run": run_id,
                                    "num_nodes": NUM_NODES, "convergence_s": -1})

    # Save results BEFORE stopping the network
    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "scalability_{}_{}.csv".format(NUM_NODES, ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults: {}".format(csv_path))

    # net.stop() may kill the process, so it's the last thing
    info("\n*** Results saved. Stopping network...\n")
    try:
        net.stop()
    except Exception:
        pass


if __name__ == '__main__':
    setLogLevel('info')
    main()
