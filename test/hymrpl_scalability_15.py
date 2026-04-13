#!/usr/bin/env python3
"""
HyMRPL — Teste de Escalabilidade com 15 nós

Topologia em árvore com 3 branches, profundidade máxima de 4 hops:

    sensor1 (Root, S)
    ├── sensor2 (S) ── sensor5 (N) ── sensor8 (N)  ── sensor11 (N) ── sensor14 (N)
    ├── sensor3 (S) ── sensor6 (N) ── sensor9 (N)  ── sensor12 (N) ── sensor15 (N)
    └── sensor4 (N) ── sensor7 (N) ── sensor10 (S) ── sensor13 (N)

Métricas coletadas:
  - Tempo de convergência (até o nó mais distante responder)
  - PDR e latência para nós a 1, 2, 3, 4 e 5 hops
  - CPU e memória do root
  - Contagem de rotas SRH e hop-by-hop
  - Mensagens DIO capturadas no root (15s)

Uso: sudo python3 hymrpl_scalability_15.py [--runs 3] [--modes storing nonstoring hybrid]
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
NUM_NODES = 15

CONVERGENCE_ADDR_TIMEOUT = 150
CONVERGENCE_PING_TIMEOUT = 240

# Topologia: (parent_idx, child_idx) — indices 0-based
# Branch 1: 0-1-4-7-10-13
# Branch 2: 0-2-5-8-11-14
# Branch 3: 0-3-6-9-12
LINKS = [
    (0, 1), (0, 2), (0, 3),           # 1-hop
    (1, 4), (2, 5), (3, 6),           # 2-hop
    (4, 7), (5, 8), (6, 9),           # 3-hop
    (7, 10), (8, 11), (9, 12),        # 4-hop
    (10, 13), (11, 14),               # 5-hop
]

HYBRID_CLASSES = {}
for i in range(NUM_NODES):
    name = 'sensor{}'.format(i + 1)
    if i == 0:
        HYBRID_CLASSES[name] = 'S'  # root
    elif i in (1, 2, 9):
        HYBRID_CLASSES[name] = 'S'  # intermediários com recursos
    else:
        HYBRID_CLASSES[name] = 'N'  # folhas ou restritos

TEST_PAIRS = [
    (0, 1, "1-hop"),     # root -> sensor2
    (0, 4, "2-hop"),     # root -> sensor5
    (0, 7, "3-hop"),     # root -> sensor8
    (0, 10, "4-hop"),    # root -> sensor11
    (0, 13, "5-hop"),    # root -> sensor14
    (13, 0, "5-hop-up"), # sensor14 -> root
    (0, 14, "5-hop-b2"), # root -> sensor15 (branch 2)
    (0, 12, "4-hop-b3"), # root -> sensor13 (branch 3)
]


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

    info("  Waiting 12s for all interfaces to come up...\n")
    time.sleep(12)

    ready = 0
    for s in sensors:
        iface = get_iface_name(s)
        out = s.cmd('ip link show {} 2>/dev/null'.format(iface))
        if iface in out and 'does not exist' not in out:
            ready += 1
    info("  Interfaces ready: {}/{}\n".format(ready, NUM_NODES))

    return net, sensors


def start_rpld(sensors, mode):
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
            cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
            conf = gen_config(s, mode, cls)
            s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
                conf, s.name))
        if d == 0:
            time.sleep(5)
        else:
            time.sleep(3)


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
    time.sleep(3)

    start_time = time.time()
    start_rpld(sensors, mode)

    # Espera o nó mais distante (sensor14, idx=13) obter endereço
    info("  Waiting for farthest node (sensor14) global address...\n")
    farthest_addr = wait_for_global_addr(sensors[13])
    if not farthest_addr:
        info("  FAIL: sensor14 never got a global address\n")
        converged = check_full_convergence(sensors)
        results["convergence_s"] = -1
        results["converged_nodes"] = converged
        return results

    info("  sensor14 got address: {}\n".format(farthest_addr))

    conv = wait_for_convergence(sensors[0], farthest_addr)
    if conv < 0:
        info("  FAIL: root cannot reach sensor14\n")
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

    info("  Stabilizing (15s)...\n")
    time.sleep(15)

    addrs = {}
    for s in sensors:
        addrs[s.name] = get_global_addr(s)

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

    routes = count_routes(sensors[0])
    results.update(routes)

    cpu, mem = measure_cpu_mem(sensors[0])
    results["root_cpu"] = cpu
    results["root_mem_mb"] = round(mem, 2)

    all_cpu, all_mem = [], []
    for s in sensors:
        c, m = measure_cpu_mem(s)
        all_cpu.append(c)
        all_mem.append(m)
    results["avg_cpu"] = round(statistics.mean(all_cpu), 2) if all_cpu else 0
    results["avg_mem_mb"] = round(statistics.mean(all_mem), 2) if all_mem else 0

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
        print("  Lat 1-hop:       {}ms".format(stat("1to2_lat_avg")))
        print("  Lat 2-hop:       {}ms".format(stat("1to5_lat_avg")))
        print("  Lat 3-hop:       {}ms".format(stat("1to8_lat_avg")))
        print("  Lat 4-hop:       {}ms".format(stat("1to11_lat_avg")))
        print("  Lat 5-hop:       {}ms".format(stat("1to14_lat_avg")))
        print("  Lat 5-hop up:    {}ms".format(stat("14to1_lat_avg")))
        print("  Root CPU:        {}%".format(stat("root_cpu")))
        print("  Root mem:        {}MB".format(stat("root_mem_mb")))
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

    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "scalability_{}_{}.csv".format(NUM_NODES, ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults: {}".format(csv_path))

    info("\n*** Results saved. Stopping network...\n")
    try:
        net.stop()
    except Exception:
        pass


if __name__ == '__main__':
    setLogLevel('info')
    main()
