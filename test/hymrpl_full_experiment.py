#!/usr/bin/env python3
"""
HyMRPL — Experimento completo: métricas extras + mobilidade

Parte 1: Coleta de métricas adicionais em topologia estática
  - Contagem DIO/DAO (overhead de controle) via tcpdump
  - Tipo de encaminhamento (SRH vs hop-by-hop) via traceroute6
  - Latência upward vs downward separada

Parte 2: Cenário de mobilidade do sensor5
  - Fase A: sensor5 conectado a sensor4 (estado inicial)
  - Fase B: sensor5 se afasta de sensor4 (atenuação +6dB)
  - Fase C: handover sensor4->sensor3 (atenuação +10dB)
  - Fase D: sensor5 estabiliza perto de sensor2
  - Fase E: sensor5 sai da rede

Topologia criada UMA vez, reutilizada pra tudo.
Uso: sudo python3 hymrpl_full_experiment.py [--runs 3] [--skip-static] [--skip-mobility]
"""

import time, re, csv, os, sys, statistics, subprocess, signal
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
PING_COUNT = 50

HYBRID_CLASSES = {
    'sensor1': 'S', 'sensor2': 'N', 'sensor3': 'S',
    'sensor4': 'N', 'sensor5': 'N',
}

# ============================================================
# Utility functions
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


def wait_for_convergence(src, dst_addr, max_attempts=180):
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


def measure_pdr_latency(src, dst_addr, count=PING_COUNT):
    result = src.cmd('ping6 -c {} -i 0.2 -W 2 {}'.format(count, dst_addr))
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


# ============================================================
# Métricas extras: DIO/DAO count, traceroute, encaminhamento
# ============================================================

def count_dio_dao(sensor, duration=15):
    """Captura pacotes RPL por 'duration' segundos e conta DIO/DAO."""
    iface = get_iface_name(sensor)
    pcap = '/tmp/rpl_{}.pcap'.format(sensor.name)
    # Inicia captura em background
    sensor.cmd('tcpdump -i {} -w {} icmp6 2>/dev/null &'.format(iface, pcap))
    time.sleep(duration)
    sensor.cmd('killall tcpdump 2>/dev/null')
    time.sleep(1)
    # Conta DIO e DAO nos pacotes capturados
    output = sensor.cmd('tcpdump -r {} -v 2>/dev/null'.format(pcap))
    # RPL DIO = code 0x01, DAO = code 0x02 no ICMPv6 type 155
    dio_count = output.lower().count('dio')
    dao_count = output.lower().count('dao')
    # Fallback: contar por "RPL" genérico
    if dio_count == 0 and dao_count == 0:
        rpl_lines = [l for l in output.split('\n') if 'RPL' in l or 'rpl' in l]
        dio_count = len([l for l in rpl_lines if 'DIS' not in l.upper()])
        dao_count = 0
    sensor.cmd('rm -f {}'.format(pcap))
    return dio_count, dao_count


def check_encapsulation(src, dst_addr):
    """Verifica tipo de encaminhamento via traceroute6."""
    output = src.cmd('traceroute6 -n -w 2 -q 1 {} 2>/dev/null'.format(dst_addr))
    hops = []
    for line in output.strip().split('\n')[1:]:  # skip header
        if '*' in line:
            hops.append('*')
        else:
            m = re.search(r'([\da-f:]+)', line)
            if m:
                hops.append(m.group(1))
    has_srh = '*' in hops or 'encap rpl' in src.cmd('ip -6 route show | grep {}'.format(dst_addr))
    return "SRH" if has_srh else "hop-by-hop", hops


# ============================================================
# Parte 1: Estática com métricas extras
# ============================================================

def run_static_extended(sensors, mode, run_id, runs_total):
    """Run estático com métricas extras: DIO/DAO, traceroute, encaminhamento."""
    info("=== {} | Run {}/{} (extended) ===\n".format(mode.upper(), run_id, runs_total))
    results = {"mode": mode, "run": run_id, "experiment": "static"}

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    start_time = time.time()
    start_rpld(sensors, mode)

    info("  Waiting for sensor5 global address...\n")
    target_addr = wait_for_global_addr(sensors[4])
    if not target_addr:
        info("  FAIL: sensor5 never got a global address!\n")
        results["convergence_s"] = -1
        return results

    conv_elapsed = wait_for_convergence(sensors[0], target_addr)
    if conv_elapsed < 0:
        info("  FAIL: DODAG did not converge\n")
        results["convergence_s"] = -1
        return results

    results["convergence_s"] = round(time.time() - start_time, 2)
    info("  Convergence: {}s\n".format(results["convergence_s"]))

    info("  Stabilizing (10s)...\n")
    time.sleep(10)

    # Endereços
    addrs = {}
    for s in sensors:
        addrs[s.name] = get_global_addr(s)

    # PDR/Latência por par
    test_pairs = [
        (1, 2, "1hop"), (1, 3, "1hop"),
        (1, 4, "2hop"), (1, 5, "3hop"),
        (5, 1, "3hop_up"),
    ]
    for src_idx, dst_idx, desc in test_pairs:
        src = sensors[src_idx - 1]
        dst_addr = addrs.get(sensors[dst_idx - 1].name)
        if not dst_addr:
            continue
        info("  {} -> sensor{} ({})...\n".format(src.name, dst_idx, desc))
        m = measure_pdr_latency(src, dst_addr)
        key = "{}to{}".format(src_idx, dst_idx)
        for k, v in m.items():
            results["{}_{}".format(key, k)] = round(v, 3)

    # Rotas
    routes = count_routes(sensors[0])
    results.update(routes)

    # CPU/Mem
    for s in sensors:
        cpu, mem = measure_cpu_mem(s)
        results["{}_cpu".format(s.name)] = cpu
        results["{}_mem_mb".format(s.name)] = round(mem, 2)

    # DIO/DAO count no root (15s de captura)
    info("  Counting DIO/DAO messages (15s)...\n")
    dio, dao = count_dio_dao(sensors[0], duration=15)
    results["root_dio_count"] = dio
    results["root_dao_count"] = dao

    # Tipo de encaminhamento (traceroute root -> sensor5)
    if addrs.get('sensor5'):
        info("  Checking encapsulation type...\n")
        encap, hops = check_encapsulation(sensors[0], addrs['sensor5'])
        results["encap_type"] = encap
        results["traceroute_hops"] = len(hops)

    return results


# ============================================================
# Parte 2: Mobilidade
# ============================================================

def run_mobility_experiment(sensors, mode, run_id):
    """
    Cenário de mobilidade do sensor5 conforme planejamento da dissertação.
    Simula mobilidade alterando atenuação via wmediumd_cli.

    Fases:
      A: sensor5 conectado a sensor4 (baseline)
      B: sensor5 se afasta (+6dB no enlace sensor4-sensor5)
      C: handover sensor4->sensor3 (+10dB sensor4, 0dB sensor3-sensor5)
      D: sensor5 estabiliza perto de sensor2
      E: sensor5 sai da rede (atenuação total)
    """
    info("\n=== MOBILITY {} | Run {} ===\n".format(mode.upper(), run_id))
    results = {"mode": mode, "run": run_id, "experiment": "mobility"}

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    start_rpld(sensors, mode)

    # Espera convergência inicial
    info("  Waiting for initial convergence...\n")
    target_addr = wait_for_global_addr(sensors[4])
    if not target_addr:
        info("  FAIL: sensor5 never got address\n")
        results["phase_A_convergence"] = -1
        return results

    conv = wait_for_convergence(sensors[0], target_addr)
    if conv < 0:
        info("  FAIL: initial convergence failed\n")
        results["phase_A_convergence"] = -1
        return results

    results["phase_A_convergence"] = round(conv, 2)
    info("  Initial convergence: {}s\n".format(results["phase_A_convergence"]))
    time.sleep(5)

    addrs = {}
    for s in sensors:
        addrs[s.name] = get_global_addr(s)

    # --- Fase A: Baseline (sensor5 conectado a sensor4) ---
    info("  --- Phase A: Baseline ---\n")
    if addrs.get('sensor5'):
        m = measure_pdr_latency(sensors[0], addrs['sensor5'], count=30)
        results["A_pdr"] = round(m["pdr"], 1)
        results["A_lat_avg"] = round(m["lat_avg"], 3)
        results["A_lat_p95"] = round(m["lat_p95"], 3)
        info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # --- Fase B: sensor5 se afasta (+6dB) ---
    info("  --- Phase B: Attenuation +6dB ---\n")
    # wmediumd_cli altera SNR entre nós
    subprocess.run('wmediumd_cli set_snr sensor4 sensor5 6 2>/dev/null', shell=True)
    time.sleep(15)

    # Mede reconvergência
    reconv_start = time.time()
    if addrs.get('sensor5'):
        m = measure_pdr_latency(sensors[0], addrs['sensor5'], count=30)
        results["B_pdr"] = round(m["pdr"], 1)
        results["B_lat_avg"] = round(m["lat_avg"], 3)
        results["B_lat_p95"] = round(m["lat_p95"], 3)
        results["B_reconv_s"] = round(time.time() - reconv_start, 2)
        info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # --- Fase C: Handover sensor4->sensor3 (+10dB sensor4, 0dB sensor3) ---
    info("  --- Phase C: Handover to sensor3 ---\n")
    subprocess.run('wmediumd_cli set_snr sensor4 sensor5 10 2>/dev/null', shell=True)
    subprocess.run('wmediumd_cli set_snr sensor3 sensor5 0 2>/dev/null', shell=True)
    reconv_start = time.time()
    time.sleep(20)

    # Verifica se sensor5 ainda é alcançável (pode ter novo endereço)
    new_addr = get_global_addr(sensors[4])
    if new_addr:
        addrs['sensor5'] = new_addr
    if addrs.get('sensor5'):
        # Tenta reconvergência
        reconv = wait_for_convergence(sensors[0], addrs['sensor5'], max_attempts=60)
        results["C_reconv_s"] = round(reconv, 2) if reconv > 0 else -1
        m = measure_pdr_latency(sensors[0], addrs['sensor5'], count=30)
        results["C_pdr"] = round(m["pdr"], 1)
        results["C_lat_avg"] = round(m["lat_avg"], 3)
        results["C_lat_p95"] = round(m["lat_p95"], 3)
        info("    PDR={:.1f}% lat={:.3f}ms reconv={}\n".format(
            m["pdr"], m["lat_avg"], results.get("C_reconv_s", "N/A")))

    # --- Fase D: sensor5 estabiliza perto de sensor2 ---
    info("  --- Phase D: Near sensor2 ---\n")
    subprocess.run('wmediumd_cli set_snr sensor4 sensor5 20 2>/dev/null', shell=True)
    subprocess.run('wmediumd_cli set_snr sensor3 sensor5 15 2>/dev/null', shell=True)
    subprocess.run('wmediumd_cli set_snr sensor2 sensor5 0 2>/dev/null', shell=True)
    time.sleep(20)

    new_addr = get_global_addr(sensors[4])
    if new_addr:
        addrs['sensor5'] = new_addr
    if addrs.get('sensor5'):
        reconv = wait_for_convergence(sensors[0], addrs['sensor5'], max_attempts=60)
        results["D_reconv_s"] = round(reconv, 2) if reconv > 0 else -1
        m = measure_pdr_latency(sensors[0], addrs['sensor5'], count=30)
        results["D_pdr"] = round(m["pdr"], 1)
        results["D_lat_avg"] = round(m["lat_avg"], 3)
        results["D_lat_p95"] = round(m["lat_p95"], 3)
        info("    PDR={:.1f}% lat={:.3f}ms\n".format(m["pdr"], m["lat_avg"]))

    # Tráfego local sensor4->sensor5 (caminho storing se ambos Classe S)
    if addrs.get('sensor5'):
        info("  --- Local traffic sensor4->sensor5 ---\n")
        m_local = measure_pdr_latency(sensors[3], addrs['sensor5'], count=30)
        results["D_local_pdr"] = round(m_local["pdr"], 1)
        results["D_local_lat_avg"] = round(m_local["lat_avg"], 3)

    # --- Fase E: sensor5 sai da rede ---
    info("  --- Phase E: sensor5 leaves ---\n")
    subprocess.run('wmediumd_cli set_snr sensor2 sensor5 30 2>/dev/null', shell=True)
    subprocess.run('wmediumd_cli set_snr sensor3 sensor5 30 2>/dev/null', shell=True)
    subprocess.run('wmediumd_cli set_snr sensor4 sensor5 30 2>/dev/null', shell=True)
    time.sleep(10)

    if addrs.get('sensor5'):
        m = measure_pdr_latency(sensors[0], addrs['sensor5'], count=10)
        results["E_pdr"] = round(m["pdr"], 1)
        info("    PDR after leave: {:.1f}%\n".format(m["pdr"]))

    # Reset atenuações pra próxima run
    for pair in [('sensor4','sensor5'), ('sensor3','sensor5'), ('sensor2','sensor5')]:
        subprocess.run('wmediumd_cli set_snr {} {} 0 2>/dev/null'.format(*pair), shell=True)
    time.sleep(3)

    return results


# ============================================================
# CSV + Summary
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
    static = [r for r in all_results if r.get("experiment") == "static"]
    mobility = [r for r in all_results if r.get("experiment") == "mobility"]

    print("\n" + "=" * 60)
    print("STATIC RESULTS")
    print("=" * 60)

    modes = {}
    for r in static:
        modes.setdefault(r["mode"], []).append(r)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        ok = [r for r in rows if r.get("convergence_s", -1) != -1]
        print("\n--- {} ({} ok) ---".format(mode.upper(), len(ok)))

        def stat(key):
            vals = [r.get(key, 0) for r in ok if r.get(key, 0) not in (0, -1, None)]
            if not vals:
                return "N/A"
            avg = statistics.mean(vals)
            std = statistics.stdev(vals) if len(vals) > 1 else 0
            return "{:.2f} +/- {:.2f}".format(avg, std)

        print("  Convergence:   {}s".format(stat("convergence_s")))
        print("  PDR 3-hop:     {}%".format(stat("1to5_pdr")))
        print("  Lat 3-hop:     {}ms".format(stat("1to5_lat_avg")))
        print("  DIO (root):    {}".format(stat("root_dio_count")))
        print("  DAO (root):    {}".format(stat("root_dao_count")))
        encaps = [r.get("encap_type", "") for r in ok]
        if encaps:
            print("  Encap type:    {}".format(encaps[0]))

    if mobility:
        print("\n" + "=" * 60)
        print("MOBILITY RESULTS")
        print("=" * 60)

        modes = {}
        for r in mobility:
            modes.setdefault(r["mode"], []).append(r)

        for mode in ["storing", "nonstoring", "hybrid"]:
            rows = modes.get(mode, [])
            if not rows:
                continue
            print("\n--- {} ({} runs) ---".format(mode.upper(), len(rows)))
            for phase in ['A', 'B', 'C', 'D', 'E']:
                pdrs = [r.get("{}_pdr".format(phase), -1) for r in rows]
                pdrs = [p for p in pdrs if p >= 0]
                lats = [r.get("{}_lat_avg".format(phase), 0) for r in rows]
                lats = [l for l in lats if l > 0]
                reconvs = [r.get("{}_reconv_s".format(phase), -1) for r in rows]
                reconvs = [rc for rc in reconvs if rc >= 0]
                if pdrs:
                    line = "  Phase {}: PDR={:.1f}%".format(phase, statistics.mean(pdrs))
                    if lats:
                        line += " lat={:.3f}ms".format(statistics.mean(lats))
                    if reconvs:
                        line += " reconv={:.2f}s".format(statistics.mean(reconvs))
                    print(line)


# ============================================================
# Main
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--modes', nargs='+', default=['storing', 'nonstoring', 'hybrid'],
                        choices=['storing', 'nonstoring', 'hybrid'])
    parser.add_argument('--skip-static', action='store_true')
    parser.add_argument('--skip-mobility', action='store_true')
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    all_results = []

    info("*** Creating topology\n")
    net, sensors = create_topology()

    # Parte 1: Estática com métricas extras
    if not args.skip_static:
        for mode in args.modes:
            info("\n### STATIC: {} ###\n".format(mode.upper()))
            for run_id in range(1, args.runs + 1):
                try:
                    r = run_static_extended(sensors, mode, run_id, args.runs)
                    all_results.append(r)
                except Exception as e:
                    import traceback
                    info("ERROR: {}\n".format(e))
                    info(traceback.format_exc() + "\n")
                    all_results.append({"mode": mode, "run": run_id,
                                        "experiment": "static", "convergence_s": -1})

    # Parte 2: Mobilidade
    if not args.skip_mobility:
        for mode in args.modes:
            info("\n### MOBILITY: {} ###\n".format(mode.upper()))
            for run_id in range(1, args.runs + 1):
                try:
                    r = run_mobility_experiment(sensors, mode, run_id)
                    all_results.append(r)
                except Exception as e:
                    import traceback
                    info("ERROR: {}\n".format(e))
                    info(traceback.format_exc() + "\n")
                    all_results.append({"mode": mode, "run": run_id,
                                        "experiment": "mobility", "phase_A_convergence": -1})

    # Salva tudo
    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "full_experiment_{}.csv".format(ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)
    print("\nResults: {}".format(csv_path))

    info("\n*** Stopping network...\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
