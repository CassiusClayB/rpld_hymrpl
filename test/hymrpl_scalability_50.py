#!/usr/bin/env python3
"""
HyMRPL — Scalability Test with 50 nodes

Tree topology with 5 branches, maximum depth of 10 hops:

    sensor1 (Root, S)
    ├── Branch 1: sensor2(S)─sensor7(N)─sensor12(N)─sensor17(N)─sensor22(N)─sensor27(N)─sensor32(N)─sensor37(N)─sensor42(N)─sensor47(N)
    ├── Branch 2: sensor3(S)─sensor8(N)─sensor13(S)─sensor18(N)─sensor23(N)─sensor28(N)─sensor33(N)─sensor38(N)─sensor43(N)─sensor48(N)
    ├── Branch 3: sensor4(S)─sensor9(N)─sensor14(N)─sensor19(S)─sensor24(N)─sensor29(N)─sensor34(N)─sensor39(N)─sensor44(N)─sensor49(N)
    ├── Branch 4: sensor5(S)─sensor10(N)─sensor15(S)─sensor20(N)─sensor25(N)─sensor30(N)─sensor35(N)─sensor40(N)─sensor45(N)─sensor50(N)
    └── Branch 5: sensor6(S)─sensor11(N)─sensor16(N)─sensor21(N)─sensor26(N)─sensor31(N)─sensor36(N)─sensor41(N)─sensor46(N)

Collected metrics:
  - Convergence time (until the farthest node responds)
  - PDR and latency for nodes at 1, 3, 5, 7, 9 and 10 hops
  - Aggregate PDR (root -> all nodes)
  - Root CPU and memory
  - SRH and hop-by-hop route count
  - DIO messages captured at root (15s)

Usage: sudo python3 hymrpl_scalability_50.py [--runs 3] [--modes storing nonstoring hybrid]
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
NUM_NODES = 50

CONVERGENCE_ADDR_TIMEOUT = 300
CONVERGENCE_PING_TIMEOUT = 400

# Build topology: 5 branches from root, each branch up to 9 more nodes (10 hops max)
# Branch layout: root -> branch_head -> 8 more nodes in chain
# Nodes 1=root, 2-6=branch heads (depth 1), 7-11=depth 2, 12-16=depth 3, etc.
# Node indexing (0-based): root=0, branch heads=1..5, then by layer

NUM_BRANCHES = 5
MAX_CHAIN_LENGTH = 9  # nodes per branch after the head (total depth = 10 for longest)

# Build links programmatically
LINKS = []
# Root (idx 0) -> branch heads (idx 1..5)
for b in range(NUM_BRANCHES):
    LINKS.append((0, b + 1))

# Each branch: chain from branch head
# Branch b has nodes at indices: head = b+1, then layer nodes
# Layout: layer k nodes for branch b are at index: 1 + NUM_BRANCHES + k*NUM_BRANCHES + b
# But branch 5 (idx=5) has only 8 chain nodes (total 49 non-root = 5 heads + 44 chain)
# Let's compute: 50 nodes total. root + 5 heads + 44 chain nodes.
# Distribute: branches 1-4 get 9 chain nodes each (36), branch 5 gets 8 chain nodes (8) = 44. OK.

# Build it layer by layer
# Layer 0: root (idx 0)
# Layer 1 (depth 1): branch heads idx 1-5
# Layer 2 (depth 2): idx 6-10 (chain node 1 of each branch)
# Layer 3 (depth 3): idx 11-15
# ...
# Layer k (depth k): idx 1 + (k-1)*5 ... 1 + (k-1)*5 + 4
# But we have 50 nodes: root + 5 heads + 44 chain = 50. 44/5 = 8.8, so 4 branches get 9, 1 gets 8.

# Simpler approach: build nodes sequentially per branch
def build_topology_links():
    """
    Builds links for a 50-node tree.
    root=0, branches start at idx 1.
    """
    links = []
    node_idx = 1  # next available node index (0 is root)
    branch_nodes = {}  # branch_id -> list of node indices in chain order

    # Branch heads
    for b in range(NUM_BRANCHES):
        links.append((0, node_idx))
        branch_nodes[b] = [node_idx]
        node_idx += 1

    # Remaining nodes distributed across branches
    remaining = NUM_NODES - 1 - NUM_BRANCHES  # 44 nodes
    chain_per_branch = remaining // NUM_BRANCHES  # 8
    extra = remaining % NUM_BRANCHES  # 4 branches get 9

    for b in range(NUM_BRANCHES):
        chain_len = chain_per_branch + (1 if b < extra else 0)
        parent = branch_nodes[b][-1]
        for _ in range(chain_len):
            links.append((parent, node_idx))
            branch_nodes[b].append(node_idx)
            parent = node_idx
            node_idx += 1

    return links, branch_nodes


LINKS, BRANCH_NODES = build_topology_links()

# Compute depth per node
DEPTH = {0: 0}
for p, c in LINKS:
    DEPTH[c] = DEPTH[p] + 1

MAX_DEPTH = max(DEPTH.values())

# Class assignment for hybrid mode:
# Root = S, branch heads = S, every 3rd intermediate = S, rest = N
HYBRID_CLASSES = {}
for i in range(NUM_NODES):
    name = 'sensor{}'.format(i + 1)
    if i == 0:
        HYBRID_CLASSES[name] = 'S'  # root
    elif i < 1 + NUM_BRANCHES:
        HYBRID_CLASSES[name] = 'S'  # branch heads
    elif DEPTH.get(i, 0) % 3 == 0:
        HYBRID_CLASSES[name] = 'S'  # every 3rd layer = S (intermediate with resources)
    else:
        HYBRID_CLASSES[name] = 'N'  # constrained

# Count classes
s_count = sum(1 for v in HYBRID_CLASSES.values() if v == 'S')
n_count = sum(1 for v in HYBRID_CLASSES.values() if v == 'N')

# Test pairs: root -> nodes at increasing depths + upward + cross-branch
# Find farthest node
farthest_idx = max(DEPTH, key=DEPTH.get)
TEST_PAIRS = []

# Add pairs at various depths
target_depths = [1, 3, 5, 7, MAX_DEPTH]
for td in target_depths:
    candidates = [idx for idx, d in DEPTH.items() if d == td and idx != 0]
    if candidates:
        target = candidates[0]
        TEST_PAIRS.append((0, target, "{}-hop".format(td)))

# Upward from farthest
TEST_PAIRS.append((farthest_idx, 0, "{}-hop-up".format(DEPTH[farthest_idx])))

# Cross-branch: root to last node of different branches
for b in range(min(3, NUM_BRANCHES)):
    last = BRANCH_NODES[b][-1]
    d = DEPTH[last]
    TEST_PAIRS.append((0, last, "{}-hop-b{}".format(d, b + 1)))


def get_iface_name(node):
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
    """Creates tree topology with 50 nodes."""
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

    info("  Waiting for all interfaces to come up ({} nodes)...\n".format(NUM_NODES))
    max_wait = 120  # up to 2 minutes
    start = time.time()
    ready = 0
    while time.time() - start < max_wait:
        ready = 0
        for s in sensors:
            iface = get_iface_name(s)
            out = s.cmd('ip link show {} 2>/dev/null'.format(iface))
            if iface in out and 'does not exist' not in out:
                ready += 1
        if ready >= NUM_NODES:
            break
        info("  {}/{} interfaces ready, waiting...\n".format(ready, NUM_NODES))
        time.sleep(10)

    info("  Interfaces ready: {}/{} (waited {:.0f}s)\n".format(
        ready, NUM_NODES, time.time() - start))

    return net, sensors


def wait_iface_ready(sensor, timeout=30):
    iface = get_iface_name(sensor)
    for _ in range(timeout):
        out = sensor.cmd('ip link show {} 2>/dev/null'.format(iface))
        if iface in out and 'does not exist' not in out:
            return True
        time.sleep(1)
    return False


def start_rpld(sensors, mode):
    """Starts rpld with staggered delays by depth."""
    for d in range(MAX_DEPTH + 1):
        nodes_at_depth = [i for i, dd in DEPTH.items() if dd == d]
        for idx in nodes_at_depth:
            s = sensors[idx]
            if not wait_iface_ready(s, timeout=10):
                info("  WARNING: {} interface not ready\n".format(s.name))
            cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
            conf = gen_config(s, mode, cls)
            s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
                conf, s.name))
        if d == 0:
            time.sleep(5)
        elif d <= 2:
            time.sleep(4)
        else:
            time.sleep(3)


def stop_rpld(sensors):
    for s in sensors:
        s.cmd('killall -9 rpld 2>/dev/null')
    time.sleep(3)


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
        time.sleep(0.5 if i < 60 else 1.0)
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
    iface = get_iface_name(sensor)
    pcap = '/tmp/dio_count_{}.pcap'.format(sensor.name)
    sensor.cmd('timeout {} tcpdump -i {} -w {} icmp6 2>/dev/null &'.format(
        duration, iface, pcap))
    time.sleep(duration + 2)
    try:
        out = subprocess.check_output(
            'tshark -r {} -Y "icmpv6.type == 155 && icmpv6.code == 1" '
            '-T fields -e frame.number 2>/dev/null | wc -l'.format(pcap),
            shell=True).decode().strip()
        return int(out)
    except (subprocess.CalledProcessError, ValueError):
        return -1


def check_full_convergence(sensors):
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
    time.sleep(5)

    start_time = time.time()
    start_rpld(sensors, mode)

    # Wait for the farthest node to get an address
    info("  Waiting for farthest node (sensor{}, depth={}) global address...\n".format(
        farthest_idx + 1, DEPTH[farthest_idx]))
    farthest_addr = wait_for_global_addr(sensors[farthest_idx])
    if not farthest_addr:
        info("  FAIL: sensor{} never got a global address\n".format(farthest_idx + 1))
        converged = check_full_convergence(sensors)
        info("  Converged nodes: {}/{}\n".format(converged, NUM_NODES))
        results["convergence_s"] = -1
        results["converged_nodes"] = converged
        return results

    info("  sensor{} got address: {}\n".format(farthest_idx + 1, farthest_addr))

    # Measure end-to-end convergence
    conv = wait_for_convergence(sensors[0], farthest_addr)
    if conv < 0:
        info("  FAIL: root cannot reach sensor{}\n".format(farthest_idx + 1))
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

    # Stabilization — longer for 50 nodes
    info("  Stabilizing (25s)...\n")
    time.sleep(25)

    # Collect addresses
    addrs = {}
    for s in sensors:
        addrs[s.name] = get_global_addr(s)

    # PDR/Latency for test pairs
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

    # Aggregate PDR: root -> all nodes (sample 20 nodes for efficiency)
    info("  Measuring aggregate PDR (root -> sampled nodes)...\n")
    total_tx, total_rx = 0, 0
    # Sample nodes at various depths
    sample_indices = set()
    for d in range(1, MAX_DEPTH + 1):
        candidates = [idx for idx, dd in DEPTH.items() if dd == d]
        # Take up to 2 per depth
        for c in candidates[:2]:
            sample_indices.add(c)
    # Also include all branch tips
    for b in range(NUM_BRANCHES):
        sample_indices.add(BRANCH_NODES[b][-1])

    for idx in sorted(sample_indices):
        dst_addr = addrs.get(sensors[idx].name)
        if not dst_addr:
            continue
        result = sensors[0].cmd('ping6 -c 10 -i 0.2 -W 2 {}'.format(dst_addr))
        match = re.search(r'(\d+) packets transmitted, (\d+) received', result)
        if match:
            total_tx += int(match.group(1))
            total_rx += int(match.group(2))

    results["aggregate_pdr"] = round((total_rx / total_tx) * 100, 1) if total_tx > 0 else 0
    results["sample_nodes_tested"] = len(sample_indices)
    info("  Aggregate PDR: {:.1f}% ({}/{}) from {} sampled nodes\n".format(
        results["aggregate_pdr"], total_rx, total_tx, len(sample_indices)))

    # Full PDR: root -> ALL nodes (10 pings each, less intensive)
    info("  Measuring full PDR (root -> all {} nodes, 5 pings each)...\n".format(NUM_NODES - 1))
    full_tx, full_rx = 0, 0
    unreachable = 0
    for i in range(1, NUM_NODES):
        dst_addr = addrs.get(sensors[i].name)
        if not dst_addr:
            unreachable += 1
            continue
        result = sensors[0].cmd('ping6 -c 5 -i 0.3 -W 2 {}'.format(dst_addr))
        match = re.search(r'(\d+) packets transmitted, (\d+) received', result)
        if match:
            full_tx += int(match.group(1))
            full_rx += int(match.group(2))
    results["full_pdr"] = round((full_rx / full_tx) * 100, 1) if full_tx > 0 else 0
    results["unreachable_nodes"] = unreachable
    info("  Full PDR: {:.1f}% (unreachable: {})\n".format(
        results["full_pdr"], unreachable))

    # Routes at root
    routes = count_routes(sensors[0])
    results.update(routes)

    # Root CPU/Mem
    cpu, mem = measure_cpu_mem(sensors[0])
    results["root_cpu"] = cpu
    results["root_mem_mb"] = round(mem, 2)

    # Average CPU/Mem across all nodes
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

    # Class distribution (for hybrid)
    if mode == 'hybrid':
        results["class_s_nodes"] = s_count
        results["class_n_nodes"] = n_count

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
    print("SCALABILITY TEST — {} NODES | {} branches | max depth {}".format(
        NUM_NODES, NUM_BRANCHES, MAX_DEPTH))
    print("Hybrid class distribution: {} S / {} N".format(s_count, n_count))
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
            vals = [r.get(key, 0) for r in ok if r.get(key) not in (0, -1, None)]
            if not vals:
                return "N/A"
            avg = statistics.mean(vals)
            std = statistics.stdev(vals) if len(vals) > 1 else 0
            return "{:.2f} +/- {:.2f}".format(avg, std)

        print("  Convergence:       {}s".format(stat("convergence_s")))
        print("  Converged nodes:   {}".format(stat("converged_nodes")))
        print("  Aggregate PDR:     {}%".format(stat("aggregate_pdr")))
        print("  Full PDR:          {}%".format(stat("full_pdr")))
        print("  Unreachable nodes: {}".format(stat("unreachable_nodes")))

        # Print test pair results
        for src_idx, dst_idx, desc in TEST_PAIRS[:5]:
            key = "{}to{}".format(src_idx + 1, dst_idx + 1)
            print("  PDR {}:     {}%".format(desc, stat("{}_pdr".format(key))))
            print("  Lat {}:     {}ms".format(desc, stat("{}_lat_avg".format(key))))

        print("  Root CPU:          {}%".format(stat("root_cpu")))
        print("  Root mem:          {}MB".format(stat("root_mem_mb")))
        print("  Avg CPU:           {}%".format(stat("avg_cpu")))
        print("  Avg mem:           {}MB".format(stat("avg_mem_mb")))
        print("  Root DIO (15s):    {}".format(stat("root_dio_15s")))
        print("  Routes SRH:        {}".format(stat("routes_srh")))
        print("  Routes via:        {}".format(stat("routes_via")))


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="HyMRPL Scalability Test — 50 nodes")
    parser.add_argument('--runs', type=int, default=3,
                        help="Number of runs per mode (default: 3)")
    parser.add_argument('--modes', nargs='+', default=['storing', 'nonstoring', 'hybrid'],
                        choices=['storing', 'nonstoring', 'hybrid'],
                        help="Modes to test (default: all three)")
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    all_results = []

    # Print topology info
    info("*** Topology: {} nodes, {} branches, max depth {}\n".format(
        NUM_NODES, NUM_BRANCHES, MAX_DEPTH))
    info("*** Hybrid classes: {} S / {} N\n".format(s_count, n_count))
    info("*** Links: {}\n".format(len(LINKS)))
    info("*** Test pairs: {}\n".format(len(TEST_PAIRS)))

    # Create topology ONCE
    info("*** Creating topology...\n")
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

    # Save final results
    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "scalability_{}_{}.csv".format(NUM_NODES, ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults saved: {}".format(csv_path))

    info("\n*** Stopping network...\n")
    try:
        net.stop()
    except Exception:
        pass


if __name__ == '__main__':
    setLogLevel('info')
    main()
