#!/usr/bin/env python3
"""
HyMRPL — Experimento com captura de pacotes e análise de protocolo.

Roda o HyMRPL nos 3 modos, captura pacotes com tcpdump,
e analisa os pcaps com tshark para extrair:
  - Contagem de DIO, DAO, DAO-ACK (ICMPv6 code 0x9B)
  - Presença de SRH (Routing Header Type 3)
  - Valor do MOP nos DIOs
  - Troca dinâmica de classe com evidência no pcap

Requer: tcpdump, tshark (wireshark-cli)

Uso: sudo python3 hymrpl_pcap_analysis.py [--runs 1]
"""

import time, re, csv, os, subprocess, json
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
PCAP_DIR = "/tmp/hymrpl_pcaps"
FIFO_PATH = "/tmp/hymrpl_cmd"

HYBRID_CLASSES = {
    'sensor1': 'S',
    'sensor2': 'N',
    'sensor3': 'S',
    'sensor4': 'S',
    'sensor5': 'N',
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


def start_capture(sensor, pcap_path):
    """Inicia captura tcpdump no namespace do sensor."""
    iface = get_iface_name(sensor)
    sensor.cmd('tcpdump -i {} -w {} -U icmp6 2>/dev/null &'.format(iface, pcap_path))
    info("  Capture started on {} -> {}\n".format(sensor.name, pcap_path))


def stop_capture(sensor):
    """Para captura tcpdump."""
    sensor.cmd('killall tcpdump 2>/dev/null')
    time.sleep(1)


def analyze_pcap(pcap_path, label=""):
    """
    Analisa pcap com tshark e extrai métricas RPL.
    Retorna dict com contagens e evidências.
    """
    results = {"label": label, "pcap": pcap_path}

    if not os.path.exists(pcap_path):
        info("  WARN: pcap not found: {}\n".format(pcap_path))
        return results

    # 1. Contagem de pacotes ICMPv6 RPL por tipo
    # ICMPv6 type 155 (0x9B) = RPL
    # code 0x00 = DIS, 0x01 = DIO, 0x02 = DAO, 0x03 = DAO-ACK
    for code, name in [(0, 'DIS'), (1, 'DIO'), (2, 'DAO'), (3, 'DAO_ACK')]:
        cmd = ('tshark -r {} -Y "icmpv6.type == 155 && icmpv6.code == {}" '
               '-T fields -e frame.number 2>/dev/null | wc -l').format(pcap_path, code)
        try:
            count = int(subprocess.check_output(cmd, shell=True).strip())
        except (subprocess.CalledProcessError, ValueError):
            count = 0
        results[name] = count

    # 2. Extrair MOP dos DIOs
    # tshark field: icmpv6.rpl.dio.flag (contains MOP in bits 5-3)
    cmd = ('tshark -r {} -Y "icmpv6.code == 1" '
           '-T fields -e icmpv6.rpl.dio.flag 2>/dev/null').format(pcap_path)
    try:
        output = subprocess.check_output(cmd, shell=True).decode().strip()
        if output:
            flags = output.split('\n')
            mops = set()
            for f in flags:
                try:
                    val = int(f, 0)
                    mop = (val >> 3) & 0x7
                    mops.add(mop)
                except ValueError:
                    pass
            results['MOP_values'] = sorted(mops)
        else:
            results['MOP_values'] = []
    except subprocess.CalledProcessError:
        results['MOP_values'] = []

    # 3. Contar pacotes com SRH (Routing Header Type 3 = RPL SRH)
    cmd = ('tshark -r {} -Y "ipv6.routing.type == 3" '
           '-T fields -e frame.number 2>/dev/null | wc -l').format(pcap_path)
    try:
        results['SRH_packets'] = int(subprocess.check_output(cmd, shell=True).strip())
    except (subprocess.CalledProcessError, ValueError):
        results['SRH_packets'] = 0

    # 4. Contar pacotes ICMPv6 Echo (ping) com e sem SRH
    cmd = ('tshark -r {} -Y "icmpv6.type == 128" '
           '-T fields -e frame.number 2>/dev/null | wc -l').format(pcap_path)
    try:
        results['echo_requests'] = int(subprocess.check_output(cmd, shell=True).strip())
    except (subprocess.CalledProcessError, ValueError):
        results['echo_requests'] = 0

    cmd = ('tshark -r {} -Y "icmpv6.type == 128 && ipv6.routing.type == 3" '
           '-T fields -e frame.number 2>/dev/null | wc -l').format(pcap_path)
    try:
        results['echo_with_srh'] = int(subprocess.check_output(cmd, shell=True).strip())
    except (subprocess.CalledProcessError, ValueError):
        results['echo_with_srh'] = 0

    # 5. Extrair SRH segments (endereços no header)
    cmd = ('tshark -r {} -Y "ipv6.routing.type == 3" '
           '-T fields -e ipv6.routing.src_addr -c 5 2>/dev/null').format(pcap_path)
    try:
        output = subprocess.check_output(cmd, shell=True).decode().strip()
        results['SRH_sample'] = output.split('\n')[:3] if output else []
    except subprocess.CalledProcessError:
        results['SRH_sample'] = []

    # 6. Timeline: primeiro DIO, primeiro DAO, primeiro echo reply
    for code, name in [(1, 'first_DIO'), (2, 'first_DAO')]:
        cmd = ('tshark -r {} -Y "icmpv6.type == 155 && icmpv6.code == {}" '
               '-T fields -e frame.time_relative -c 1 2>/dev/null').format(pcap_path, code)
        try:
            output = subprocess.check_output(cmd, shell=True).decode().strip()
            results[name] = float(output) if output else None
        except (subprocess.CalledProcessError, ValueError):
            results[name] = None

    cmd = ('tshark -r {} -Y "icmpv6.type == 129" '
           '-T fields -e frame.time_relative -c 1 2>/dev/null').format(pcap_path)
    try:
        output = subprocess.check_output(cmd, shell=True).decode().strip()
        results['first_echo_reply'] = float(output) if output else None
    except (subprocess.CalledProcessError, ValueError):
        results['first_echo_reply'] = None

    return results


def print_pcap_analysis(results):
    """Imprime análise formatada de um pcap."""
    print("\n  --- {} ---".format(results.get('label', 'Unknown')))
    print("  Mensagens RPL:")
    print("    DIS: {}  DIO: {}  DAO: {}  DAO-ACK: {}".format(
        results.get('DIS', 0), results.get('DIO', 0),
        results.get('DAO', 0), results.get('DAO_ACK', 0)))
    print("  MOP nos DIOs: {}".format(results.get('MOP_values', [])))
    print("  Pacotes com SRH: {}".format(results.get('SRH_packets', 0)))
    print("  Echo requests: {} (com SRH: {})".format(
        results.get('echo_requests', 0), results.get('echo_with_srh', 0)))
    if results.get('SRH_sample'):
        print("  SRH segments (amostra): {}".format(results['SRH_sample'][:2]))
    print("  Timeline:")
    for key in ['first_DIO', 'first_DAO', 'first_echo_reply']:
        val = results.get(key)
        print("    {}: {}s".format(key, "{:.3f}".format(val) if val else "N/A"))


def run_experiment(sensors, mode, run_id):
    """Roda experimento com captura de pacotes."""
    info("\n=== PCAP ANALYSIS | {} | Run {} ===\n".format(mode.upper(), run_id))
    results = {"mode": mode, "run": run_id}

    stop_rpld(sensors)
    clean_state(sensors)
    time.sleep(3)

    # Iniciar capturas em todos os nós
    pcap_files = {}
    for s in sensors:
        pcap_path = os.path.join(PCAP_DIR, "{}_{}_{}.pcap".format(mode, s.name, run_id))
        pcap_files[s.name] = pcap_path
        start_capture(s, pcap_path)
    time.sleep(2)

    # Iniciar rpld
    start_rpld(sensors, mode)

    # Esperar convergência
    info("  Waiting for convergence...\n")
    addr5 = wait_for_global_addr(sensors[4])
    if not addr5:
        info("  FAIL: sensor5 no address\n")
        for s in sensors:
            stop_capture(s)
        return results

    conv = wait_for_convergence(sensors[0], addr5)
    results["convergence_s"] = round(conv, 2) if conv > 0 else -1
    info("  Convergence: {}s\n".format(results["convergence_s"]))
    time.sleep(5)

    # Gerar tráfego de dados (pings)
    info("  Generating data traffic...\n")
    sensors[0].cmd('ping6 -c 20 -i 0.3 {} > /dev/null 2>&1'.format(addr5))
    time.sleep(2)

    # Captura estática igual pra todos os modos (30s total)
    info("  Static capture period (30s)...\n")
    time.sleep(30)

    # Parar capturas
    for s in sensors:
        stop_capture(s)
    time.sleep(2)

    # Analisar pcaps
    info("\n  Analyzing pcaps...\n")
    all_analysis = []
    for s in sensors:
        pcap_path = pcap_files[s.name]
        analysis = analyze_pcap(pcap_path,
                                label="{} {} run{}".format(mode, s.name, run_id))
        all_analysis.append(analysis)
        print_pcap_analysis(analysis)

    # Consolidar métricas do root
    root_analysis = all_analysis[0]
    results["root_DIO"] = root_analysis.get('DIO', 0)
    results["root_DAO"] = root_analysis.get('DAO', 0)
    results["root_SRH"] = root_analysis.get('SRH_packets', 0)
    results["root_MOP"] = str(root_analysis.get('MOP_values', []))
    results["root_echo_with_srh"] = root_analysis.get('echo_with_srh', 0)
    results["root_echo_total"] = root_analysis.get('echo_requests', 0)

    # Total de mensagens RPL na rede
    total_dio = sum(a.get('DIO', 0) for a in all_analysis)
    total_dao = sum(a.get('DAO', 0) for a in all_analysis)
    total_srh = sum(a.get('SRH_packets', 0) for a in all_analysis)
    results["total_DIO"] = total_dio
    results["total_DAO"] = total_dao
    results["total_SRH"] = total_srh

    print("\n  CONSOLIDATED:")
    print("    Total DIO: {}  DAO: {}  SRH packets: {}".format(
        total_dio, total_dao, total_srh))
    print("    MOP in DIOs: {}".format(root_analysis.get('MOP_values', [])))

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
        modes.setdefault(r.get("mode", ""), []).append(r)

    print("\n" + "=" * 70)
    print("PCAP ANALYSIS SUMMARY")
    print("=" * 70)

    for mode in ["storing", "nonstoring", "hybrid"]:
        rows = modes.get(mode, [])
        if not rows:
            continue
        print("\n--- {} ---".format(mode.upper()))
        for r in rows:
            print("  Run {}: DIO={} DAO={} SRH={} MOP={} echo_srh={}/{}".format(
                r.get('run', '?'),
                r.get('total_DIO', 0),
                r.get('total_DAO', 0),
                r.get('total_SRH', 0),
                r.get('root_MOP', '?'),
                r.get('root_echo_with_srh', 0),
                r.get('root_echo_total', 0)))

    print("\nExpected behavior:")
    print("  STORING:    MOP=[2], SRH=0, echo_with_srh=0")
    print("  NONSTORING: MOP=[1], SRH>0, echo_with_srh>0")
    print("  HYBRID:     MOP=[6], SRH>0, echo_with_srh>0")
    print("  All modes:  same DIO/DAO count (no extra messages)")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--modes', nargs='+', default=['storing', 'nonstoring', 'hybrid'],
                        choices=['storing', 'nonstoring', 'hybrid'])
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(PCAP_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    all_results = []

    info("*** Creating topology\n")
    net, sensors = create_topology()

    for mode in args.modes:
        for run_id in range(1, args.runs + 1):
            try:
                r = run_experiment(sensors, mode, run_id)
                all_results.append(r)
            except Exception as e:
                import traceback
                info("ERROR: {}\n".format(e))
                info(traceback.format_exc() + "\n")
                all_results.append({"mode": mode, "run": run_id})

    stop_rpld(sensors)
    csv_path = os.path.join(RESULTS_DIR, "pcap_analysis_{}.csv".format(ts))
    save_csv(all_results, csv_path)
    print_summary(all_results)

    print("\nResults: {}".format(csv_path))
    print("PCAPs: {}".format(PCAP_DIR))

    info("\n*** Stopping network...\n")
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()
