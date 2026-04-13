#!/usr/bin/env python3
"""
HyMRPL — Teste Completo de Resiliência em Topologia Mesh

Cenário completo com:
  - Topologia mesh (15 nós, enlaces redundantes)
  - PDR, latência, hops dinâmicos
  - CPU e memória por nó (proxy de energia)
  - Emulação de mobilidade via tc netem (degradação progressiva)
  - Captura de pacotes (DIO/DAO/SRH)
  - Churn (saída/entrada de nós)
  - Reconvergência por caminhos alternativos

Topologia mesh (15 nós):

    sensor1 (Root, S)
    ├── sensor2 (S)  ├── sensor3 (S)  └── sensor4 (N)
    │                │                │
    sensor5 (N)──────┘                sensor7 (S)
    │    └── sensor6 (N)──────────────┘
    │         │
    sensor8 (N)    sensor9 (N)    sensor10 (N)
    │              │    │              │
    sensor11 (N)───┘    sensor12 (N)──┘
    │                   │
    sensor13 (N)────────┘
    │
    sensor15 (N)    sensor14 (N)

Fases (12 fases de complexidade crescente):
  0.  Baseline: todos ativos, métricas de referência
  1.  Mobilidade leve: 10% loss no sensor5 via tc netem
  2.  Mobilidade severa: 30% loss no sensor5
  3.  Churn: derruba sensor5, nós reconvergem via sensor6/7
  4a. Restaura sensor5, aguarda reconvergência
  4b. Derruba sensor7, nós reconvergem via sensor5
  5a. Restaura sensor7, aguarda reconvergência
  5b. Mobilidade + churn: 20% loss no sensor8, derruba sensor3
  6.  Restaura sensor3, remove loss
  7.  Churn duplo: derruba sensor9 e sensor10 simultaneamente
  8.  Restaura tudo
  9.  Verificação final: confirma reconvergência completa

Uso: sudo python3 hymrpl_mesh_resilience.py [--runs 3] [--modes storing nonstoring hybrid]
"""

import time, re, csv, os, statistics
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
PING_COUNT = 10
NUM_NODES = 15

CONVERGENCE_ADDR_TIMEOUT = 120
CONVERGENCE_PING_TIMEOUT = 180

# Links bidirecionais da topologia mesh
LINKS = [
    (0, 1), (0, 2), (0, 3),
    (1, 4), (1, 5),
    (2, 4), (2, 6),
    (3, 5), (3, 6),
    (4, 7), (4, 8),
    (5, 7), (5, 9),
    (6, 8), (6, 9),
    (7, 10),
    (8, 10), (8, 11),
    (9, 11),
    (10, 12),
    (11, 12), (11, 13),
    (12, 14),
]

# Adjacência bidirecional para BFS
ADJ = {i: set() for i in range(NUM_NODES)}
for p, c in LINKS:
    ADJ[p].add(c)
    ADJ[c].add(p)

HYBRID_CLASSES = {}
for i in range(NUM_NODES):
    name = 'sensor{}'.format(i + 1)
    if i == 0:
        HYBRID_CLASSES[name] = 'S'
    elif i in (1, 2, 6):
        HYBRID_CLASSES[name] = 'S'
    else:
        HYBRID_CLASSES[name] = 'N'

# ── Fases do experimento ──
PHASES = [
    {
        "name": "0_baseline",
        "desc": "Todos os nós ativos, sem degradação",
        "kill": [], "restore": [], "loss": [], "clear_loss": [],
    },
    {
        "name": "1_mobility_10pct",
        "desc": "10% packet loss no sensor5 (mobilidade leve)",
        "kill": [], "restore": [], "loss": [(4, 10)], "clear_loss": [],
    },
    {
        "name": "2_mobility_30pct",
        "desc": "30% packet loss no sensor5 (mobilidade severa)",
        "kill": [], "restore": [], "loss": [(4, 30)], "clear_loss": [],
    },
    {
        "name": "3_churn_kill5",
        "desc": "Derruba sensor5 — nós reconvergem via sensor6/7",
        "kill": [4], "restore": [], "loss": [], "clear_loss": [4],
    },
    {
        "name": "4a_restore5",
        "desc": "Restaura sensor5 — aguarda reconvergência completa",
        "kill": [], "restore": [4], "loss": [], "clear_loss": [],
    },
    {
        "name": "4b_kill7",
        "desc": "Derruba sensor7 — nós reconvergem via sensor5 restaurado",
        "kill": [6], "restore": [], "loss": [], "clear_loss": [],
    },
    {
        "name": "5a_restore7",
        "desc": "Restaura sensor7 — aguarda reconvergência",
        "kill": [], "restore": [6], "loss": [], "clear_loss": [],
    },
    {
        "name": "5b_mobility_churn",
        "desc": "20% loss no sensor8 + derruba sensor3",
        "kill": [2], "restore": [], "loss": [(7, 20)], "clear_loss": [],
    },
    {
        "name": "6_restore3_clear",
        "desc": "Restaura sensor3, remove loss do sensor8",
        "kill": [], "restore": [2], "loss": [], "clear_loss": [7],
    },
    {
        "name": "7_churn_double",
        "desc": "Derruba sensor9 e sensor10 simultaneamente",
        "kill": [8, 9], "restore": [], "loss": [], "clear_loss": [],
    },
    {
        "name": "8_restore_all",
        "desc": "Restaura sensor9 e sensor10",
        "kill": [], "restore": [8, 9], "loss": [], "clear_loss": [],
    },
    {
        "name": "9_final_check",
        "desc": "Verificação final — todos ativos, sem degradação",
        "kill": [], "restore": [], "loss": [], "clear_loss": [],
    },
]


# ── Funções utilitárias ──

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
    info("  Waiting 12s for interfaces...\n")
    time.sleep(12)
    ready = sum(1 for s in sensors
                if get_iface_name(s) in s.cmd('ip link show {} 2>/dev/null'.format(
                    get_iface_name(s))))
    info("  Interfaces ready: {}/{}\n".format(ready, NUM_NODES))
    return net, sensors


def start_rpld(sensors, mode, skip_set=None):
    """Inicia rpld em ordem de profundidade BFS (bidirecional)."""
    if skip_set is None:
        skip_set = set()
    depth = {0: 0}
    visited = {0}
    queue = [0]
    while queue:
        n = queue.pop(0)
        for nb in ADJ[n]:
            if nb not in visited:
                visited.add(nb)
                depth[nb] = depth[n] + 1
                queue.append(nb)
    max_depth = max(depth.values()) if depth else 0
    for d in range(max_depth + 1):
        nodes_at_depth = [i for i, dd in depth.items() if dd == d]
        for idx in nodes_at_depth:
            if idx in skip_set:
                continue
            s = sensors[idx]
            cls = HYBRID_CLASSES.get(s.name, 'S') if mode == 'hybrid' else 'S'
            conf = gen_config(s, mode, cls)
            s.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
                conf, s.name))
        time.sleep(5 if d == 0 else 3)


def stop_rpld(sensors):
    for s in sensors:
        s.cmd('killall -9 rpld 2>/dev/null')
    time.sleep(2)


def kill_node(sensor):
    """Simula saída do nó: mata rpld e aplica 100% packet loss.
    NÃO derruba a interface para preservar o estado 802.15.4/6LoWPAN."""
    sensor.cmd('killall -9 rpld 2>/dev/null')
    iface = get_iface_name(sensor)
    # Flush rotas para não servir de relay
    sensor.cmd('ip -6 route flush proto static 2>/dev/null')
    sensor.cmd('ip -6 route flush proto boot 2>/dev/null')
    # 100% loss = nó completamente isolado da rede
    sensor.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    sensor.cmd('tc qdisc add dev {} root netem loss 100%'.format(iface))
    info("    KILLED {} (rpld stopped + 100% loss)\n".format(sensor.name))


def restore_node(sensor, mode, sensors=None, killed_set=None):
    """Restaura um nó e reinicia rpld em todos os nós vivos.
    Necessário porque o event loop do rpld para de processar eventos
    depois de ~30s (bug do rpld/libev com interfaces 6LoWPAN)."""
    iface = get_iface_name(sensor)
    sensor_idx = int(sensor.name.replace('sensor', '')) - 1

    # Remove isolamento do nó
    sensor.cmd('killall -9 rpld 2>/dev/null')
    sensor.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    sensor.cmd('ip -6 route flush proto static 2>/dev/null')
    sensor.cmd('ip -6 route flush proto boot 2>/dev/null')
    sensor.cmd('ip -6 route flush proto ra 2>/dev/null')
    sensor.cmd('ip -6 addr flush dev {} scope global 2>/dev/null'.format(iface))
    sensor.cmd('ip link set {} up 2>/dev/null'.format(iface))
    time.sleep(1)
    # Restaura link-local original
    original_ll = 'fe80::{:x}'.format(sensor_idx + 1)
    current_ll = sensor.cmd(
        'ip -6 addr show dev {} scope link 2>/dev/null'.format(iface))
    if original_ll not in current_ll:
        sensor.cmd('ip -6 addr flush dev {} scope link 2>/dev/null'.format(iface))
        sensor.cmd('ip -6 addr add {}/64 dev {} scope link 2>/dev/null'.format(
            original_ll, iface))
    info("    RESTORED {} (interface ready)\n".format(sensor.name))

    # Reinicia rpld em todos os nós vivos pra forçar reconvergência
    # (o event loop do rpld para depois de ~30s, então os vizinhos
    # não processam DIS nem emitem DIO sem restart)
    if sensors is not None:
        ks = killed_set if killed_set is not None else set()
        ks_after = ks - {sensor_idx}

        # Para rpld em todos os nós vivos
        for i, s in enumerate(sensors):
            if i in ks_after:
                continue
            s.cmd('killall -9 rpld 2>/dev/null')
        time.sleep(1)

        # Limpa estado de roteamento (mas NÃO derruba interfaces)
        for i, s in enumerate(sensors):
            if i in ks_after:
                continue
            s_iface = get_iface_name(s)
            s.cmd('ip -6 route flush proto static 2>/dev/null')
            s.cmd('ip -6 route flush proto boot 2>/dev/null')
            s.cmd('ip -6 addr flush dev {} scope global 2>/dev/null'.format(s_iface))

        # Reinicia rpld em ordem BFS
        start_rpld(sensors, mode, skip_set=ks_after)
    else:
        cls = HYBRID_CLASSES.get(sensor.name, 'S') if mode == 'hybrid' else 'S'
        conf = gen_config(sensor, mode, cls)
        sensor.cmd('rpld -C {} -m stderr -d 3 > /tmp/rpld_{}.log 2>&1 &'.format(
            conf, sensor.name))


def add_packet_loss(sensor, loss_pct):
    iface = get_iface_name(sensor)
    sensor.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    if loss_pct > 0:
        sensor.cmd('tc qdisc add dev {} root netem loss {}%'.format(iface, loss_pct))
        info("    tc netem: {}% loss on {}\n".format(loss_pct, sensor.name))


def clear_packet_loss(sensor):
    iface = get_iface_name(sensor)
    sensor.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))
    info("    tc netem: cleared on {}\n".format(sensor.name))


def clean_state(sensors):
    for s in sensors:
        iface = get_iface_name(s)
        s.cmd('ip -6 route flush proto static 2>/dev/null')
        s.cmd('ip -6 route flush proto boot 2>/dev/null')
        s.cmd('ip -6 route flush proto ra 2>/dev/null')
        s.cmd('ip -6 addr flush dev {} scope global 2>/dev/null'.format(iface))
        s.cmd('tc qdisc del dev {} root 2>/dev/null'.format(iface))


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


# ── Funções de medição ──

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
    if lat_values:
        idx_p95 = min(int(len(lat_values) * 0.95), len(lat_values) - 1)
        lat_p95 = sorted(lat_values)[idx_p95]
    else:
        lat_p95 = 0
    return {"pdr": pdr, "lat_avg": lat_avg, "lat_p95": lat_p95}


def get_hop_count(src, dst_addr):
    result = src.cmd('traceroute6 -n -m 10 -w 2 -q 1 {} 2>/dev/null'.format(dst_addr))
    hops = 0
    for line in result.strip().split('\n')[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            if '*' not in line:
                hops = int(parts[0])
    return hops if hops > 0 else -1


def measure_cpu_mem(sensor):
    """Mede CPU e memória do rpld."""
    output = sensor.cmd('ps -o %cpu,%mem,rss -C rpld --no-headers 2>/dev/null')
    if not output.strip():
        pid = sensor.cmd('pgrep -f rpld 2>/dev/null').strip().split('\n')[0].strip()
        if pid and pid.isdigit():
            output = sensor.cmd('ps -o %cpu,%mem,rss -p {} --no-headers 2>/dev/null'.format(pid))
    if not output.strip():
        return 0, 0
    # Pega primeira linha válida (pode ter múltiplas se ps retornar mais de um)
    for line in output.strip().split('\n'):
        parts = line.strip().split()
        if len(parts) >= 3:
            try:
                return float(parts[0]), int(parts[2]) / 1024.0
            except (ValueError, IndexError):
                continue
    return 0, 0


def count_routes(sensor):
    output = sensor.cmd('ip -6 route show')
    return {"routes_srh": output.count('encap rpl'),
            "routes_via": output.count('via fe80')}


def capture_packets(sensor, duration=10):
    """Captura pacotes por N segundos e conta DIO, DAO e SRH."""
    iface = get_iface_name(sensor)
    pcap = '/tmp/pcap_{}_{}.pcap'.format(sensor.name, int(time.time()))

    sensor.cmd('timeout {} tcpdump -i {} -w {} 2>/dev/null &'.format(
        duration, iface, pcap))
    time.sleep(duration + 2)

    counts = {"dio": 0, "dao": 0, "srh": 0}

    try:
        out = sensor.cmd(
            'tshark -r {} -Y "icmpv6.type == 155 && icmpv6.code == 1" '
            '-T fields -e frame.number 2>/dev/null | wc -l'.format(pcap))
        val = out.strip().split('\n')[-1].strip()
        counts["dio"] = int(val) if val.isdigit() else 0
    except (ValueError, Exception):
        pass

    try:
        out = sensor.cmd(
            'tshark -r {} -Y "icmpv6.type == 155 && icmpv6.code == 2" '
            '-T fields -e frame.number 2>/dev/null | wc -l'.format(pcap))
        val = out.strip().split('\n')[-1].strip()
        counts["dao"] = int(val) if val.isdigit() else 0
    except (ValueError, Exception):
        pass

    try:
        out = sensor.cmd(
            'tshark -r {} -Y "ipv6.routing.type == 3" '
            '-T fields -e frame.number 2>/dev/null | wc -l'.format(pcap))
        val = out.strip().split('\n')[-1].strip()
        counts["srh"] = int(val) if val.isdigit() else 0
    except (ValueError, Exception):
        pass

    sensor.cmd('rm -f {} 2>/dev/null'.format(pcap))
    return counts


def measure_phase(sensors, root, phase_name, killed_set, mode):
    """Coleta todas as métricas para uma fase."""
    info("  [MEASURE] Phase: {}\n".format(phase_name))
    results = {"phase": phase_name}

    # Captura de pacotes no root (DIO/DAO)
    info("    Capturing packets (10s)...\n")
    pcap_root = capture_packets(root, duration=10)
    results["root_dio_10s"] = pcap_root["dio"]
    results["root_dao_10s"] = pcap_root["dao"]

    # Captura SRH em nó intermediário ativo
    srh_node_idx = 1 if 1 not in killed_set else (3 if 3 not in killed_set else None)
    if srh_node_idx is not None:
        pcap_mid = capture_packets(sensors[srh_node_idx], duration=10)
        results["mid_srh_10s"] = pcap_mid["srh"]
    else:
        results["mid_srh_10s"] = 0

    # Endereços globais dos nós vivos
    addrs = {}
    for i, s in enumerate(sensors):
        if i in killed_set:
            continue
        addr = get_global_addr(s)
        if addr:
            addrs[s.name] = addr

    # PDR, latência, hops por nó
    reachable = 0
    total_tx, total_rx = 0, 0
    lat_values = []
    hop_counts = []

    for i in range(1, NUM_NODES):
        if i in killed_set:
            continue
        dst_addr = addrs.get(sensors[i].name)
        if not dst_addr:
            info("    -> {:10s} NO GLOBAL ADDR [UNREACHABLE]\n".format(sensors[i].name))
            total_tx += PING_COUNT
            continue

        m = measure_pdr_latency(root, dst_addr, count=PING_COUNT)
        hops = get_hop_count(root, dst_addr) if m["pdr"] > 0 else -1

        if m["pdr"] > 0:
            reachable += 1
            lat_values.append(m["lat_avg"])
            if hops > 0:
                hop_counts.append(hops)

        total_tx += PING_COUNT
        total_rx += int(PING_COUNT * m["pdr"] / 100)

        status = "OK" if m["pdr"] > 0 else "UNREACHABLE"
        info("    -> {:10s} PDR={:5.1f}% lat={:7.3f}ms hops={:2d} [{}]\n".format(
            sensors[i].name, m["pdr"], m["lat_avg"],
            hops if hops > 0 else -1, status))

    alive = NUM_NODES - len(killed_set)
    results["alive_nodes"] = alive
    results["reachable"] = reachable
    results["pdr"] = round((total_rx / total_tx) * 100, 1) if total_tx > 0 else 0
    results["lat_avg"] = round(statistics.mean(lat_values), 3) if lat_values else 0
    if lat_values:
        idx_p95 = min(int(len(lat_values) * 0.95), len(lat_values) - 1)
        results["lat_p95"] = round(sorted(lat_values)[idx_p95], 3)
    else:
        results["lat_p95"] = 0
    results["hops_avg"] = round(statistics.mean(hop_counts), 1) if hop_counts else 0
    results["hops_max"] = max(hop_counts) if hop_counts else 0
    results["hops_min"] = min(hop_counts) if hop_counts else 0

    # Rotas no root
    routes = count_routes(root)
    results["routes_srh"] = routes["routes_srh"]
    results["routes_via"] = routes["routes_via"]

    # CPU e memória de todos os nós ativos
    cpu_vals, mem_vals = [], []
    for i, s in enumerate(sensors):
        if i in killed_set:
            continue
        cpu, mem = measure_cpu_mem(s)
        cpu_vals.append(cpu)
        mem_vals.append(mem)

    results["root_cpu"] = cpu_vals[0] if cpu_vals else 0
    results["root_mem_mb"] = round(mem_vals[0], 2) if mem_vals else 0
    results["avg_cpu"] = round(statistics.mean(cpu_vals), 2) if cpu_vals else 0
    results["avg_mem_mb"] = round(statistics.mean(mem_vals), 2) if mem_vals else 0
    results["max_cpu"] = round(max(cpu_vals), 2) if cpu_vals else 0
    results["max_mem_mb"] = round(max(mem_vals), 2) if mem_vals else 0

    info("  [RESULT] Phase {}: reach={}/{} PDR={:.1f}% lat={:.3f}ms "
         "hops={:.1f} DIO={} DAO={} SRH={} CPU={:.1f}% MEM={:.1f}MB\n".format(
             phase_name, reachable, alive - 1, results["pdr"],
             results["lat_avg"], results["hops_avg"],
             results["root_dio_10s"], results["root_dao_10s"],
             results["mid_srh_10s"], results["avg_cpu"], results["avg_mem_mb"]))

    return results


# ── Execução principal ──

def run_single(sensors, mode, run_id, runs_total):
    info("\n" + "=" * 70 + "\n")
    info("=== {} | Run {}/{} | MESH RESILIENCE (COMPLETE) ===\n".format(
        mode.upper(), run_id, runs_total))
    all_phase_results = []

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    # Sobe todas as interfaces
    for s in sensors:
        iface = get_iface_name(s)
        s.cmd('ip link set {} up 2>/dev/null'.format(iface))
    time.sleep(2)

    start_rpld(sensors, mode)

    # Espera convergência até o nó mais distante
    info("  Waiting for convergence (sensor15)...\n")
    farthest_addr = wait_for_global_addr(sensors[14])
    if farthest_addr:
        conv = wait_for_convergence(sensors[0], farthest_addr)
        if conv > 0:
            info("  Convergence to sensor15: {:.2f}s\n".format(conv))
        else:
            info("  WARNING: convergence to sensor15 FAILED\n")
    else:
        info("  WARNING: sensor15 did not get global address\n")

    info("  Stabilizing (20s)...\n")
    time.sleep(20)

    # Executa fases
    killed_set = set()

    for phase in PHASES:
        phase_name = phase["name"]
        info("\n--- Phase: {} ---\n".format(phase_name))
        info("    {}\n".format(phase["desc"]))

        # 1. Restaura nós primeiro (precisam de tempo pra reconverger)
        for idx in phase["restore"]:
            restore_node(sensors[idx], mode, sensors=sensors, killed_set=killed_set)
            killed_set.discard(idx)

        # 2. Remove loss
        for idx in phase["clear_loss"]:
            if idx not in killed_set:
                clear_packet_loss(sensors[idx])

        # 3. Aplica loss
        for idx, pct in phase["loss"]:
            add_packet_loss(sensors[idx], pct)

        # 4. Derruba nós por último
        for idx in phase["kill"]:
            kill_node(sensors[idx])
            killed_set.add(idx)

        # Espera reconvergência
        has_action = phase["kill"] or phase["restore"] or phase["loss"] or phase["clear_loss"]
        if has_action:
            has_restore = bool(phase["restore"])
            wait_time = 45 if has_restore else 25
            info("    Waiting {}s for reconvergence...\n".format(wait_time))
            time.sleep(wait_time)

        # Mede
        phase_results = measure_phase(sensors, sensors[0], phase_name, killed_set, mode)
        phase_results["mode"] = mode
        phase_results["run"] = run_id
        all_phase_results.append(phase_results)

    return all_phase_results


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

    print("\n" + "=" * 100)
    print("MESH RESILIENCE TEST — {} NODES (COMPLETE)".format(NUM_NODES))
    print("=" * 100)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        print("\n--- {} ---".format(mode.upper()))

        phases = {}
        for r in rows:
            phases.setdefault(r["phase"], []).append(r)

        header = "{:22s} {:>6s} {:>6s} {:>7s} {:>7s} {:>5s} {:>5s} {:>5s} {:>5s} {:>6s} {:>6s}".format(
            "Phase", "PDR%", "Reach", "Lat", "LatP95", "Hops", "DIO", "DAO", "SRH", "CPU%", "MEM")
        print("  " + header)
        print("  " + "-" * len(header))

        for phase in PHASES:
            pname = phase["name"]
            phase_rows = phases.get(pname, [])
            if not phase_rows:
                continue

            def avg(key, rows=phase_rows):
                vals = [r.get(key, 0) for r in rows if r.get(key) is not None]
                return statistics.mean(vals) if vals else 0

            print("  {:22s} {:>5.1f}% {:>5.0f} {:>6.3f} {:>6.3f} {:>5.1f} {:>5.0f} {:>5.0f} {:>5.0f} {:>5.1f} {:>5.1f}".format(
                pname, avg("pdr"), avg("reachable"),
                avg("lat_avg"), avg("lat_p95"), avg("hops_avg"),
                avg("root_dio_10s"), avg("root_dao_10s"), avg("mid_srh_10s"),
                avg("avg_cpu"), avg("avg_mem_mb")))


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='HyMRPL Mesh Resilience Test (complete scenario)')
    parser.add_argument('--runs', type=int, default=3,
                        help='Number of runs per mode (default: 3)')
    parser.add_argument('--modes', nargs='+',
                        default=['storing', 'nonstoring', 'hybrid'],
                        choices=['storing', 'nonstoring', 'hybrid'],
                        help='Modes to test')
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    all_results = []

    info("*** Creating MESH topology ({} nodes, {} links)\n".format(
        NUM_NODES, len(LINKS)))
    net, sensors = create_topology()

    for mode in args.modes:
        info("\n### MESH RESILIENCE: {} ###\n".format(mode.upper()))
        for run_id in range(1, args.runs + 1):
            try:
                phase_results = run_single(sensors, mode, run_id, args.runs)
                all_results.extend(phase_results)
                # Salva incrementalmente
                save_csv(all_results, os.path.join(
                    RESULTS_DIR, "mesh_resilience_{}.csv".format(ts)))
            except Exception as e:
                import traceback
                info("ERROR run {}: {}\n".format(run_id, e))
                info(traceback.format_exc() + "\n")

    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "mesh_resilience_{}.csv".format(ts))
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
