#!/bin/bash
# ============================================================
# HyMRPL — Run ALL dissertation experiments sequentially
#
# IMPORTANT: Run with nohup to survive mn -c:
#   sudo nohup bash ~/rpld_hymrpl/test/hymrpl_run_all_experiments.sh 1 > /tmp/hymrpl_run.log 2>&1 &
#   tail -f /tmp/hymrpl_run.log
#
# Or run one test at a time:
#   sudo bash ~/rpld_hymrpl/test/hymrpl_run_all_experiments.sh 1 storing
#
# Usage:
#   sudo bash hymrpl_run_all_experiments.sh [runs] [filter]
#   filter: storing|nonstoring|hybrid|advantage|dynamic|adaptive|integrated|mobility|scalability|churn|mesh|pcap|all
# ============================================================

RUNS=${1:-3}
FILTER=${2:-all}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS="/tmp/hymrpl_results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG="/tmp/hymrpl_experiments_${TIMESTAMP}.log"

ln -sf "$LOG" /tmp/hymrpl_experiments_latest.log
mkdir -p "$RESULTS"

log() { echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; echo "$*"; }

safe_cleanup() {
    killall -9 rpld 2>/dev/null
    rmmod mac802154_hwsim 2>/dev/null
    sleep 3
}

run_one() {
    local name="$1" script="$2" args="$3"
    log "========== $name =========="
    safe_cleanup
    [ ! -f "$SCRIPT_DIR/$script" ] && { log "SKIP: $script not found"; return 1; }
    python3 "$SCRIPT_DIR/$script" $args >> "$LOG" 2>&1
    log "--- $name done (rc=$?) ---"
    sleep 3
}

should_run() { [ "$FILTER" = "all" ] || echo "$1" | grep -qi "$FILTER"; }

log "HyMRPL Experiments | Runs=$RUNS | Filter=$FILTER | $(date)"

should_run "storing"     && run_one "Benchmark Storing"     "hymrpl_benchmark.py"        "--runs $RUNS --modes storing"
should_run "nonstoring"  && run_one "Benchmark NonStoring"  "hymrpl_benchmark.py"        "--runs $RUNS --modes nonstoring"
should_run "hybrid"      && run_one "Benchmark Hybrid"      "hymrpl_benchmark.py"        "--runs $RUNS --modes hybrid"
should_run "advantage"   && run_one "Hybrid Advantage"      "hymrpl_hybrid_advantage.py" "--runs $RUNS"
should_run "dynamic"     && run_one "Dynamic Switch"        "hymrpl_dynamic_switch.py"   "--runs $RUNS"
should_run "adaptive"    && run_one "Adaptive Switch"       "hymrpl_adaptive_switch.py"  "--runs $RUNS"
should_run "integrated"  && run_one "Adaptive+Security"     "hymrpl_test_adaptive_integrated.py" "--runs $RUNS"
should_run "mobility"    && run_one "Mobility v2"           "hymrpl_mobility_v2.py"      "--runs $RUNS"
should_run "scalability" && run_one "Scalability 10"        "hymrpl_scalability_10.py"   "--runs $RUNS"
should_run "scalability" && run_one "Scalability 15"        "hymrpl_scalability_15.py"   "--runs $RUNS"
should_run "scalability" && run_one "Scalability 20"        "hymrpl_scalability_20.py"   "--runs $RUNS"
should_run "churn"       && run_one "Churn 20 nodes"        "hymrpl_churn_mobility.py"   "--runs $RUNS"
should_run "mesh"        && run_one "Mesh Resilience 15"    "hymrpl_mesh_resilience.py"  "--runs $RUNS"
should_run "pcap"        && run_one "Pcap Analysis"         "hymrpl_pcap_analysis.py"    "--runs $RUNS"

log "ALL DONE | $(date) | Results: $(ls $RESULTS/*.csv 2>/dev/null | wc -l) CSVs"
