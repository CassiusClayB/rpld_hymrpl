#!/usr/bin/env python3
"""
HyMRPL Benchmark — Storing vs Non-Storing vs Hybrid (MOP=6)

Roda cada modo em sequência, mantendo a topologia viva durante as N runs
de cada modo. Só recria a topologia ao trocar de modo.

Uso: sudo python3 hymrpl_benchmark.py [--runs 5] [--modes storing nonstoring hybrid]
"""

import time, re, csv, os, sys, statistics, subprocess
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
PING_COUNT = 50

# Timeout mais generoso pra convergência (especialmente hybrid)
CONVERGENCE_ADDR_TIMEOUT = 90   # segundos esperando endereço global
CONVERGENCE_PING_TIMEOUT = 180  # tentativas de ping (x 0.5s = 90s)

TEST_PAIRS = [
    (1, 2, "1-hop"), (1, 3, "1-hop"),
    (1, 4, "2-hop"), (1, 5, "3-hop"),
    (5, 1, "3-hop-up"),
]

HYBRID_CLASSES = {
    'sensor1': 'S', 'sensor2': 'N', 'sensor3': 'S',
    'sensor4': 'N', 'sensor5': 'N',
}


def gen_config(node, mode, node_class="S"):
    iface = '{}-pan0'.format(node.name)
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
    conf_name = 'lowpan-{}.conf'.format(node.name)
    node.pexec("echo '{}' > {}".format(cmd, conf_name), shell=True)
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
    """Start rpld on each sensor with staggered delays for proper DODAG formation."""
    # Start root first
    root = sensors[0]
    cls = HYBRID_CLASSES.get(root.name, 'S') if mode == 'hybrid' else 'S'
    conf = gen_config(root, mode, cls)
    root.cmd('nohup rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, root.name))
    time.sleep(3)  # root precisa estar estável antes dos filhos

    # Start 1-hop nodes (sensor2, sensor3)
    for s in [sensors[1], sensors[2]]:
        cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
        conf = gen_config(s, mode, cls)
        s.cmd('nohup rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))
    time.sleep(2)  # espera 1-hop nodes processarem DIO

    # Start 2-hop node (sensor4)
    s = sensors[3]
    cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
    conf = gen_config(s, mode, cls)
    s.cmd('nohup rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))
    time.sleep(2)

    # Start 3-hop node (sensor5)
    s = sensors[4]
    cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
    conf = gen_config(s, mode, cls)
    s.cmd('nohup rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))


def stop_rpld(sensors):
    for s in sensors:
        s.cmd('killall -9 rpld 2>/dev/null')
    time.sleep(2)


def flush_routes(sensors):
    """Remove all rpld-installed routes so next run starts clean."""
    for s in sensors:
        # Flush all IPv6 routes except link-local and kernel routes
        s.cmd('ip -6 route flush proto static 2>/dev/null')
        s.cmd('ip -6 route flush proto boot 2>/dev/null')
        s.cmd('ip -6 route flush proto 99 2>/dev/null')  # rpld pode usar proto customizado


def get_global_addr(sensor):
    output = sensor.cmd('ip -6 addr show {}-pan0 | grep "scope global"'.format(sensor.name))
    match = re.search(r'inet6\s+(\S+)/64', output)
    return match.group(1) if match else None


def wait_for_global_addr(sensor, timeout=CONVERGENCE_ADDR_TIMEOUT):
    """Wait for a sensor to get a global IPv6 address, with timeout."""
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
    """
    Wait until src can ping dst_addr. Returns elapsed time or -1 on failure.
    Uses increasing intervals to avoid flooding the network during formation.
    """
    start = time.time()
    for i in range(max_attempts):
        result = src.cmd('ping6 -c 1 -W 2 {}'.format(dst_addr))
        if '1 received' in result:
            return time.time() - start
        # Backoff: first 20 attempts every 0.5s, then every 1s
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
    """Dump rpld logs for debugging when convergence fails."""
    info("  --- rpld logs for {} run {} ---\n".format(mode, run_id))
    for s in sensors:
        output = s.cmd('tail -20 /tmp/rpld_{}.log 2>/dev/null'.format(s.name))
        if output.strip():
            info("  [{}] {}\n".format(s.name, output.strip()[:200]))
    # Also show route tables
    for s in sensors:
        routes = s.cmd('ip -6 route show dev {}-pan0 2>/dev/null'.format(s.name))
        if routes.strip():
            info("  [{}] routes: {}\n".format(s.name, routes.strip()[:200]))


def run_single(sensors, mode, run_id, runs_total):
    """Run a single experiment (topology already up)."""
    info("=== {} | Run {}/{} ===\n".format(mode.upper(), run_id, runs_total))
    results = {"mode": mode, "run": run_id}

    # Clean previous state
    stop_rpld(sensors)
    flush_routes(sensors)
    # Flush old SLAAC/rpld addresses so rpld generates fresh ones
    for s in sensors:
        s.cmd('ip -6 addr flush dev {}-pan0 scope global 2>/dev/null'.format(s.name))
    time.sleep(3)

    # Start rpld with staggered delays and measure convergence
    start_time = time.time()
    start_rpld(sensors, mode)

    # Wait for sensor5 (farthest node) to get a global address
    info("  Waiting for sensor5 global address...\n")
    target_addr = wait_for_global_addr(sensors[4])

    if not target_addr:
        info("  FAIL: sensor5 never got a global address!\n")
        dump_rpld_logs(sensors, mode, run_id)
        results["convergence_s"] = -1
        return results

    info("  sensor5 got address: {}\n".format(target_addr))

    # Also verify intermediate nodes have addresses
    for i, s in enumerate(sensors[:-1]):
        addr = get_global_addr(s)
        if not addr:
            info("  WARNING: {} has no global address yet\n".format(s.name))

    # Now measure convergence: time until root can ping sensor5
    info("  Waiting for end-to-end connectivity...\n")
    conv_elapsed = wait_for_convergence(sensors[0], target_addr)

    if conv_elapsed < 0:
        info("  FAIL: DODAG did not converge (root cannot reach sensor5)\n")
        dump_rpld_logs(sensors, mode, run_id)
        results["convergence_s"] = -1
        return results

    conv_time = time.time() - start_time
    results["convergence_s"] = round(conv_time, 2)
    info("  Convergence: {}s\n".format(results["convergence_s"]))

    # Let routes stabilize before measuring
    info("  Stabilizing (10s)...\n")
    time.sleep(10)

    # Get all addresses
    addrs = {}
    for s in sensors:
        addrs[s.name] = get_global_addr(s)
        if not addrs[s.name]:
            info("  WARNING: {} still has no global address\n".format(s.name))

    # PDR/Latency
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

    # Routes
    routes = count_routes(sensors[0])
    results.update(routes)

    # CPU/Mem
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


def gen_summary(all_results):
    modes = {}
    for r in all_results:
        modes.setdefault(r["mode"], []).append(r)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        # Count successful runs (convergence != -1)
        ok_rows = [r for r in rows if r.get("convergence_s", -1) != -1]
        fail_count = len(rows) - len(ok_rows)
        print("\n--- {} ({} runs, {} converged, {} failed) ---".format(
            mode.upper(), len(rows), len(ok_rows), fail_count))

        def stat(key):
            vals = [r.get(key, 0) for r in ok_rows if r.get(key, 0) not in (0, -1, None)]
            if not vals:
                return "N/A"
            avg = statistics.mean(vals)
            std = statistics.stdev(vals) if len(vals) > 1 else 0
            return "{:.2f} +/- {:.2f}".format(avg, std)

        print("  Convergence:     {}s".format(stat("convergence_s")))
        print("  PDR 1-hop:       {}%".format(stat("1to2_pdr")))
        print("  PDR 2-hop:       {}%".format(stat("1to4_pdr")))
        print("  PDR 3-hop:       {}%".format(stat("1to5_pdr")))
        print("  Latency 1-hop:   {}ms".format(stat("1to2_lat_avg")))
        print("  Latency 2-hop:   {}ms".format(stat("1to4_lat_avg")))
        print("  Latency 3-hop:   {}ms".format(stat("1to5_lat_avg")))
        print("  Lat p95 3-hop:   {}ms".format(stat("1to5_lat_p95")))

        srh = [r.get("routes_srh", 0) for r in ok_rows]
        via = [r.get("routes_via", 0) for r in ok_rows]
        if srh:
            print("  Routes SRH:      {:.0f}".format(statistics.mean(srh)))
        if via:
            print("  Routes via:      {:.0f}".format(statistics.mean(via)))


def gen_latex(all_results, path):
    modes = {}
    for r in all_results:
        modes.setdefault(r["mode"], []).append(r)

    def a(rows, k):
        v = [r.get(k, 0) for r in rows if r.get(k, 0) not in (-1, None)]
        return statistics.mean(v) if v else 0

    def s(rows, k):
        v = [r.get(k, 0) for r in rows if r.get(k, 0) not in (-1, None)]
        return statistics.stdev(v) if len(v) > 1 else 0

    def cell(rows, k):
        av, sd = a(rows, k), s(rows, k)
        if sd > 0.01:
            return "${:.2f} \\pm {:.2f}$".format(av, sd)
        return "${:.2f}$".format(av)

    n = max(len(v) for v in modes.values()) if modes else 0
    tex = "\\begin{table}[H]\n\\centering\\footnotesize\n"
    tex += "\\caption{Comparativo Storing, Non-Storing e HyMRPL -- " + str(n) + " execuções}\n"
    tex += "\\label{tab:comparativo_hymrpl}\n"
    tex += "\\begin{tabular}{lccc}\n\\hline\n"
    tex += "\\textbf{Métrica} & \\textbf{Storing} & \\textbf{Non-Storing} & \\textbf{HyMRPL} \\\\\n\\hline\n"

    metrics = [
        ("Convergência (s)", "convergence_s"),
        ("PDR 1-hop (\\%)", "1to2_pdr"),
        ("PDR 2-hop (\\%)", "1to4_pdr"),
        ("PDR 3-hop (\\%)", "1to5_pdr"),
        ("Latência 1-hop (ms)", "1to2_lat_avg"),
        ("Latência 2-hop (ms)", "1to4_lat_avg"),
        ("Latência 3-hop (ms)", "1to5_lat_avg"),
        ("Lat. p95 3-hop (ms)", "1to5_lat_p95"),
        ("Rotas SRH", "routes_srh"),
        ("Rotas via", "routes_via"),
    ]
    for label, key in metrics:
        row = label
        for m in ["storing", "nonstoring", "hybrid"]:
            rows = modes.get(m, [])
            row += " & " + (cell(rows, key) if rows else "--")
        tex += row + " \\\\\n"

    tex += "\\hline\n\\end{tabular}\n\\end{table}\n"
    with open(path, 'w') as f:
        f.write(tex)


def gen_pgfplots(all_results, outdir):
    modes = {}
    for r in all_results:
        modes.setdefault(r["mode"], []).append(r)

    def a(rows, k):
        v = [r.get(k, 0) for r in rows if r.get(k, 0) not in (-1, None)]
        return statistics.mean(v) if v else 0

    def s(rows, k):
        v = [r.get(k, 0) for r in rows if r.get(k, 0) not in (-1, None)]
        return statistics.stdev(v) if len(v) > 1 else 0

    with open(os.path.join(outdir, "pdr_by_hops.csv"), 'w') as f:
        f.write("hops,storing,nonstoring,hybrid\n")
        for h, k in [(1, "1to2_pdr"), (2, "1to4_pdr"), (3, "1to5_pdr")]:
            f.write("{},{:.1f},{:.1f},{:.1f}\n".format(
                h, a(modes.get("storing",[]),k), a(modes.get("nonstoring",[]),k), a(modes.get("hybrid",[]),k)))

    with open(os.path.join(outdir, "latency_by_hops.csv"), 'w') as f:
        f.write("hops,storing,nonstoring,hybrid\n")
        for h, k in [(1,"1to2_lat_avg"),(2,"1to4_lat_avg"),(3,"1to5_lat_avg")]:
            f.write("{},{:.3f},{:.3f},{:.3f}\n".format(
                h, a(modes.get("storing",[]),k), a(modes.get("nonstoring",[]),k), a(modes.get("hybrid",[]),k)))

    with open(os.path.join(outdir, "convergence.csv"), 'w') as f:
        f.write("mode,avg,std\n")
        for m in ["storing","nonstoring","hybrid"]:
            rows = modes.get(m, [])
            f.write("{},{:.2f},{:.2f}\n".format(m, a(rows,"convergence_s"), s(rows,"convergence_s")))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--modes', nargs='+', default=['storing','nonstoring','hybrid'],
                        choices=['storing','nonstoring','hybrid'])
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    all_results = []

    for mode in args.modes:
        info("\n### TESTING MODE: {} ###\n".format(mode.upper()))

        # Create topology once per mode
        net, sensors = create_topology()

        for run_id in range(1, args.runs + 1):
            try:
                r = run_single(sensors, mode, run_id, args.runs)
                all_results.append(r)
                save_csv(all_results, os.path.join(RESULTS_DIR, "benchmark_{}.csv".format(ts)))
            except Exception as e:
                import traceback
                info("ERROR run {}: {}\n".format(run_id, e))
                info(traceback.format_exc() + "\n")
                all_results.append({"mode": mode, "run": run_id, "convergence_s": -1, "error": str(e)})

        # Stop topology for this mode
        stop_rpld(sensors)
        info("*** Stopping topology for mode {}\n".format(mode))
        try:
            net.stop()
        except Exception as e:
            info("WARNING: net.stop() raised: {}\n".format(e))
        time.sleep(3)

        # Cleanup manual — garante que tudo morreu
        subprocess.run('killall -9 rpld 2>/dev/null', shell=True)
        subprocess.run('mn -c 2>/dev/null', shell=True, capture_output=True)
        time.sleep(3)
        subprocess.run('modprobe -r mac802154_hwsim 2>/dev/null', shell=True)
        time.sleep(5)

    # Generate outputs
    csv_path = os.path.join(RESULTS_DIR, "benchmark_{}.csv".format(ts))
    save_csv(all_results, csv_path)
    gen_latex(all_results, os.path.join(RESULTS_DIR, "tabela_comparativa.tex"))
    gen_pgfplots(all_results, RESULTS_DIR)
    gen_summary(all_results)

    print("\n\nFiles in {}:".format(RESULTS_DIR))
    for f in os.listdir(RESULTS_DIR):
        print("  " + f)


if __name__ == '__main__':
    setLogLevel('info')
    main()
