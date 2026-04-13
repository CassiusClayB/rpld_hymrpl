#!/usr/bin/env python3
"""
HyMRPL — Experimento que demonstra a vantagem do modo híbrido.

Topologia com classes mistas que evidenciam a diferença:
    sensor1 (Root, S)
       /        \
  sensor2(N)   sensor3(S)
                  |
               sensor4(S)   ← storing-like, mantém rotas locais
                  |
               sensor5(N)   ← non-storing, nó com restrição/móvel

Cenários:
  1. Estático: compara latência local sensor4->sensor5 nos 3 modos
     - No hybrid, sensor4(S) tem rota local pra sensor5 → latência baixa
     - No nonstoring, tudo via SRH pelo root → latência alta
     - No storing, tudo hop-by-hop → latência baixa

  2. Troca dinâmica: sensor5 começa N, depois troca pra S via FIFO
     - Mostra que o hybrid adapta o encaminhamento em runtime

  3. Degradação seletiva: perda de pacotes só no caminho N (sensor2)
     - Mostra que o caminho S (sensor3->sensor4) não é afetado

Uso: sudo python3 hymrpl_hybrid_advantage.py [--runs 3]
"""

import time, re, csv, os, statistics
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"

# Classes que mostram a vantagem do hybrid
HYBRID_CLASSES = {
    'sensor1': 'S',  # Root
    'sensor2': 'N',  # Non-storing (restrição energética)
    'sensor3': 'S',  # Storing (estável, com recursos)
    'sensor4': 'S',  # Storing (estável, com recursos)
    'sensor5': 'N',  # Non-storing (móvel/restrito)
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
        return {"pdr": 0, "lat_avg": 0, "lat_p50": 0, "lat_p95": 0}
    tx, rx = int(match.group(1)), int(match.group(2))
    pdr = (rx / tx) * 100.0 if tx > 0 else 0
    lat_match = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', result)
    lat_avg = float(lat_match.group(2)) if lat_match else 0
    lat_values = [float(m.group(1)) for m in re.finditer(r'time=([\d.]+)', result)]
    return {"pdr": pdr, "lat_avg": lat_avg,
            "lat_p50": percentile(lat_values, 50),
            "lat_p95": percentile(lat_values, 95)}


def count_routes(sensor):
    output = sensor.cmd('ip -6 route show')
    return {"routes_srh": output.count('encap rpl'),
            "routes_via": output.count('via fe80')}


# ============================================================
# Experiments
# ============================================================

def run_experiment(sensors, mode, run_id):
    """
    Cenário completo:
      1. Baseline: mede todos os pares
      2. Tráfego local sensor4->sensor5 (mostra vantagem do storing local)
      3. Degradação no caminho N (sensor2): 20% loss
      4. Mede impacto no caminho S (sensor3->sensor4->sensor5) — deve ser zero
    """
    info("=== {} | Run {} ===\n".format(mode.upper(), run_id))
    results = {"mode": mode, "run": run_id}

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    start_time = time.time()
    start_rpld(sensors, mode)

    # Wait convergence
    info("  Waiting for convergence...\n")
    addr5 = wait_for_global_addr(sensors[4])
    if not addr5:
        info("  FAIL: sensor5 no address\n")
        results["convergence_s"] = -1
        return results

    conv = wait_for_convergence(sensors[0], addr5)
    if conv < 0:
        info("  FAIL: no convergence\n")
        results["convergence_s"] = -1
        return results

    results["convergence_s"] = round(time.time() - start_time, 2)
    info("  Convergence: {}s\n".format(results["convergence_s"]))
    time.sleep(10)

    # Get all addresses
    addrs = {}
    for s in sensors:
        addrs[s.name] = get_global_addr(s)

    # --- 1. Baseline: all pairs ---
    pairs = [
        (0, 1, "root_to_s2"),   # root -> sensor2 (1-hop, N path)
        (0, 2, "root_to_s3"),   # root -> sensor3 (1-hop, S path)
        (0, 3, "root_to_s4"),   # root -> sensor4 (2-hop, S path)
        (0, 4, "root_to_s5"),   # root -> sensor5 (3-hop, mixed S+N)
        (4, 0, "s5_to_root"),   # sensor5 -> root (upward)
    ]
    for src_i, dst_i, key in pairs:
        dst_addr = addrs.get(sensors[dst_i].name)
        if not dst_addr:
            continue
        info("  {} -> {} ({})...\n".format(sensors[src_i].name, sensors[dst_i].name, key))
        m = measure_pdr_latency(sensors[src_i], dst_addr)
        results["{}_pdr".format(key)] = round(m["pdr"], 1)
        results["{}_lat".format(key)] = round(m["lat_avg"], 3)
        results["{}_p95".format(key)] = round(m["lat_p95"], 3)

    # --- 2. Local traffic sensor4 -> sensor5 (key differentiator) ---
    if addrs.get('sensor5'):
        info("  Local: sensor4 -> sensor5...\n")
        m = measure_pdr_latency(sensors[3], addrs['sensor5'], count=50)
        results["local_s4s5_pdr"] = round(m["pdr"], 1)
        results["local_s4s5_lat"] = round(m["lat_avg"], 3)
        results["local_s4s5_p95"] = round(m["lat_p95"], 3)
        info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # --- 3. Local traffic sensor3 -> sensor4 (pure S path) ---
    if addrs.get('sensor4'):
        info("  Local: sensor3 -> sensor4...\n")
        m = measure_pdr_latency(sensors[2], addrs['sensor4'], count=50)
        results["local_s3s4_pdr"] = round(m["pdr"], 1)
        results["local_s3s4_lat"] = round(m["lat_avg"], 3)
        info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # --- 4. Degradation on N-path (sensor2): 20% loss ---
    info("  Adding 20% loss on sensor2...\n")
    iface2 = get_iface_name(sensors[1])
    sensors[1].cmd('tc qdisc add dev {} root netem loss 20%'.format(iface2))
    time.sleep(5)

    # Measure N-path (root -> sensor2) — should be degraded
    if addrs.get('sensor2'):
        m = measure_pdr_latency(sensors[0], addrs['sensor2'])
        results["degraded_s2_pdr"] = round(m["pdr"], 1)
        results["degraded_s2_lat"] = round(m["lat_avg"], 3)
        info("    root->sensor2 (degraded): PDR={:.1f}%\n".format(m["pdr"]))

    # Measure S-path (root -> sensor4) — should be unaffected
    if addrs.get('sensor4'):
        m = measure_pdr_latency(sensors[0], addrs['sensor4'])
        results["degraded_s4_pdr"] = round(m["pdr"], 1)
        results["degraded_s4_lat"] = round(m["lat_avg"], 3)
        info("    root->sensor4 (S-path): PDR={:.1f}%\n".format(m["pdr"]))

    # Measure mixed path (root -> sensor5) — should be unaffected
    if addrs.get('sensor5'):
        m = measure_pdr_latency(sensors[0], addrs['sensor5'])
        results["degraded_s5_pdr"] = round(m["pdr"], 1)
        results["degraded_s5_lat"] = round(m["lat_avg"], 3)
        info("    root->sensor5 (mixed): PDR={:.1f}%\n".format(m["pdr"]))

    # Cleanup loss
    sensors[1].cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface2))

    # --- 5. Route info ---
    routes_root = count_routes(sensors[0])
    routes_s4 = count_routes(sensors[3])
    results["root_srh"] = routes_root["routes_srh"]
    results["root_via"] = routes_root["routes_via"]
    results["s4_via"] = routes_s4["routes_via"]
    info("  Routes: root SRH={} via={}, s4 via={}\n".format(
        routes_root["routes_srh"], routes_root["routes_via"], routes_s4["routes_via"]))

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

    print("\n" + "=" * 60)
    print("HYBRID ADVANTAGE RESULTS")
    print("=" * 60)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        ok = [r for r in rows if r.get("convergence_s", -1) != -1]
        print("\n--- {} ({} runs) ---".format(mode.upper(), len(ok)))

        def avg(key):
            vals = [r.get(key, -1) for r in ok if r.get(key, -1) not in (-1, None)]
            return "{:.3f}".format(statistics.mean(vals)) if vals else "N/A"

        print("  Convergence:       {}s".format(avg("convergence_s")))
        print("  root->s2 (N-path): PDR={}% lat={}ms".format(avg("root_to_s2_pdr"), avg("root_to_s2_lat")))
        print("  root->s4 (S-path): PDR={}% lat={}ms".format(avg("root_to_s4_pdr"), avg("root_to_s4_lat")))
        print("  root->s5 (mixed):  PDR={}% lat={}ms".format(avg("root_to_s5_pdr"), avg("root_to_s5_lat")))
        print("  LOCAL s4->s5:      PDR={}% lat={}ms".format(avg("local_s4s5_pdr"), avg("local_s4s5_lat")))
        print("  LOCAL s3->s4:      PDR={}% lat={}ms".format(avg("local_s3s4_pdr"), avg("local_s3s4_lat")))
        print("  --- With 20% loss on sensor2 ---")
        print("  root->s2 degraded: PDR={}%".format(avg("degraded_s2_pdr")))
        print("  root->s4 (S-path): PDR={}%".format(avg("degraded_s4_pdr")))
        print("  root->s5 (mixed):  PDR={}%".format(avg("degraded_s5_pdr")))
        print("  Routes: root SRH={} via={}, s4 via={}".format(
            avg("root_srh"), avg("root_via"), avg("s4_via")))


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

    info("*** Creating topology\n")
    net, sensors = create_topology()

    for mode in args.modes:
        info("\n### MODE: {} ###\n".format(mode.upper()))
        for run_id in range(1, args.runs + 1):
            try:
                r = run_experiment(sensors, mode, run_id)
                all_results.append(r)
            except Exception as e:
                import traceback
                info("ERROR: {}\n".format(e))
                info(traceback.format_exc() + "\n")
                all_results.append({"mode": mode, "run": run_id, "convergence_s": -1})

    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "hybrid_advantage_{}.csv".format(ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults: {}".format(csv_path))

    info("\n*** Stopping network...\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
