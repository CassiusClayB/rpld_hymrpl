#!/bin/bash
# Launcher da bateria completa de experimentos, a partir do diretório
# neutro (~/hymrpl_tests, SEM "rpld" no caminho) para não ser morto pelo
# pkill -9 -f rpld do Mininet-WiFi. Roda em background com nohup.
#
# Uso: bash run_all_launcher.sh [runs]
RUNS=${1:-3}
killall -9 rpld 2>/dev/null
mn -c >/dev/null 2>&1
sleep 2
nohup bash /home/wifi/hymrpl_tests/hymrpl_run_all_experiments.sh "$RUNS" \
    > /tmp/hymrpl_run.log 2>&1 &
echo "RUN_ALL_PID=$!"
