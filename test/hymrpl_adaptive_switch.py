#!/usr/bin/env python3
"""
HyMRPL — Experimento de troca adaptativa de classe.

Decisão automática baseada em 3 critérios:
  1. Perda de pacotes (PDR): se PDR < 80% → favorece N
  2. Energia residual: se bateria < 30% → favorece N (menos overhead)
  3. Mobilidade (estabilidade do parent): se parent mudou recentemente → favorece N

Lógica de decisão:
  score = w_pdr * score_pdr + w_energy * score_energy + w_mobility * score_mobility
  Se score >= THRESHOLD → Classe S (nó estável, com recursos)
  Se score <  THRESHOLD → Classe N (nó instável, restrito)

Cenários simulados:
  Fase A: sensor5 estável, bateria cheia, sem perda → deve ser S
  Fase B: degradação no link (20% loss) → PDR cai → deve trocar pra N
  Fase C: link recupera, mas bateria baixa (simulada) → continua N
  Fase D: link bom + bateria ok + estável → volta pra S
  Fase E: mobilidade (parent change simulado) → troca pra N
  Fase F: estabiliza no novo parent → volta pra S

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

import time, re, csv, os, statistics, random
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
FIFO_PATH = "/tmp/hymrpl_cmd"

# --- Adaptive decision parameters ---
W_PDR = 0.4        # peso da perda de pacotes
W_ENERGY = 0.3     # peso da energia residual
W_MOBILITY = 0.3   # peso da estabilidade (mobilidade)
THRESHOLD = 0.75    # score >= threshold → Classe S

HYBRID_CLASSES = {
    'sensor1': 'S',
    'sensor2': 'N',
    'sensor3': 'S',
    'sensor4': 'S',
    'sensor5': 'N',  # initial, will be managed by adaptive logic
}


class AdaptiveClassManager:
    """
    Gerenciador adaptativo de classe para um nó.
    Combina 3 métricas pra decidir se o nó deve ser S ou N.
    """

    def __init__(self, node_name, w_pdr=W_PDR, w_energy=W_ENERGY,
                 w_mobility=W_MOBILITY, threshold=THRESHOLD):
        self.node_name = node_name
        self.w_pdr = w_pdr
        self.w_energy = w_energy
        self.w_mobility = w_mobility
        self.threshold = threshold
        self.current_class = 'N'
        self.history = []

    def compute_score(self, pdr, energy_pct, parent_stable):
        """
        Calcula score composto:
          pdr: 0-100 (porcentagem de entrega)
          energy_pct: 0-100 (porcentagem de bateria restante)
          parent_stable: True/False (parent não mudou nos últimos N segundos)

        Retorna score 0.0-1.0 e a classe recomendada.
        """
        # Normaliza PDR: 100% → 1.0, 0% → 0.0
        score_pdr = min(pdr / 100.0, 1.0)

        # Normaliza energia: 100% → 1.0, 0% → 0.0
        score_energy = min(energy_pct / 100.0, 1.0)

        # Mobilidade: estável → 1.0, instável → 0.0
        score_mobility = 1.0 if parent_stable else 0.0

        score = (self.w_pdr * score_pdr +
                 self.w_energy * score_energy +
                 self.w_mobility * score_mobility)

        recommended = 'S' if score >= self.threshold else 'N'

        decision = {
            'score': round(score, 3),
            'score_pdr': round(score_pdr, 3),
            'score_energy': round(score_energy, 3),
            'score_mobility': round(score_mobility, 3),
            'pdr': round(pdr, 1),
            'energy_pct': round(energy_pct, 1),
            'parent_stable': parent_stable,
            'recommended': recommended,
            'previous': self.current_class,
            'switched': recommended != self.current_class,
        }

        self.current_class = recommended
        self.history.append(decision)
        return decision

    def get_class(self):
        return self.current_class


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


def send_fifo_cmd(sensor, cmd_str):
    sensor.cmd('echo "{}" > {} 2>/dev/null'.format(cmd_str, FIFO_PATH))
    info("  FIFO: sent '{}' to {}\n".format(cmd_str, sensor.name))


def simulate_parent_change(sensor):
    """Simula mudança de parent: derruba e sobe o link."""
    iface = get_iface_name(sensor)
    info("  Simulating parent change on {}...\n".format(sensor.name))
    sensor.cmd('ip link set {} down'.format(iface))
    time.sleep(3)
    sensor.cmd('ip link set {} up'.format(iface))
    time.sleep(5)


def apply_loss(sensor, loss_pct):
    """Aplica perda de pacotes no link do sensor."""
    iface = get_iface_name(sensor)
    sensor.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    if loss_pct > 0:
        sensor.cmd('tc qdisc add dev {} root netem loss {}%'.format(iface, loss_pct))
        info("  Applied {}% loss on {}\n".format(loss_pct, sensor.name))
    else:
        info("  Removed loss on {}\n".format(sensor.name))


def count_routes(sensor):
    output = sensor.cmd('ip -6 route show')
    return {"srh": output.count('encap rpl'), "via": output.count('via fe80')}


def run_experiment(sensors, run_id):
    """
    6 fases que exercitam os 3 critérios de decisão:

    Fase A: Tudo estável (PDR alto, bateria 90%, parent estável) → espera S
    Fase B: Link degradado 25% loss (PDR cai, bateria 80%, estável) → espera N
    Fase C: Link recupera, bateria baixa 20% (PDR ok, estável) → espera N
    Fase D: Link ok, bateria recupera 70%, estável → espera S
    Fase E: Mobilidade (parent change, PDR ok, bateria 70%) → espera N
    Fase F: Estabiliza (PDR ok, bateria 65%, parent estável 30s) → espera S
    """
    info("\n{}\n=== ADAPTIVE SWITCH | Run {} ===\n{}\n".format("=" * 60, run_id, "=" * 60))
    results = {"run": run_id}
    manager = AdaptiveClassManager('sensor5')

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    start_time = time.time()
    start_rpld(sensors)

    info("  Waiting for convergence...\n")
    addr5 = wait_for_global_addr(sensors[4])
    if not addr5:
        info("  FAIL: sensor5 no address\n")
        results["convergence_s"] = -1
        return results

    root_addr = get_global_addr(sensors[0])
    addr4 = get_global_addr(sensors[3])
    conv = wait_for_convergence(sensors[0], addr5)
    if conv < 0:
        info("  FAIL: no convergence\n")
        results["convergence_s"] = -1
        return results

    results["convergence_s"] = round(time.time() - start_time, 2)
    info("  Convergence: {}s\n".format(results["convergence_s"]))
    time.sleep(10)

    phases = [
        # (name, description, loss_pct, energy_pct, parent_stable, do_parent_change)
        ("A", "Estável, bateria cheia",          0,  90, True,  False),
        ("B", "Link degradado 25% loss",        25,  80, True,  False),
        ("C", "Link ok, bateria baixa 20%",      0,  20, True,  False),
        ("D", "Tudo ok, bateria 70%",            0,  70, True,  False),
        ("E", "Mobilidade (parent change)",      0,  70, False, True),
        ("F", "Estabilizado após mobilidade",    0,  65, True,  False),
    ]

    for phase_name, desc, loss_pct, energy_pct, parent_stable, do_parent_change in phases:
        info("\n  --- PHASE {}: {} ---\n".format(phase_name, desc))

        # Apply network conditions
        if do_parent_change:
            simulate_parent_change(sensors[4])
            # Re-wait for connectivity
            time.sleep(5)
            reconn = wait_for_convergence(sensors[0], addr5, max_attempts=60)
            results["{}_reconvergence_s".format(phase_name)] = round(reconn, 2) if reconn > 0 else -1
            time.sleep(5)
        else:
            apply_loss(sensors[4], loss_pct)
            time.sleep(5)

        # Measure actual PDR
        m = measure_pdr_latency(sensors[0], addr5, count=30)
        actual_pdr = m["pdr"]
        info("    Measured PDR: {:.1f}%\n".format(actual_pdr))

        # Adaptive decision
        decision = manager.compute_score(actual_pdr, energy_pct, parent_stable)
        recommended = decision['recommended']
        info("    Decision: score={:.3f} (pdr={:.3f} energy={:.3f} mob={:.3f}) → {}\n".format(
            decision['score'], decision['score_pdr'],
            decision['score_energy'], decision['score_mobility'],
            recommended))

        # Apply class change if needed
        if decision['switched']:
            send_fifo_cmd(sensors[4], "CLASS_{}".format(recommended))
            info("    Waiting for route update (12s)...\n")
            time.sleep(12)
        else:
            info("    No switch needed (already {})\n".format(recommended))
            time.sleep(3)

        # Measure performance after decision
        # root → sensor5
        m = measure_pdr_latency(sensors[0], addr5, count=40)
        results["{}_root_s5_lat".format(phase_name)] = round(m["lat_avg"], 3)
        results["{}_root_s5_p95".format(phase_name)] = round(m["lat_p95"], 3)
        results["{}_root_s5_pdr".format(phase_name)] = round(m["pdr"], 1)
        info("    root→s5: lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

        # sensor4 → sensor5 (local)
        if addr5:
            m = measure_pdr_latency(sensors[3], addr5, count=40)
            results["{}_s4s5_lat".format(phase_name)] = round(m["lat_avg"], 3)
            results["{}_s4s5_p95".format(phase_name)] = round(m["lat_p95"], 3)
            results["{}_s4s5_pdr".format(phase_name)] = round(m["pdr"], 1)
            info("    s4→s5:   lat={:.3f}ms PDR={:.1f}%\n".format(m["lat_avg"], m["pdr"]))

        # sensor5 → root
        if root_addr:
            m = measure_pdr_latency(sensors[4], root_addr, count=40)
            results["{}_s5root_lat".format(phase_name)] = round(m["lat_avg"], 3)
            results["{}_s5root_pdr".format(phase_name)] = round(m["pdr"], 1)

        # Store decision info
        results["{}_class".format(phase_name)] = recommended
        results["{}_score".format(phase_name)] = decision['score']
        results["{}_score_pdr".format(phase_name)] = decision['score_pdr']
        results["{}_score_energy".format(phase_name)] = decision['score_energy']
        results["{}_score_mobility".format(phase_name)] = decision['score_mobility']
        results["{}_energy_pct".format(phase_name)] = energy_pct
        results["{}_switched".format(phase_name)] = 1 if decision['switched'] else 0

        # Routes
        r5 = count_routes(sensors[4])
        results["{}_s5_via".format(phase_name)] = r5["via"]
        results["{}_s5_srh".format(phase_name)] = r5["srh"]

    # Cleanup
    apply_loss(sensors[4], 0)
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
        if not vals:
            return None
        return statistics.mean(vals)

    print("\n" + "=" * 75)
    print("ADAPTIVE CLASS SWITCH — SUMMARY ({} runs)".format(len(ok)))
    print("=" * 75)
    print("Convergence: {:.2f}s".format(avg("convergence_s")))
    print("Weights: PDR={} Energy={} Mobility={} | Threshold={}".format(
        W_PDR, W_ENERGY, W_MOBILITY, THRESHOLD))

    phases = [
        ("A", "Estável, bat=90%"),
        ("B", "25% loss, bat=80%"),
        ("C", "Link ok, bat=20%"),
        ("D", "Tudo ok, bat=70%"),
        ("E", "Parent change, bat=70%"),
        ("F", "Estabilizado, bat=65%"),
    ]

    print("\n{:<6} {:<24} {:>6} {:>7} {:>10} {:>10} {:>10} {:>5}".format(
        "Phase", "Condition", "Class", "Score", "root→s5", "s4→s5", "PDR", "Sw?"))
    print("-" * 75)

    for phase, desc in phases:
        cls = None
        for r in ok:
            c = r.get("{}_class".format(phase))
            if c:
                cls = c
                break
        score = avg("{}_score".format(phase))
        rs5 = avg("{}_root_s5_lat".format(phase))
        s4s5 = avg("{}_s4s5_lat".format(phase))
        pdr = avg("{}_root_s5_pdr".format(phase))
        sw = avg("{}_switched".format(phase))

        print("{:<6} {:<24} {:>6} {:>7.3f} {:>8.3f}ms {:>8.3f}ms {:>8.1f}% {:>5}".format(
            phase, desc,
            cls if cls else "?",
            score if score else 0,
            rs5 if rs5 else 0,
            s4s5 if s4s5 else 0,
            pdr if pdr else 0,
            "YES" if sw and sw > 0.5 else "no"))

    print("\nExpected class transitions:")
    print("  A→S (stable) → B→N (loss) → C→N (low bat) → D→S (recovered)")
    print("  → E→N (mobility) → F→S (stabilized)")


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
