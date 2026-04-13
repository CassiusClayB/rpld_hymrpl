#!/bin/bash
# HyMRPL — Roda os 3 modos em sequência
# Uso: sudo bash hymrpl_run_all.sh [runs]

RUNS=${1:-5}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS="/tmp/hymrpl_results"
mkdir -p "$RESULTS"

echo "=== HyMRPL Benchmark: $RUNS runs per mode ==="

for MODE in storing nonstoring hybrid; do
    echo ""
    echo "##############################"
    echo "# MODE: $MODE"
    echo "##############################"

    # Cleanup antes
    killall -9 rpld 2>/dev/null
    mn -c > /dev/null 2>&1
    sleep 3

    python3 "$SCRIPT_DIR/hymrpl_run_mode.py" --mode "$MODE" --runs "$RUNS"

    echo "# $MODE finished"

    # Cleanup depois
    killall -9 rpld 2>/dev/null
    mn -c > /dev/null 2>&1
    sleep 3
done

echo ""
echo "=== ALL DONE ==="
echo "Results in $RESULTS:"
ls -la "$RESULTS"
