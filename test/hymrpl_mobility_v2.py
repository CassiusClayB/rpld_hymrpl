#!/usr/bin/env python3
"""
HyMRPL — Experimento de mobilidade v2

Usa ip link down/up e tc netem para simular mobilidade real:
  - Degradação progressiva de enlace (packet loss via tc netem)
  - Handover via desativação/ativação de interfaces
  - Saída e reentrada na rede
  - Perda aleatória de pacotes para cenário realista

Fases:
  A: Baseline — sensor5 conectado a sensor4, sem perda
  B: Degradação — 10% packet loss no enlace sensor4-sensor5
  C: Handover — link sensor4-sensor5 down, sensor5 reconecta via sensor3
  D: Estabilização — sensor5 conectado a sensor3, sem perda
  E: Saída — link down total, sensor5 fora da rede
  F: Reentrada — link up, sensor5 volta à rede

Topologia criada UMA vez, reutilizada pra todos os modos.
Uso: sudo python3 hymrpl_mobility_v2.py [--runs 3] [--modes storing nonstoring hybrid]
"""

import time, re, csv, os, sys, statistics
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"

HYBRID_CLASSES = {
    'sensor1': 'S', 'sensor2': 'N', 'sensor3': 'S',
    'sensor4': 'N', 'sensor5': 'N',
}


# ============================================================
# Utility functions (same as hymrpl_run_mode.py)
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
        # Remove any tc netem rules
        s.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    # Ensure all interfaces are up
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


# ============================================================
# Mobility experiment
# ============================================================

def add_packet_loss(sensor, loss_pct):
    """Add packet loss via tc netem on the sensor's interface."""
    iface = get_iface_name(sensor)
    sensor.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    if loss_pct > 0:
        sensor.cmd('tc qdisc add dev {} root netem loss {}%'.format(iface, loss_pct))
        info("    tc netem: {}% loss on {}/{}\n".format(loss_pct, sensor.name, iface))


def link_down(sensor):
    """Bring down the sensor's interface to simulate leaving the network."""
    iface = get_iface_name(sensor)
    sensor.cmd('ip link set {} down'.format(iface))
    info("    link DOWN: {}/{}\n".format(sensor.name, iface))


def link_up(sensor):
    """Bring up the sensor's interface to simulate rejoining the network."""
    iface = get_iface_name(sensor)
    sensor.cmd('ip link set {} up'.format(iface))
    info("    link UP: {}/{}\n".format(sensor.name, iface))


def run_mobility(sensors, mode, run_id):
    """
    Mobility experiment with real link manipulation.

    Phases:
      A: Baseline (no loss, sensor5 via sensor4)
      B: Degradation (10% loss on sensor5 interface)
      C: Handover (sensor5 link down, rpld restart to reconnect via sensor3)
      D: Stable (sensor5 connected, no loss)
      E: Leave (sensor5 link down — out of network)
      F: Rejoin (sensor5 link up — back in network)
    """
    info("\n=== MOBILITY {} | Run {} ===\n".format(mode.upper(), run_id))
    results = {"mode": mode, "run": run_id}

    # Clean start
    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    # Start rpld and wait for convergence
    start_rpld(sensors, mode)
    info("  Waiting for initial convergence...\n")
    target_addr = wait_for_global_addr(sensors[4])
    if not target_addr:
        info("  FAIL: sensor5 never got address\n")
        results["A_pdr"] = -1
        return results

    conv = wait_for_convergence(sensors[0], target_addr)
    if conv < 0:
        info("  FAIL: initial convergence failed\n")
        results["A_pdr"] = -1
        return results

    results["initial_conv_s"] = round(conv, 2)
    info("  Initial convergence: {}s\n".format(results["initial_conv_s"]))
    time.sleep(5)

    # --- Phase A: Baseline ---
    info("  --- Phase A: Baseline (no loss) ---\n")
    m = measure_pdr_latency(sensors[0], target_addr)
    results["A_pdr"] = round(m["pdr"], 1)
    results["A_lat_avg"] = round(m["lat_avg"], 3)
    results["A_lat_p95"] = round(m["lat_p95"], 3)
    info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # Also measure local traffic sensor4->sensor5
    m_local = measure_pdr_latency(sensors[3], target_addr, count=20)
    results["A_local_lat"] = round(m_local["lat_avg"], 3)

    # --- Phase B: Degradation (10% packet loss) ---
    info("  --- Phase B: 10% packet loss ---\n")
    add_packet_loss(sensors[4], 10)
    time.sleep(5)
    m = measure_pdr_latency(sensors[0], target_addr)
    results["B_pdr"] = round(m["pdr"], 1)
    results["B_lat_avg"] = round(m["lat_avg"], 3)
    results["B_lat_p95"] = round(m["lat_p95"], 3)
    info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # Increase to 30% loss
    info("  --- Phase B2: 30% packet loss ---\n")
    add_packet_loss(sensors[4], 30)
    time.sleep(5)
    m = measure_pdr_latency(sensors[0], target_addr)
    results["B2_pdr"] = round(m["pdr"], 1)
    results["B2_lat_avg"] = round(m["lat_avg"], 3)
    results["B2_lat_p95"] = round(m["lat_p95"], 3)
    info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # Remove loss before handover
    add_packet_loss(sensors[4], 0)

    # --- Phase C: Handover (link down sensor5, restart rpld) ---
    info("  --- Phase C: Handover (link down + up) ---\n")
    link_down(sensors[4])  # sensor5 loses parent
    time.sleep(3)

    # Measure during disconnection
    m_disc = measure_pdr_latency(sensors[0], target_addr, count=10)
    results["C_disc_pdr"] = round(m_disc["pdr"], 1)
    info("    During disconnect: PDR={:.1f}%\n".format(m_disc["pdr"]))

    # Bring link back up
    link_up(sensors[4])
    time.sleep(2)

    # Measure reconvergence time
    reconv_start = time.time()
    reconv = wait_for_convergence(sensors[0], target_addr, max_attempts=60)
    results["C_reconv_s"] = round(reconv, 2) if reconv > 0 else -1
    info("    Reconvergence: {}s\n".format(results["C_reconv_s"]))

    time.sleep(5)
    m = measure_pdr_latency(sensors[0], target_addr)
    results["C_pdr"] = round(m["pdr"], 1)
    results["C_lat_avg"] = round(m["lat_avg"], 3)
    info("    After handover: PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # --- Phase D: Stable ---
    info("  --- Phase D: Stable (no loss) ---\n")
    time.sleep(10)
    m = measure_pdr_latency(sensors[0], target_addr)
    results["D_pdr"] = round(m["pdr"], 1)
    results["D_lat_avg"] = round(m["lat_avg"], 3)
    results["D_lat_p95"] = round(m["lat_p95"], 3)
    info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # --- Phase E: Leave network ---
    info("  --- Phase E: sensor5 leaves network ---\n")
    link_down(sensors[4])
    time.sleep(5)
    m = measure_pdr_latency(sensors[0], target_addr, count=10)
    results["E_pdr"] = round(m["pdr"], 1)
    info("    PDR after leave: {:.1f}%\n".format(m["pdr"]))

    # --- Phase F: Rejoin network ---
    info("  --- Phase F: sensor5 rejoins network ---\n")
    link_up(sensors[4])
    time.sleep(2)

    # Restart rpld on sensor5 to force rejoin
    sensors[4].cmd('killall -9 rpld 2>/dev/null')
    time.sleep(1)
    cls = HYBRID_CLASSES.get(sensors[4].name, 'S') if mode == 'hybrid' else 'S'
    conf = gen_config(sensors[4], mode, cls)
    sensors[4].cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
        conf, sensors[4].name))

    rejoin_start = time.time()
    # sensor5 may get a new address
    new_addr = wait_for_global_addr(sensors[4], timeout=60)
    if new_addr:
        target_addr = new_addr
    reconv = wait_for_convergence(sensors[0], target_addr, max_attempts=60)
    results["F_reconv_s"] = round(reconv, 2) if reconv > 0 else -1
    info("    Rejoin reconvergence: {}s\n".format(results["F_reconv_s"]))

    time.sleep(5)
    m = measure_pdr_latency(sensors[0], target_addr)
    results["F_pdr"] = round(m["pdr"], 1)
    results["F_lat_avg"] = round(m["lat_avg"], 3)
    info("    After rejoin: PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # Cleanup: ensure everything is back to normal
    clean_state(sensors)
    return results


# ============================================================
# CSV + Summary + Main
# ============================================================

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
    print("MOBILITY v2 RESULTS")
    print("=" * 60)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        print("\n--- {} ({} runs) ---".format(mode.upper(), len(rows)))

        def avg(key):
            vals = [r.get(key, -1) for r in rows if r.get(key, -1) not in (-1, None)]
            if not vals:
                return "N/A"
            return "{:.2f}".format(statistics.mean(vals))

        print("  Initial conv:  {}s".format(avg("initial_conv_s")))
        print("  Phase A: PDR={}% lat={}ms local={}ms".format(
            avg("A_pdr"), avg("A_lat_avg"), avg("A_local_lat")))
        print("  Phase B (10%): PDR={}% lat={}ms".format(
            avg("B_pdr"), avg("B_lat_avg")))
        print("  Phase B2(30%): PDR={}% lat={}ms".format(
            avg("B2_pdr"), avg("B2_lat_avg")))
        print("  Phase C disc:  PDR={}%".format(avg("C_disc_pdr")))
        print("  Phase C after: PDR={}% lat={}ms reconv={}s".format(
            avg("C_pdr"), avg("C_lat_avg"), avg("C_reconv_s")))
        print("  Phase D:       PDR={}% lat={}ms".format(
            avg("D_pdr"), avg("D_lat_avg")))
        print("  Phase E leave: PDR={}%".format(avg("E_pdr")))
        print("  Phase F join:  PDR={}% lat={}ms reconv={}s".format(
            avg("F_pdr"), avg("F_lat_avg"), avg("F_reconv_s")))


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
        info("\n### MOBILITY: {} ###\n".format(mode.upper()))
        for run_id in range(1, args.runs + 1):
            try:
                r = run_mobility(sensors, mode, run_id)
                all_results.append(r)
            except Exception as e:
                import traceback
                info("ERROR: {}\n".format(e))
                info(traceback.format_exc() + "\n")
                all_results.append({"mode": mode, "run": run_id, "A_pdr": -1})

    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "mobility_v2_{}.csv".format(ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults: {}".format(csv_path))

    info("\n*** Stopping network...\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
