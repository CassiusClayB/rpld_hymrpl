#!/usr/bin/env python3
"""
HyMRPL — Coleta automatizada de métricas experimentais

Coleta todas as métricas citadas na dissertação:
  - PDR (Packet Delivery Ratio)
  - Latência fim-a-fim (min, avg, max, p50, p95)
  - Tempo de convergência (Tc)
  - CPU e memória por nó
  - Tipo de encaminhamento (SRH vs hop-by-hop) via traceroute6
  - Volume de mensagens DIO/DAO (via tcpdump)

Gera CSVs prontos pra importar no LaTeX (pgfplots/pgfplotstable).

Uso: sudo python3 hymrpl_collect_metrics.py --mode hybrid --runs 10
"""

import subprocess
import time
import re
import csv
import os
import sys
import argparse
from datetime import datetime

RESULTS_DIR = "/tmp/hymrpl_results"
SENSORS = ["sensor1", "sensor2", "sensor3", "sensor4"]
PREFIX = "fd3c:be8a:173f:8e80"


def run_in_ns(node, cmd):
    """Executa comando dentro do namespace do nó"""
    result = subprocess.run(
        f"ip netns exec {node} {cmd}",
        shell=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout, result.stderr


def measure_pdr_latency(src, dst_ip, count=100):
    """
    Mede PDR e latência via ping6.
    Retorna dict com: pdr, lat_min, lat_avg, lat_max, lat_values
    """
    stdout, _ = run_in_ns(src, f"ping6 -c {count} -i 0.2 -W 2 {dst_ip}")

    # Extrair pacotes recebidos
    match = re.search(r'(\d+) packets transmitted, (\d+) received', stdout)
    if not match:
        return {"pdr": 0, "lat_min": 0, "lat_avg": 0, "lat_max": 0, "lat_values": []}

    tx = int(match.group(1))
    rx = int(match.group(2))
    pdr = (rx / tx) * 100.0 if tx > 0 else 0

    # Extrair latências
    lat_match = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', stdout)
    if lat_match:
        lat_min = float(lat_match.group(1))
        lat_avg = float(lat_match.group(2))
        lat_max = float(lat_match.group(3))
    else:
        lat_min = lat_avg = lat_max = 0

    # Extrair valores individuais pra calcular percentis
    lat_values = []
    for line in stdout.split('\n'):
        m = re.search(r'time=([\d.]+)', line)
        if m:
            lat_values.append(float(m.group(1)))

    return {
        "pdr": pdr,
        "lat_min": lat_min,
        "lat_avg": lat_avg,
        "lat_max": lat_max,
        "lat_values": lat_values
    }


def percentile(values, p):
    """Calcula percentil p de uma lista de valores"""
    if not values:
        return 0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_v) else f
    return sorted_v[f] + (k - f) * (sorted_v[c] - sorted_v[f])


def measure_convergence(root, target_ip):
    """
    Mede tempo de convergência: tempo entre iniciar rpld e
    conseguir o primeiro ping6 com sucesso.
    """
    start = time.time()
    for _ in range(60):  # tenta por 60 segundos
        stdout, _ = run_in_ns(root, f"ping6 -c 1 -W 1 {target_ip}")
        if "1 received" in stdout:
            return time.time() - start
        time.sleep(0.5)
    return -1  # falhou


def measure_cpu_mem(node):
    """Coleta CPU e memória do processo rpld no nó"""
    stdout, _ = run_in_ns(node, "ps aux | grep rpld | grep -v grep")
    if not stdout.strip():
        return 0, 0

    parts = stdout.split()
    try:
        cpu = float(parts[2])
        mem_pct = float(parts[3])
        # Memória em MB (aproximado)
        stdout2, _ = run_in_ns(node, "free -m | grep Mem")
        total_mem = int(stdout2.split()[1]) if stdout2 else 512
        mem_mb = total_mem * mem_pct / 100.0
        return cpu, mem_mb
    except (IndexError, ValueError):
        return 0, 0


def check_encapsulation(root, target_ip):
    """
    Verifica tipo de encaminhamento via traceroute6.
    Retorna 'SRH' se houver asteriscos (indicando encapsulamento)
    ou 'hop-by-hop' se todos os saltos respondem.
    """
    stdout, _ = run_in_ns(root, f"traceroute6 -n -w 2 -q 1 {target_ip}")
    if "* * *" in stdout:
        return "SRH"
    return "hop-by-hop"


def count_control_messages(node, duration=10):
    """
    Conta mensagens DIO e DAO capturadas via tcpdump.
    Roda tcpdump por 'duration' segundos.
    """
    # Captura em background
    pcap = f"/tmp/{node}_rpl.pcap"
    run_in_ns(node, f"timeout {duration} tcpdump -i lowpan0 -w {pcap} icmp6 2>/dev/null &")
    time.sleep(duration + 2)

    # Conta DIO (code 0x01) e DAO (code 0x02)
    stdout, _ = run_in_ns(node, f"tcpdump -r {pcap} -v 2>/dev/null | grep -c 'RPL'")
    total = int(stdout.strip()) if stdout.strip().isdigit() else 0

    stdout_dio, _ = run_in_ns(node, f"tcpdump -r {pcap} -v 2>/dev/null | grep -c 'DIO'")
    dio = int(stdout_dio.strip()) if stdout_dio.strip().isdigit() else 0

    stdout_dao, _ = run_in_ns(node, f"tcpdump -r {pcap} -v 2>/dev/null | grep -c 'DAO'")
    dao = int(stdout_dao.strip()) if stdout_dao.strip().isdigit() else 0

    return dio, dao


def run_experiment(mode, run_id):
    """Executa uma rodada completa de coleta"""
    print(f"\n=== Run {run_id} | Mode: {mode} ===")
    results = {"mode": mode, "run": run_id}

    # PDR e latência: root -> sensor4
    target = f"{PREFIX}::4"  # ajustar conforme endereço real
    print(f"  Measuring PDR/latency (sensor1 -> {target})...")
    metrics = measure_pdr_latency("sensor1", target, count=100)
    results["pdr"] = metrics["pdr"]
    results["lat_min"] = metrics["lat_min"]
    results["lat_avg"] = metrics["lat_avg"]
    results["lat_max"] = metrics["lat_max"]
    results["lat_p50"] = percentile(metrics["lat_values"], 50)
    results["lat_p95"] = percentile(metrics["lat_values"], 95)

    # Tipo de encaminhamento
    print("  Checking encapsulation type...")
    results["encap"] = check_encapsulation("sensor1", target)

    # CPU e memória por nó
    for node in SENSORS:
        cpu, mem = measure_cpu_mem(node)
        results[f"{node}_cpu"] = cpu
        results[f"{node}_mem"] = mem

    # Volume de controle (no root)
    print("  Counting control messages (10s capture)...")
    dio, dao = count_control_messages("sensor1", duration=10)
    results["dio_count"] = dio
    results["dao_count"] = dao

    return results


def main():
    parser = argparse.ArgumentParser(description='HyMRPL metrics collector')
    parser.add_argument('--mode', required=True,
                        choices=['storing', 'nonstoring', 'hybrid'],
                        help='RPL mode being tested')
    parser.add_argument('--runs', type=int, default=10,
                        help='Number of experiment runs (default: 10)')
    parser.add_argument('--target', type=str,
                        default=f"{PREFIX}::4",
                        help='Target IPv6 address')
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_file = f"{RESULTS_DIR}/{args.mode}_{timestamp}.csv"

    all_results = []
    for i in range(1, args.runs + 1):
        r = run_experiment(args.mode, i)
        all_results.append(r)
        time.sleep(5)  # intervalo entre runs

    # Salvar CSV
    if all_results:
        keys = all_results[0].keys()
        with open(csv_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(all_results)

    # Calcular médias e desvio padrão
    import statistics
    pdrs = [r["pdr"] for r in all_results]
    lats = [r["lat_avg"] for r in all_results]

    print(f"\n{'='*50}")
    print(f"Mode: {args.mode} | Runs: {args.runs}")
    print(f"PDR:     {statistics.mean(pdrs):.1f}% ± {statistics.stdev(pdrs):.1f}%")
    print(f"Latency: {statistics.mean(lats):.2f}ms ± {statistics.stdev(lats):.2f}ms")
    print(f"Results saved to: {csv_file}")
    print(f"{'='*50}")


if __name__ == '__main__':
    main()
