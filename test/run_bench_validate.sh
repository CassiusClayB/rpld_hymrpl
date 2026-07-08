#!/bin/bash
# Launcher de validação: roda o benchmark dos 3 modos a partir de um
# diretório SEM "rpld" no caminho (evita o pkill -9 -f rpld do Mininet-WiFi).
killall -9 rpld 2>/dev/null
mn -c >/dev/null 2>&1
sleep 2
nohup python3 /home/wifi/hymrpl_tests/hymrpl_benchmark.py --runs 1 \
    --modes storing nonstoring hybrid \
    > /tmp/bench_validate.log 2>&1 &
echo "PID=$!"
