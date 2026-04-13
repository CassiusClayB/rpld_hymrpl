#!/usr/bin/env python3
"""
HyMRPL — Teste de Mobilidade com Churn (20 nós)

Simula churn real: em cada fase, um nó SAI da rede e outro nó DIFERENTE
ENTRA na rede. Nunca é o mesmo nó saindo e voltando.

Topologia (mesma do scalability_20):
    sensor1 (Root, S)
    ├── sensor2 (S) ── sensor6 (N) ── sensor10 (N) ── sensor14 (N) ── sensor18 (N)
    ├── sensor3 (S) ── sensor7 (N) ── sensor11 (N) ── sensor15 (N) ── sensor19 (N)
    ├── sensor4 (S) ── sensor8 (N) ── sensor12 (S) ── sensor16 (N) ── sensor20 (N)
    └── sensor5 (N) ── sensor9 (N) ── sensor13 (S) ── sensor17 (N)

Cenário de churn:
  Fase 0: Baseline — todos os 20 nós ativos, mede PDR agregado
  Fase 1: sensor18 SAI (folha branch 1, 5-hop)
           sensor20 já estava fora → ENTRA (folha branch 3, 5-hop)
           Espera: sensor20 não estava na rede, agora entra
  Fase 2: sensor7 SAI (intermediário branch 2, 2-hop — derruba sensor11,15,19)
           sensor18 ENTRA de volta
  Fase 3: sensor9 SAI (intermediário branch 4, 2-hop — derruba sensor13,17)
           sensor7 ENTRA de volta (restaura branch 2)
  Fase 4: sensor5 SAI (1-hop, derruba branch 4 inteira: sensor9,13,17)
           sensor9 ENTRA de volta (mas sem pai, não reconecta até sensor5 voltar)
  Fase 5: sensor5 ENTRA de volta (restaura branch 4)
           sensor15 SAI (folha branch 2, 4-hop)
  Fase 6: Degradação — 20% loss no sensor6 (intermediário branch 1)
           sensor15 ENTRA de volta
  Fase 7: Recovery — remove loss, mede PDR final

Em cada fase mede:
  - PDR agregado (root -> todos os nós ativos)
  - Latência para nós representativos
  - Tempo de reconvergência
  - Nós alcançáveis

Uso: sudo python3 hymrpl_churn_mobility.py [--runs 3] [--modes storing nonstoring hybrid]
"""

import time, re, csv, os, sys, statistics, subprocess
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
NUM_NODES = 20

CONVERGENCE_ADDR_TIMEOUT = 180
CONVERGENCE_PING_TIMEOUT = 300

LINKS = [
    (0, 1), (0, 2), (0, 3), (0, 4),
    (1, 5), (2, 6), (3, 7), (4, 8),
    (5, 9), (6, 10), (7, 11), (8, 12),
    (9, 13), (10, 14), (11, 15), (12, 16),
    (13, 17), (14, 18), (15, 19),
]

HYBRID_CLASSES = {}
for i in range(NUM_NODES):
    name = 'sensor{}'.format(i + 1)
    if i == 0:
        HYBRID_CLASSES[name] = 'S'
    elif i in (1, 2, 3, 11, 12):
        HYBRID_CLASSES[name] = 'S'
    else:
        HYBRID_CLASSES[name] = 'N'


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
    for p, c in LINKS:
        net.addLink(sensors[p], sensors[c], cls=LoWPAN)
    net.build()
    return net, sensors


def start_rpld_all(sensors, mode):
    """Inicia rpld em todos os nós, escalonado por profundidade."""
    depth = {0: 0}
    for p, c in LINKS:
        depth[c] = depth[p] + 1
    max_d = max(depth.values())
    for d in range(max_d + 1):
        nodes_at_d = [i for i, dd in depth.items() if dd == d]
        for idx in nodes_at_d:
            start_rpld_single(sensors[idx], mode)
        time.sleep(4 if d == 0 else 2)


def start_rpld_single(sensor, mode):
    """Inicia rpld em um único nó."""
    cls = HYBRID_CLASSES.get(sensor.name, 'S') if mode == 'hybrid' else 'S'
    conf = gen_config(sensor, mode, cls)
    sensor.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
        conf, sensor.name))


def stop_rpld_all(sensors):
    for s in sensors:
        s.cmd('killall -9 rpld 2>/dev/null')
    time.sleep(2)


def stop_rpld_single(sensor):
    sensor.cmd('killall -9 rpld 2>/dev/null')


def clean_state(sensors):
    for s in sensors:
        iface = get_iface_name(s)
        s.cmd('ip -6 route flush proto static 2>/dev/null')
        s.cmd('ip -6 route flush proto boot 2>/dev/null')
        s.cmd('ip -6 addr flush dev {} scope global 2>/dev/null'.format(iface))
        s.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
        s.cmd('ip link set {} up 2>/dev/null'.format(iface))


def node_leave(sensor):
    """Nó sai da rede: mata rpld + derruba interface."""
    iface = get_iface_name(sensor)
    sensor.cmd('killall -9 rpld 2>/dev/null')
    sensor.cmd('ip link set {} down'.format(iface))
    info("    [LEAVE] {} interface down, rpld killed\n".format(sensor.name))


def node_join(sensor, mode):
    """Nó entra na rede: sobe interface + inicia rpld."""
    iface = get_iface_name(sensor)
    sensor.cmd('ip link set {} up'.format(iface))
    sensor.cmd('ip -6 addr flush dev {} scope global 2>/dev/null'.format(iface))
    time.sleep(1)
    start_rpld_single(sensor, mode)
    info("    [JOIN]  {} interface up, rpld started\n".format(sensor.name))


def get_global_addr(sensor):
    iface = get_iface_name(sensor)
    output = sensor.cmd('ip -6 addr show {} | grep "scope global"'.format(iface))
    match = re.search(r'inet6\s+(\S+)/64', output)
    return match.group(1) if match else None


def wait_for_global_addr(sensor, timeout=60):
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


def measure_aggregate_pdr(root, sensors, skip_indices=None):
    """Mede PDR do root para todos os nós ativos. Retorna (pdr%, reachable, total)."""
    if skip_indices is None:
        skip_indices = set()
    total_tx, total_rx = 0, 0
    reachable = 0
    tested = 0
    for i in range(1, NUM_NODES):
        if i in skip_indices:
            continue
        addr = get_global_addr(sensors[i])
        if not addr:
            continue
        tested += 1
        result = root.cmd('ping6 -c 10 -i 0.2 -W 2 {}'.format(addr))
        match = re.search(r'(\d+) packets transmitted, (\d+) received', result)
        if match:
            tx, rx = int(match.group(1)), int(match.group(2))
            total_tx += tx
            total_rx += rx
            if rx > 0:
                reachable += 1
    pdr = (total_rx / total_tx) * 100 if total_tx > 0 else 0
    return round(pdr, 1), reachable, tested


def measure_latency(src, dst_addr, count=20):
    result = src.cmd('ping6 -c {} -i 0.2 -W 2 {}'.format(count, dst_addr))
    lat_match = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', result)
    if lat_match:
        return round(float(lat_match.group(2)), 3)
    return -1


def add_loss(sensor, pct):
    iface = get_iface_name(sensor)
    sensor.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    if pct > 0:
        sensor.cmd('tc qdisc add dev {} root netem loss {}%'.format(iface, pct))
    info("    [LOSS]  {} = {}%\n".format(sensor.name, pct))


# ============================================================
# Fases do experimento de churn
# ============================================================
# Cada fase: (nome, descrição, nó_que_sai_idx, nó_que_entra_idx, loss_node_idx, loss_pct)
# None = nenhuma ação nesse slot
CHURN_PHASES = [
    # Fase 0: baseline (sem ação)
    ("baseline", "Todos os 20 nós ativos", None, None, None, 0),
    # Fase 1: folha sai, outra folha de branch diferente entra
    # sensor18 (idx=17, folha branch1 5-hop) SAI
    # sensor20 (idx=19, folha branch3 5-hop) estava DOWN desde o início → ENTRA
    ("churn_1", "sensor18 SAI, sensor20 ENTRA", 17, 19, None, 0),
    # Fase 2: intermediário sai (derruba sub-árvore), folha anterior volta
    # sensor7 (idx=6, branch2 2-hop) SAI → derruba sensor11,15,19
    # sensor18 (idx=17) ENTRA de volta
    ("churn_2", "sensor7 SAI (derruba branch2), sensor18 ENTRA", 6, 17, None, 0),
    # Fase 3: outro intermediário sai, anterior volta
    # sensor9 (idx=8, branch4 2-hop) SAI → derruba sensor13,17
    # sensor7 (idx=6) ENTRA de volta (restaura branch2)
    ("churn_3", "sensor9 SAI (derruba branch4), sensor7 ENTRA", 8, 6, None, 0),
    # Fase 4: nó 1-hop sai (derruba branch inteira)
    # sensor5 (idx=4, 1-hop) SAI → derruba toda branch4
    # sensor9 (idx=8) ENTRA (mas sem pai, não reconecta)
    ("churn_4", "sensor5 SAI (branch4 inteira), sensor9 ENTRA (sem pai)", 4, 8, None, 0),
    # Fase 5: restaura branch4, folha de outra branch sai
    # sensor5 (idx=4) ENTRA de volta
    # sensor15 (idx=14, folha branch2 4-hop) SAI
    ("churn_5", "sensor5 ENTRA, sensor15 SAI", 14, 4, None, 0),
    # Fase 6: degradação + restaura folha
    # sensor15 (idx=14) ENTRA de volta
    # 20% loss no sensor6 (idx=5, intermediário branch1)
    ("degrade", "20% loss sensor6, sensor15 ENTRA", None, 14, 5, 20),
    # Fase 7: recovery
    ("recovery", "Remove loss, mede estado final", None, None, 5, 0),
]


def run_churn(sensors, mode, run_id):
    info("\n" + "=" * 60 + "\n")
    info("=== CHURN MOBILITY | {} | Run {} | {} nodes ===\n".format(
        mode.upper(), run_id, NUM_NODES))
    info("=" * 60 + "\n")
    results = {"mode": mode, "run": run_id, "num_nodes": NUM_NODES}

    # Clean start
    stop_rpld_all(sensors)
    clean_state(sensors)
    time.sleep(3)

    # Fase especial: sensor20 (idx=19) começa FORA da rede
    node_leave(sensors[19])
    time.sleep(1)

    # Inicia todos os outros
    start_time = time.time()
    info("  Starting rpld on 19 nodes (sensor20 starts offline)...\n")

    depth = {0: 0}
    for p, c in LINKS:
        depth[c] = depth[p] + 1
    max_d = max(depth.values())
    for d in range(max_d + 1):
        nodes_at_d = [i for i, dd in depth.items() if dd == d and i != 19]
        for idx in nodes_at_d:
            start_rpld_single(sensors[idx], mode)
        time.sleep(4 if d == 0 else 2)

    # Espera convergência inicial
    info("  Waiting for initial convergence...\n")
    # Usa sensor18 (idx=17) como referência (5-hop, mais distante ativo)
    ref_addr = wait_for_global_addr(sensors[17], timeout=CONVERGENCE_ADDR_TIMEOUT)
    if not ref_addr:
        info("  FAIL: sensor18 never got address\n")
        results["initial_conv_s"] = -1
        return results

    conv = wait_for_convergence(sensors[0], ref_addr, max_attempts=CONVERGENCE_PING_TIMEOUT)
    results["initial_conv_s"] = round(time.time() - start_time, 2) if conv > 0 else -1
    info("  Initial convergence: {}s\n".format(results["initial_conv_s"]))

    if conv < 0:
        return results

    time.sleep(10)  # estabilização

    # Track de quais nós estão offline
    offline = {19}  # sensor20 começa offline

    # Executa cada fase
    for phase_name, desc, leave_idx, join_idx, loss_idx, loss_pct in CHURN_PHASES:
        info("\n  --- {} : {} ---\n".format(phase_name.upper(), desc))

        # Ação de saída
        if leave_idx is not None and leave_idx not in offline:
            node_leave(sensors[leave_idx])
            offline.add(leave_idx)
            time.sleep(3)

        # Ação de entrada
        if join_idx is not None and join_idx in offline:
            node_join(sensors[join_idx], mode)
            offline.discard(join_idx)
            time.sleep(5)

        # Ação de degradação
        if loss_idx is not None:
            add_loss(sensors[loss_idx], loss_pct)
            time.sleep(3)

        # Espera reconvergência
        info("    Waiting for stabilization (12s)...\n")
        time.sleep(12)

        # Mede PDR agregado
        pdr, reachable, tested = measure_aggregate_pdr(sensors[0], sensors, skip_indices=offline)
        results["{}_pdr".format(phase_name)] = pdr
        results["{}_reachable".format(phase_name)] = reachable
        results["{}_tested".format(phase_name)] = tested
        results["{}_offline".format(phase_name)] = len(offline)
        info("    PDR={:.1f}% reachable={}/{} offline={}\n".format(
            pdr, reachable, tested, len(offline)))

        # Latência para um nó representativo (sensor14, idx=13, 4-hop se ativo)
        if 13 not in offline:
            addr14 = get_global_addr(sensors[13])
            if addr14:
                lat = measure_latency(sensors[0], addr14)
                results["{}_lat_4hop".format(phase_name)] = lat
                info("    Lat root->sensor14: {}ms\n".format(lat))

        # Latência para nó 1-hop (sensor2, idx=1)
        addr2 = get_global_addr(sensors[1])
        if addr2:
            lat = measure_latency(sensors[0], addr2)
            results["{}_lat_1hop".format(phase_name)] = lat

    # Cleanup
    clean_state(sensors)
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

    print("\n" + "=" * 75)
    print("CHURN MOBILITY — {} NODES".format(NUM_NODES))
    print("=" * 75)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        ok = [r for r in rows if r.get("initial_conv_s", -1) != -1]
        print("\n--- {} ({} runs, {} ok) ---".format(mode.upper(), len(rows), len(ok)))

        def avg(key):
            vals = [r.get(key) for r in ok if r.get(key) is not None and r.get(key) != -1]
            if not vals:
                return "N/A"
            return "{:.2f}".format(statistics.mean(vals))

        print("  Initial conv: {}s".format(avg("initial_conv_s")))
        for phase_name, desc, _, _, _, _ in CHURN_PHASES:
            pdr = avg("{}_pdr".format(phase_name))
            reach = avg("{}_reachable".format(phase_name))
            off = avg("{}_offline".format(phase_name))
            lat4 = avg("{}_lat_4hop".format(phase_name))
            print("  {:<12} PDR={:>6}%  reach={:>4}  offline={:>3}  lat_4hop={:>6}ms".format(
                phase_name, pdr, reach, off, lat4))


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

    # Cria topologia UMA vez e reutiliza pra todos os modos
    info("*** Creating topology (persistent for all modes)\n")
    net, sensors = create_topology()

    # Espera interfaces ficarem prontas
    info("  Waiting 15s for all interfaces...\n")
    time.sleep(15)

    for mode in args.modes:
        info("\n### CHURN MOBILITY: {} ###\n".format(mode.upper()))

        for run_id in range(1, args.runs + 1):
            try:
                r = run_churn(sensors, mode, run_id)
                all_results.append(r)
                save_csv(all_results, os.path.join(
                    RESULTS_DIR, "churn_mobility_{}.csv".format(ts)))
            except Exception as e:
                import traceback
                info("ERROR: {}\n".format(e))
                info(traceback.format_exc() + "\n")
                all_results.append({"mode": mode, "run": run_id,
                                    "num_nodes": NUM_NODES, "initial_conv_s": -1})

    # Salva resultados ANTES de parar a rede
    stop_rpld_all(sensors)
    csv_path = os.path.join(RESULTS_DIR, "churn_mobility_{}.csv".format(ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults: {}".format(csv_path))

    # net.stop() pode matar o processo, então é a última coisa
    info("\n*** Results saved. Stopping network...\n")
    try:
        net.stop()
    except Exception:
        pass


if __name__ == '__main__':
    setLogLevel('info')
    main()
