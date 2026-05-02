#!/usr/bin/env python3
"""
HyMRPL — Benchmark with persistent topology.
Creates the topology ONCE, runs all modes switching only the rpld config.
Usage: sudo python3 hymrpl_run_mode.py [--runs 3] [--modes storing nonstoring hybrid]
"""

import time, re, csv, os, sys, statistics
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi
from mn_wifi.cli import CLI

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
PING_COUNT = 50
CONVERGENCE_ADDR_TIMEOUT = 90
CONVERGENCE_PING_TIMEOUT = 180

TEST_PAIRS = [
    (1, 2, "1-hop"), (1, 3, "1-hop"),
    (1, 4, "2-hop"), (1, 5, "3-hop"),
    (5, 1, "3-hop-up"),
]

HYBRID_CLASSES = {
    'sensor1': 'S', 'sensor2': 'N', 'sensor3': 'S',
    'sensor4': 'N', 'sensor5': 'N',
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


def start_rpld(sensors, mode):
    root = sensors[0]
    cls = HYBRID_CLASSES.get(root.name, 'S') if mode == 'hybrid' else 'S'
    conf = gen_config(root, mode, cls)
    root.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, root.name))
    time.sleep(3)
    for s in [sensors[1], sensors[2]]:
        cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
        conf = gen_config(s, mode, cls)
        s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))
    time.sleep(2)
    for s in [sensors[3], sensors[4]]:
        cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
        conf = gen_config(s, mode, cls)
        s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))
        time.sleep(2)


def stop_rpld(sensors):
    for s in sensors:
        s.cmd('killall -9 rpld 2>/dev/null')
    time.sleep(2)


def clean_state(sensors):
    """Cleans routes and global addresses without destroying the topology."""
    for s in sensors:
        iface = get_iface_name(s)
        s.cmd('ip -6 route flush proto static 2>/dev/null')
        s.cmd('ip -6 route flush proto boot 2>/dev/null')
        s.cmd('ip -6 route flush proto 99 2>/dev/null')
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


def percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


def wait_for_convergence(src, dst_addr, max_attempts=CONVERGENCE_PING_TIMEOUT):
    start = time.time()
    for i in range(max_attempts):
        result = src.cmd('ping6 -c 1 -W 2 {}'.format(dst_addr))
        if '1 received' in result:
            return time.time() - start
        time.sleep(0.5 if i < 20 else 1.0)
    return -1


def measure_pdr_latency(src, dst_addr):
    result = src.cmd('ping6 -c {} -i 0.2 -W 2 {}'.format(PING_COUNT, dst_addr))
    match = re.search(r'(\d+) packets transmitted, (\d+) received', result)
    if not match:
        return {"pdr": 0, "lat_min": 0, "lat_avg": 0, "lat_max": 0, "lat_p50": 0, "lat_p95": 0}
    tx, rx = int(match.group(1)), int(match.group(2))
    pdr = (rx / tx) * 100.0 if tx > 0 else 0
    lat_match = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', result)
    lat_min = float(lat_match.group(1)) if lat_match else 0
    lat_avg = float(lat_match.group(2)) if lat_match else 0
    lat_max = float(lat_match.group(3)) if lat_match else 0
    lat_values = [float(m.group(1)) for m in re.finditer(r'time=([\d.]+)', result)]
    return {"pdr": pdr, "lat_min": lat_min, "lat_avg": lat_avg, "lat_max": lat_max,
            "lat_p50": percentile(lat_values, 50), "lat_p95": percentile(lat_values, 95)}


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


def dump_rpld_logs(sensors, mode, run_id):
    info("  --- rpld logs for {} run {} ---\n".format(mode, run_id))
    for s in sensors:
        output = s.cmd('tail -20 /tmp/rpld_{}.log 2>/dev/null'.format(s.name))
        if output.strip():
            info("  [{}] {}\n".format(s.name, output.strip()[:300]))


def run_single(sensors, mode, run_id, runs_total):
    info("=== {} | Run {}/{} ===\n".format(mode.upper(), run_id, runs_total))
    results = {"mode": mode, "run": run_id}

    # Stop previous rpld and clean state (without destroying topology)
    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    # Start rpld with new mode
    start_time = time.time()
    start_rpld(sensors, mode)

    info("  Waiting for sensor5 global address...\n")
    target_addr = wait_for_global_addr(sensors[4])
    if not target_addr:
        info("  FAIL: sensor5 never got a global address!\n")
        dump_rpld_logs(sensors, mode, run_id)
        results["convergence_s"] = -1
        return results

    info("  sensor5 got address: {}\n".format(target_addr))

    info("  Waiting for end-to-end connectivity...\n")
    conv_elapsed = wait_for_convergence(sensors[0], target_addr)
    if conv_elapsed < 0:
        info("  FAIL: DODAG did not converge\n")
        dump_rpld_logs(sensors, mode, run_id)
        results["convergence_s"] = -1
        return results

    conv_time = time.time() - start_time
    results["convergence_s"] = round(conv_time, 2)
    info("  Convergence: {}s\n".format(results["convergence_s"]))

    info("  Stabilizing (10s)...\n")
    time.sleep(10)

    addrs = {}
    for s in sensors:
        addrs[s.name] = get_global_addr(s)

    for src_idx, dst_idx, desc in TEST_PAIRS:
        src = sensors[src_idx - 1]
        dst_addr = addrs.get(sensors[dst_idx - 1].name)
        if not dst_addr:
            info("  SKIP {} -> sensor{}: no address\n".format(src.name, dst_idx))
            continue
        info("  {} -> sensor{} ({})...\n".format(src.name, dst_idx, desc))
        m = measure_pdr_latency(src, dst_addr)
        key = "{}to{}".format(src_idx, dst_idx)
        for k, v in m.items():
            results["{}_{}".format(key, k)] = round(v, 3)

    routes = count_routes(sensors[0])
    results.update(routes)

    for s in sensors:
        cpu, mem = measure_cpu_mem(s)
        results["{}_cpu".format(s.name)] = cpu
        results["{}_mem_mb".format(s.name)] = round(mem, 2)

    return results


def save_csv(results, path):
    if not results:
        return
    keys = sorted(set().union(*(r.keys() for r in results)))
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(results)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--modes', nargs='+', default=['storing', 'nonstoring', 'hybrid'],
                        choices=['storing', 'nonstoring', 'hybrid'])
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    all_results = []

    # Create topology ONCE
    info("*** Creating topology (persistent for all modes)\n")
    net, sensors = create_topology()

    # Run all modes on the same topology
    for mode in args.modes:
        info("\n### TESTING MODE: {} ###\n".format(mode.upper()))

        for run_id in range(1, args.runs + 1):
            try:
                r = run_single(sensors, mode, run_id, args.runs)
                all_results.append(r)
                save_csv(all_results, os.path.join(RESULTS_DIR, "benchmark_{}.csv".format(ts)))
            except Exception as e:
                import traceback
                info("ERROR run {}: {}\n".format(run_id, e))
                info(traceback.format_exc() + "\n")
                all_results.append({"mode": mode, "run": run_id, "convergence_s": -1})

    # Final cleanup
    stop_rpld(sensors)

    # Generate outputs before net.stop()
    csv_path = os.path.join(RESULTS_DIR, "benchmark_{}.csv".format(ts))
    save_csv(all_results, csv_path)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    modes = {}
    for r in all_results:
        modes.setdefault(r["mode"], []).append(r)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        ok = [r for r in rows if r.get("convergence_s", -1) != -1]
        print("\n--- {} ({} runs, {} ok) ---".format(mode.upper(), len(rows), len(ok)))

        def stat(key):
            vals = [r.get(key, 0) for r in ok if r.get(key, 0) not in (0, -1, None)]
            if not vals:
                return "N/A"
            avg = statistics.mean(vals)
            std = statistics.stdev(vals) if len(vals) > 1 else 0
            return "{:.2f} +/- {:.2f}".format(avg, std)

        print("  Convergence:   {}s".format(stat("convergence_s")))
        print("  PDR 1-hop:     {}%".format(stat("1to2_pdr")))
        print("  PDR 2-hop:     {}%".format(stat("1to4_pdr")))
        print("  PDR 3-hop:     {}%".format(stat("1to5_pdr")))
        print("  Lat avg 3-hop: {}ms".format(stat("1to5_lat_avg")))
        print("  Lat p95 3-hop: {}ms".format(stat("1to5_lat_p95")))

    print("\nResults: {}".format(csv_path))
    print("=" * 60)

    # net.stop() will kill the process, but results have already been saved
    info("\n*** Results saved. Stopping network (may kill process)...\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
