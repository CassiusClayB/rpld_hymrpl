#!/usr/bin/env python3
"""
HyMRPL — Script de mobilidade para sensor5
Simula deslocamento alterando atenuação via wmediumd.

Eventos:
  t=0s   : sensor5 conectado a sensor4 (atenuação 0dB)
  t=30s  : sensor5 se afasta de sensor4 (atenuação +6dB)
  t=60s  : sensor5 faz handover para sensor3 (atenuação sensor4 +10dB)
  t=90s  : sensor5 estabiliza perto de sensor2
  t=120s : sensor5 sai da rede (atenuação total)

Uso: sudo python3 hymrpl_mobility.py
(rodar em paralelo com a topologia ativa)
"""

import subprocess
import time
import sys

def set_attenuation(node_a, node_b, atten_db):
    """
    Altera atenuação entre dois nós via wmediumd.
    Requer wmediumd rodando com API socket.
    """
    # wmediumd_cli é a ferramenta para alterar atenuação em runtime
    cmd = f"wmediumd_cli set_snr {node_a} {node_b} {atten_db}"
    print(f"[{time.strftime('%H:%M:%S')}] {cmd}")
    subprocess.run(cmd, shell=True)

def collect_metrics(sensor_name, target_ip):
    """Coleta PDR e latência via ping6"""
    cmd = f"ip netns exec {sensor_name} ping6 -c 10 -i 0.5 {target_ip}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(f"[{sensor_name}] {result.stdout.split(chr(10))[-3] if result.stdout else 'FAIL'}")

def main():
    print("=== HyMRPL Mobility Experiment ===")
    print("Certifique-se de que a topologia está rodando (hymrpl_topology.py)")

    # Fase 1: Estado inicial (sensor5 perto de sensor4)
    print("\n--- Fase 1: sensor5 conectado a sensor4 (0dB) ---")
    time.sleep(5)
    collect_metrics("sensor1", "fd3c:be8a:173f:8e80::5")

    # Fase 2: sensor5 se afasta (+6dB no enlace sensor4-sensor5)
    print("\n--- Fase 2: sensor5 se afastando (+6dB) ---")
    set_attenuation("sensor4", "sensor5", 6)
    time.sleep(15)
    collect_metrics("sensor1", "fd3c:be8a:173f:8e80::5")

    # Fase 3: Handover — sensor5 perde sensor4, conecta em sensor3
    print("\n--- Fase 3: Handover sensor4->sensor3 (+10dB) ---")
    set_attenuation("sensor4", "sensor5", 10)
    set_attenuation("sensor3", "sensor5", 0)
    time.sleep(20)
    collect_metrics("sensor1", "fd3c:be8a:173f:8e80::5")

    # Fase 4: sensor5 sai da rede
    print("\n--- Fase 4: sensor5 sai da rede ---")
    set_attenuation("sensor3", "sensor5", 30)
    set_attenuation("sensor4", "sensor5", 30)
    time.sleep(10)
    collect_metrics("sensor1", "fd3c:be8a:173f:8e80::5")

    print("\n=== Experiment complete ===")

if __name__ == '__main__':
    main()
