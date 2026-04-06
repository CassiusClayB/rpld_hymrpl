#!/usr/bin/env python3
"""
HyMRPL — Monitor de adaptação dinâmica de perfil (Classe S / Classe N)

Coleta métricas locais do nó e decide o perfil funcional:
  - Classe S (storing): nó estável, com recursos disponíveis
  - Classe N (non-storing): nó sobrecarregado, com pouca bateria ou móvel

Métricas usadas:
  - CPU usage (%)
  - Memória disponível (MB)
  - Bateria simulada (%)
  - Índice de mobilidade (via RSSI variance, se disponível)

A decisão é escrita no FIFO /tmp/hymrpl_cmd que o rpld pode ler
para alternar o perfil em runtime.

Uso: sudo python3 hymrpl_monitor.py [--interval 5] [--battery-file /tmp/battery]
"""

import os
import sys
import time
import argparse

# --- Limiares de decisão ---
CPU_THRESHOLD_HIGH = 70.0      # acima disso -> Classe N
CPU_THRESHOLD_LOW = 40.0       # abaixo disso -> Classe S
MEM_THRESHOLD_LOW = 20.0       # MB livres; abaixo -> Classe N
BATTERY_THRESHOLD_LOW = 20.0   # % bateria; abaixo -> Classe N
BATTERY_THRESHOLD_HIGH = 50.0  # acima disso -> pode ser Classe S
HYSTERESIS_CYCLES = 3          # ciclos consecutivos antes de trocar

FIFO_PATH = "/tmp/hymrpl_cmd"
LOG_PATH = "/tmp/hymrpl_monitor.log"


def get_cpu_usage():
    """Retorna uso de CPU (%) via /proc/stat"""
    with open('/proc/stat', 'r') as f:
        line = f.readline()
    parts = line.split()
    idle = int(parts[4])
    total = sum(int(x) for x in parts[1:])
    return idle, total


def get_mem_available_mb():
    """Retorna memória disponível em MB"""
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 1024.0
    return 999.0


def get_battery(battery_file):
    """
    Lê nível de bateria simulado de um arquivo.
    Em ambiente real, leria de /sys/class/power_supply/.
    Retorna 100.0 se o arquivo não existir.
    """
    if battery_file and os.path.exists(battery_file):
        with open(battery_file, 'r') as f:
            try:
                return float(f.read().strip())
            except ValueError:
                return 100.0
    return 100.0


def write_fifo(profile):
    """Escreve comando de troca de perfil no FIFO"""
    try:
        if not os.path.exists(FIFO_PATH):
            os.mkfifo(FIFO_PATH)
        fd = os.open(FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, f"CLASS_{profile}\n".encode())
        os.close(fd)
    except (OSError, BrokenPipeError):
        pass  # rpld não está lendo o FIFO agora, tudo bem


def log_event(timestamp, cpu, mem, battery, current, decision, reason):
    """Registra evento no log"""
    with open(LOG_PATH, 'a') as f:
        f.write(f"{timestamp},{cpu:.1f},{mem:.1f},{battery:.1f},"
                f"{current},{decision},{reason}\n")


def decide_profile(cpu_pct, mem_mb, battery_pct, current_class, counter):
    """
    Decide o perfil funcional baseado nas métricas.
    Retorna (nova_classe, novo_counter, razão)

    Lógica:
    - Se CPU alta OU memória baixa OU bateria baixa -> sugere Classe N
    - Se CPU baixa E memória ok E bateria ok -> sugere Classe S
    - Histerese: só troca após HYSTERESIS_CYCLES consecutivos
    """
    suggest_n = False
    reason = "stable"

    if cpu_pct > CPU_THRESHOLD_HIGH:
        suggest_n = True
        reason = f"cpu_high({cpu_pct:.0f}%)"
    elif mem_mb < MEM_THRESHOLD_LOW:
        suggest_n = True
        reason = f"mem_low({mem_mb:.0f}MB)"
    elif battery_pct < BATTERY_THRESHOLD_LOW:
        suggest_n = True
        reason = f"battery_low({battery_pct:.0f}%)"

    if suggest_n and current_class == "S":
        counter += 1
        if counter >= HYSTERESIS_CYCLES:
            return "N", 0, reason
        return "S", counter, f"pending_N({counter}/{HYSTERESIS_CYCLES})"

    if not suggest_n and current_class == "N":
        # Só volta pra S se tudo estiver confortável
        if (cpu_pct < CPU_THRESHOLD_LOW and
            mem_mb > MEM_THRESHOLD_LOW * 2 and
            battery_pct > BATTERY_THRESHOLD_HIGH):
            counter += 1
            if counter >= HYSTERESIS_CYCLES:
                return "S", 0, "resources_recovered"
            return "N", counter, f"pending_S({counter}/{HYSTERESIS_CYCLES})"

    # Sem mudança
    return current_class, 0 if not suggest_n else counter, reason


def main():
    parser = argparse.ArgumentParser(description='HyMRPL adaptive profile monitor')
    parser.add_argument('--interval', type=int, default=5,
                        help='Monitoring interval in seconds (default: 5)')
    parser.add_argument('--battery-file', type=str, default='/tmp/hymrpl_battery',
                        help='Path to simulated battery level file')
    parser.add_argument('--initial-class', type=str, default='S',
                        choices=['S', 'N'],
                        help='Initial node class (default: S)')
    args = parser.parse_args()

    current_class = args.initial_class
    counter = 0

    # Inicializar log
    with open(LOG_PATH, 'w') as f:
        f.write("timestamp,cpu_pct,mem_mb,battery_pct,current,decision,reason\n")

    print(f"HyMRPL Monitor started (interval={args.interval}s, "
          f"initial_class={current_class})")
    print(f"FIFO: {FIFO_PATH} | Log: {LOG_PATH}")

    # Primeira leitura de CPU (precisa de delta)
    prev_idle, prev_total = get_cpu_usage()
    time.sleep(1)

    while True:
        # Coletar métricas
        idle, total = get_cpu_usage()
        d_idle = idle - prev_idle
        d_total = total - prev_total
        cpu_pct = (1.0 - d_idle / max(d_total, 1)) * 100.0
        prev_idle, prev_total = idle, total

        mem_mb = get_mem_available_mb()
        battery_pct = get_battery(args.battery_file)

        # Decidir
        new_class, counter, reason = decide_profile(
            cpu_pct, mem_mb, battery_pct, current_class, counter)

        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')

        if new_class != current_class:
            print(f"[{timestamp}] SWITCH: {current_class} -> {new_class} ({reason})")
            write_fifo(new_class)
            current_class = new_class
        else:
            print(f"[{timestamp}] class={current_class} cpu={cpu_pct:.0f}% "
                  f"mem={mem_mb:.0f}MB bat={battery_pct:.0f}% ({reason})")

        log_event(timestamp, cpu_pct, mem_mb, battery_pct,
                  current_class, new_class, reason)

        time.sleep(args.interval)


if __name__ == '__main__':
    main()
