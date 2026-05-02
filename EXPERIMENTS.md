# HyMRPL — Experiment Execution Guide

All scripts are located in `test/` and must be run as **root** on the VM with the configured environment (kernel with SRH, Mininet-WiFi with 6LoWPAN, rpld compiled with HyMRPL patches).

Results are saved as CSV files in `/tmp/hymrpl_results/`.

## Prerequisites

- VM with kernel 6.11+ and `CONFIG_IPV6_RPL_LWTUNNEL=y`
- Mininet-WiFi with 6LoWPAN support
- rpld compiled with HyMRPL patches (see [README.md](README.md))
- For packet capture: `sudo apt install -y wireshark-cli`

## Cleanup before each experiment

```bash
sudo killall -9 rpld 2>/dev/null
sudo mn -c 2>/dev/null
```

---

## 1. Interactive Topology (5 nodes)

Creates the base topology and opens the Mininet-WiFi CLI for manual interaction.

```bash
sudo python3 test/hymrpl_topology.py
```

From the CLI: `ip -6 route`, `traceroute6`, `tcpdump`, class switching via FIFO.

---

## 2. Static Benchmark (5 nodes, 3 modes)

Compares Storing, Non-Storing, and HyMRPL. Collects PDR, per-hop latency, convergence time, CPU, and memory.

```bash
sudo python3 test/hymrpl_benchmark.py --runs 10 --modes storing nonstoring hybrid
```

Alternative with persistent topology:

```bash
sudo python3 test/hymrpl_run_mode.py --runs 10 --modes storing nonstoring hybrid
```

---

## 3. All Modes in Sequence (shell)

Sequential execution of all 3 modes with automatic cleanup between them:

```bash
sudo bash test/hymrpl_run_all.sh 5    # 5 runs per mode
```

---

## 4. Hybrid Advantage (5 nodes, mixed classes + degradation)

Fault isolation between Class S and Class N, with 20% packet loss on the sensor2 link:

```bash
sudo python3 test/hymrpl_hybrid_advantage.py --runs 5
```

---

## 5. Dynamic Class Switching via FIFO

Runtime class switching (N→S→N) on sensor5, measuring PDR and latency per phase:

```bash
sudo python3 test/hymrpl_dynamic_switch.py --runs 3
```

---

## 6. Adaptive Class Decision

Composite score model (PDR, energy, parent stability) across 6 phases:

```bash
sudo python3 test/hymrpl_adaptive_switch.py --runs 3
```

---

## 7. Mobility (degradation, disconnection, rejoin)

Progressive degradation via `tc netem`, disconnection via `ip link down/up`, and rejoin:

```bash
sudo python3 test/hymrpl_mobility_v2.py --runs 3 --modes storing nonstoring hybrid
```

---

## 8. Packet Capture and Analysis

Capture via `tcpdump` + analysis with `tshark` (DIO, DAO, SRH, MOP):

```bash
sudo apt install -y wireshark-cli
sudo python3 test/hymrpl_pcap_analysis.py --runs 1
```

Pcaps are saved to `/tmp/hymrpl_pcaps/`.

---

## 9. Scalability (10, 15, 20, and 50 nodes)

```bash
sudo python3 test/hymrpl_scalability_10.py --runs 3 --modes storing nonstoring hybrid
sudo python3 test/hymrpl_scalability_15.py --runs 3 --modes storing nonstoring hybrid
sudo python3 test/hymrpl_scalability_20.py --runs 3 --modes storing nonstoring hybrid
sudo python3 test/hymrpl_scalability_50.py --runs 3 --modes storing nonstoring hybrid
```

---

## 10. Churn (20 nodes, simultaneous join/leave)

8 phases of increasing complexity (leaves, intermediaries, 1-hop nodes):

```bash
sudo python3 test/hymrpl_churn_mobility.py --runs 3 --modes storing nonstoring hybrid
```

---

## 11. Mesh Topology Resilience (15 nodes)

Mesh topology, mobility via `tc netem`, churn, and reconvergence across 12 phases:

```bash
sudo python3 test/hymrpl_mesh_resilience.py --runs 3 --modes storing nonstoring hybrid
```

---

## 12. Full Experiment (static + mobility)

Extra metrics (DIO/DAO via tcpdump, traceroute6, upward/downward latency) + mobility:

```bash
sudo python3 test/hymrpl_full_experiment.py --runs 3
sudo python3 test/hymrpl_full_experiment.py --runs 3 --skip-static
sudo python3 test/hymrpl_full_experiment.py --runs 3 --skip-mobility
```

---

## 13. Automated Metric Collection

Generates CSVs ready for LaTeX (pgfplots):

```bash
sudo python3 test/hymrpl_collect_metrics.py --mode hybrid --runs 10
sudo python3 test/hymrpl_collect_metrics.py --mode storing --runs 10
sudo python3 test/hymrpl_collect_metrics.py --mode nonstoring --runs 10
```

---

## 14. Result Analysis Scripts

Process CSVs and generate consolidated statistics:

```bash
python3 test/analyze_all.py
python3 test/analyze_hybrid_advantage.py
python3 test/analyze_dynamic_switch.py
python3 test/analyze_adaptive_switch.py
```

---

## Summary

| Experiment | Script | Nodes | Runs | Modes | Approx. Time |
|---|---|---|---|---|---|
| Interactive topology | `hymrpl_topology.py` | 5 | — | hybrid | manual |
| Static benchmark | `hymrpl_benchmark.py` | 5 | 10 | 3 | ~30 min |
| Hybrid Advantage | `hymrpl_hybrid_advantage.py` | 5 | 5 | 3 | ~20 min |
| Dynamic switching | `hymrpl_dynamic_switch.py` | 5 | 3 | hybrid | ~10 min |
| Adaptive decision | `hymrpl_adaptive_switch.py` | 5 | 3 | hybrid | ~15 min |
| Mobility | `hymrpl_mobility_v2.py` | 5 | 3 | 3 | ~20 min |
| Packet capture | `hymrpl_pcap_analysis.py` | 5 | 1 | 3 | ~10 min |
| Scalability 10 | `hymrpl_scalability_10.py` | 10 | 3 | 3 | ~20 min |
| Scalability 15 | `hymrpl_scalability_15.py` | 15 | 3 | 3 | ~30 min |
| Scalability 20 | `hymrpl_scalability_20.py` | 20 | 3 | 3 | ~40 min |
| Scalability 50 | `hymrpl_scalability_50.py` | 50 | 3 | 3 | ~90 min |
| Churn | `hymrpl_churn_mobility.py` | 20 | 3 | 3 | ~45 min |
| Mesh resilience | `hymrpl_mesh_resilience.py` | 15 | 3 | 3 | ~40 min |
| Full experiment | `hymrpl_full_experiment.py` | 5 | 3 | 3 | ~25 min |
