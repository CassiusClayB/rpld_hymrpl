#!/usr/bin/env python3
"""
HyMRPL — Experimento de troca adaptativa de classe (motor integrado).

Valida que o motor adaptativo integrado no rpld decide corretamente
a classe do nó baseado em 3 critérios:
  1. PDR (medido internamente via DAO-ACK)
  2. Energia residual (lido de /tmp/hymrpl_battery)
  3. Estabilidade do parent (detecção interna de parent change)

O teste NÃO envia comandos FIFO — apenas manipula as condições
e verifica nos logs do rpld se o motor adaptativo tomou a decisão correta.

Cenários:
  Fase A: Estável, bateria 100% → espera S (score ~1.0)
  Fase B: Bateria cai pra 10% → espera N (score ~0.43)
  Fase C: Bateria volta pra 100%, link degradado 30% → espera N ou S limítrofe
  Fase D: Tudo recupera → espera S
  Fase E: Parent change (link down/up) → espera N temporário
  Fase F: Estabiliza → espera S

Topologia:
    sensor1 (Root, S)
       /        \\
  sensor2(N)   sensor3(S)
                  |
               sensor4(S)
                  |
               sensor5(adaptativo)

Uso: sudo python3 hymrpl_adaptive_switch.py [--runs 3]
"""

import time, re, csv, os, statistics
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"

HYBRID_CLASSES = {
    'sensor1': 'S',
    'sensor2': 'N',
    'sensor3': 'S',
    'sensor4': 'S',
    'sensor5': 'S',  # starts as S, adaptive will manage
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
    root = sensors[0]
    conf = gen_config(root, HYBRID_CLASSES.get(root.name, 'S'))
    root.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, root.name))
    time.sleep(3)
    for s in [sensors[1], sensors[2]]:
        conf = gen_config(s, HYBRID_CLASSES.get(s.name, 'S'))
        s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(conf, s.name))
    time.sleep(2)
    for s in [sensors[3], sensors[4]]:
        conf = gen_config(s, HYBRID_CLASSES.get(s.name, 'S'))
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
        return {"pdr": 0, "lat_avg": 0, "lat_p95": 0}
    tx, rx = int(match.group(1)), int(match.group(2))
    pdr = (rx / tx) * 100.0 if tx > 0 else 0
    lat_match = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', result)
    lat_avg = float(lat_match.group(2)) if lat_match else 0
    lat_values = [float(m.group(1)) for m in re.finditer(r'time=([\d.]+)', result)]
    return {"pdr": pdr, "lat_avg": lat_avg, "lat_p95": percentile(lat_values, 95)}


def set_battery(sensor, level):
    """Set simulated battery level for the adaptive engine (per-interface)."""
    iface = get_iface_name(sensor)
    sensor.cmd('echo {} > /tmp/hymrpl_battery_{}'.format(level, iface))


def apply_loss(sensor, loss_pct):
    """Aplica perda de pacotes no link do sensor."""
    iface = get_iface_name(sensor)
    sensor.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    if loss_pct > 0:
        sensor.cmd('tc qdisc add dev {} root netem loss {}%'.format(iface, loss_pct))
        info("    Applied {}% loss on {}\n".format(loss_pct, sensor.name))
    else:
        info("    Removed loss on {}\n".format(sensor.name))


def get_adaptive_class(sensor):
    """Get current class from rpld log (last adaptive decision)."""
    output = sensor.cmd(
        'grep -oE "class=[SN]|switch [SN] -> [SN]" /tmp/rpld_{}.log 2>/dev/null | tail -1'.format(
            sensor.name))
    if 'switch' in output:
        m = re.search(r'-> ([SN])', output)
        return m.group(1) if m else None
    m = re.search(r'class=([SN])', output)
    return m.group(1) if m else None


def get_adaptive_score(sensor):
    """Get last adaptive score from rpld log."""
    output = sensor.cmd(
        'grep -oE "score=[0-9.]+" /tmp/rpld_{}.log 2>/dev/null | tail -1'.format(
            sensor.name))
    m = re.search(r'score=([\d.]+)', output)
    return float(m.group(1)) if m else None


def count_adaptive_switches(sensor):
    """Count total adaptive switches in log."""
    output = sensor.cmd(
        'grep -c "adaptive: switch" /tmp/rpld_{}.log 2>/dev/null'.format(sensor.name))
    try:
        return int(output.strip())
    except ValueError:
        return 0


def count_routes(sensor):
    output = sensor.cmd('ip -6 route show')
    return {"srh": output.count('encap rpl'), "via": output.count('via fe80')}


def run_experiment(sensors, run_id):
    """
    6 fases que exercitam o motor adaptativo integrado no rpld.
    Manipulamos condições externas e verificamos se o rpld decide corretamente.
    """
    info("\n{}\n=== ADAPTIVE SWITCH | Run {} ===\n{}\n".format("=" * 60, run_id, "=" * 60))
    results = {"run": run_id}

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    # Start with battery 100% (adaptive should choose S)
    set_battery(sensors[4], 100)

    start_time = time.time()
    start_rpld(sensors)

    info("  Waiting for convergence...\n")
    addr5 = wait_for_global_addr(sensors[4])
    if not addr5:
        info("  FAIL: sensor5 no address\n")
        results["convergence_s"] = -1
        return results

    root_addr = get_global_addr(sensors[0])
    conv = wait_for_convergence(sensors[0], addr5)
    if conv < 0:
        info("  FAIL: no convergence\n")
        results["convergence_s"] = -1
        return results

    results["convergence_s"] = round(time.time() - start_time, 2)
    info("  Convergence: {}s\n".format(results["convergence_s"]))

    # Wait for adaptive engine to stabilize (5s interval × 3 hysteresis cycles)
    info("  Waiting for adaptive stabilization (25s)...\n")
    time.sleep(25)

    phases = [
        # (name, desc, battery, loss_pct, do_parent_change, wait_s, expected_class)
        ("A", "Estável, bat=100%",       100,  0, False, 20, "S"),
        ("B", "Bateria baixa 10%",        10,  0, False, 25, "N"),
        ("C", "Bat=100%, loss=30%",      100, 30, False, 25, "N"),
        ("D", "Tudo recupera",           100,  0, False, 25, "S"),
        ("E", "Parent change",           100,  0, True,  30, "N"),
        ("F", "Estabiliza",              100,  0, False, 30, "S"),
    ]

    for phase_name, desc, battery, loss_pct, do_parent_change, wait_s, expected in phases:
        info("\n  --- PHASE {}: {} (expected={}) ---\n".format(phase_name, desc, expected))

        # Apply conditions
        set_battery(sensors[4], battery)
        info("    Battery set to {}%\n".format(battery))

        if do_parent_change:
            iface4 = get_iface_name(sensors[3])
            info("    Simulating parent loss (link down 3s)...\n")
            sensors[3].cmd('ip link set {} down'.format(iface4))
            time.sleep(3)
            sensors[3].cmd('ip link set {} up'.format(iface4))
            time.sleep(5)
            # Re-check connectivity
            reconn = wait_for_convergence(sensors[0], addr5, max_attempts=60)
            results["{}_reconvergence_s".format(phase_name)] = round(reconn, 2) if reconn > 0 else -1
        else:
            apply_loss(sensors[4], loss_pct)

        # Wait for adaptive engine to react
        info("    Waiting {}s for adaptive decision...\n".format(wait_s))
        time.sleep(wait_s)

        # Check what the adaptive engine decided
        current_class = get_adaptive_class(sensors[4])
        current_score = get_adaptive_score(sensors[4])
        info("    Adaptive result: class={} score={}\n".format(current_class, current_score))

        results["{}_class".format(phase_name)] = current_class
        results["{}_score".format(phase_name)] = current_score
        results["{}_expected".format(phase_name)] = expected
        results["{}_correct".format(phase_name)] = 1 if current_class == expected else 0

        # Measure performance
        m = measure_pdr_latency(sensors[0], addr5, count=40)
        results["{}_root_s5_lat".format(phase_name)] = round(m["lat_avg"], 3)
        results["{}_root_s5_pdr".format(phase_name)] = round(m["pdr"], 1)
        info("    root→s5: lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

        if addr5:
            m = measure_pdr_latency(sensors[3], addr5, count=40)
            results["{}_s4s5_lat".format(phase_name)] = round(m["lat_avg"], 3)
            results["{}_s4s5_pdr".format(phase_name)] = round(m["pdr"], 1)
            info("    s4→s5:   lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

        # Routes
        r5 = count_routes(sensors[4])
        results["{}_s5_via".format(phase_name)] = r5["via"]
        results["{}_s5_srh".format(phase_name)] = r5["srh"]

        if current_class == expected:
            info("    ✓ CORRECT: adaptive chose {} as expected\n".format(expected))
        else:
            info("    ✗ MISMATCH: expected {} got {}\n".format(expected, current_class))

    # Cleanup
    apply_loss(sensors[4], 0)
    set_battery(sensors[4], 100)

    # Total switches
    total_switches = count_adaptive_switches(sensors[4])
    results["total_switches"] = total_switches
    info("\n  Total adaptive switches: {}\n".format(total_switches))

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
        vals = [r.get(key) for r in ok if r.get(key) is not None and r.get(key) != -1]
        return statistics.mean(vals) if vals else None

    print("\n" + "=" * 75)
    print("ADAPTIVE ENGINE — SUMMARY ({} runs)".format(len(ok)))
    print("=" * 75)
    print("Convergence: {:.2f}s".format(avg("convergence_s")))

    phases = ["A", "B", "C", "D", "E", "F"]
    descs = ["Estável bat=100%", "Bat=10%", "Bat=100% loss=30%",
             "Recuperado", "Parent change", "Estabilizado"]

    print("\n{:<6} {:<20} {:>8} {:>8} {:>7} {:>10} {:>10}".format(
        "Phase", "Condition", "Expected", "Got", "Score", "root→s5", "PDR"))
    print("-" * 75)

    correct_total = 0
    for phase, desc in zip(phases, descs):
        expected = None
        got = None
        for r in ok:
            expected = r.get("{}_expected".format(phase))
            got = r.get("{}_class".format(phase))
            break
        score = avg("{}_score".format(phase))
        lat = avg("{}_root_s5_lat".format(phase))
        pdr = avg("{}_root_s5_pdr".format(phase))
        correct = avg("{}_correct".format(phase))
        if correct and correct > 0.5:
            correct_total += 1
        mark = "✓" if correct and correct > 0.5 else "✗"

        print("{:<6} {:<20} {:>8} {:>6} {} {:>7} {:>8.3f}ms {:>8.1f}%".format(
            phase, desc,
            expected if expected else "?",
            got if got else "?",
            mark,
            "{:.3f}".format(score) if score else "?",
            lat if lat else 0,
            pdr if pdr else 0))

    print("\nAccuracy: {}/{} phases correct".format(correct_total, len(phases)))
    print("Total adaptive switches (avg): {:.1f}".format(avg("total_switches") or 0))


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
    csv_path = os.path.join(RESULTS_DIR, "adaptive_switch_{}.csv".format(ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults: {}".format(csv_path))

    info("\n*** Stopping network...\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
