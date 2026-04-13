#!/usr/bin/env python3
"""Análise do experimento hybrid_advantage."""
import csv, statistics

def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))

def stats(rows, key):
    vals = [float(r[key]) for r in rows if r.get(key) and r[key] not in ('', 'N/A', '-1')]
    if not vals:
        return None, None
    return statistics.mean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0)

def fmt(avg, std):
    if avg is None: return "N/A"
    return "{:.3f} ± {:.3f}".format(avg, std)

rows = load('rpld_hymrpl/test/hybrid_advantage_20260401_075458.csv')
modes = {}
for r in rows:
    modes.setdefault(r['mode'], []).append(r)

print("=" * 72)
print("ANÁLISE — HYBRID ADVANTAGE (5 runs por modo)")
print("=" * 72)

for mode in ['storing', 'nonstoring', 'hybrid']:
    mr = modes.get(mode, [])
    if not mr: continue
    print("\n--- {} ({} runs) ---".format(mode.upper(), len(mr)))
    print("  Convergência:          {}s".format(fmt(*stats(mr, 'convergence_s'))))
    print()
    print("  [Baseline]")
    print("  root→s2 (1-hop):       lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 'root_to_s2_lat')), fmt(*stats(mr, 'root_to_s2_pdr'))))
    print("  root→s3 (1-hop):       lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 'root_to_s3_lat')), fmt(*stats(mr, 'root_to_s3_pdr'))))
    print("  root→s4 (2-hop):       lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 'root_to_s4_lat')), fmt(*stats(mr, 'root_to_s4_pdr'))))
    print("  root→s5 (3-hop):       lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 'root_to_s5_lat')), fmt(*stats(mr, 'root_to_s5_pdr'))))
    print("  s5→root (upward):      lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 's5_to_root_lat')), fmt(*stats(mr, 's5_to_root_pdr'))))
    print("  root→s5 P95:           {} ms".format(fmt(*stats(mr, 'root_to_s5_p95'))))
    print()
    print("  [Tráfego Local]")
    print("  s4→s5 (1-hop local):   lat={} ms  P95={} ms  PDR={}%".format(
        fmt(*stats(mr, 'local_s4s5_lat')),
        fmt(*stats(mr, 'local_s4s5_p95')),
        fmt(*stats(mr, 'local_s4s5_pdr'))))
    print("  s3→s4 (1-hop local):   lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 'local_s3s4_lat')), fmt(*stats(mr, 'local_s3s4_pdr'))))
    print()
    print("  [Degradação 20% loss em sensor2]")
    print("  root→s2 (degradado):   lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 'degraded_s2_lat')), fmt(*stats(mr, 'degraded_s2_pdr'))))
    print("  root→s4 (S-path):      lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 'degraded_s4_lat')), fmt(*stats(mr, 'degraded_s4_pdr'))))
    print("  root→s5 (mixed):       lat={} ms  PDR={}%".format(
        fmt(*stats(mr, 'degraded_s5_lat')), fmt(*stats(mr, 'degraded_s5_pdr'))))
    print()
    print("  [Rotas]")
    print("  root SRH: {}   root via: {}   s4 via: {}".format(
        fmt(*stats(mr, 'root_srh')), fmt(*stats(mr, 'root_via')), fmt(*stats(mr, 's4_via'))))

# --- Tabela comparativa ---
print("\n" + "=" * 72)
print("TABELA COMPARATIVA")
print("=" * 72)
header = "{:<20} {:>14} {:>14} {:>14}".format("Métrica", "STORING", "NONSTORING", "HYBRID")
print(header)
print("-" * 72)

metrics = [
    ("Convergência (s)",     'convergence_s'),
    ("root→s5 lat (ms)",     'root_to_s5_lat'),
    ("root→s5 P95 (ms)",     'root_to_s5_p95'),
    ("root→s5 PDR (%)",      'root_to_s5_pdr'),
    ("s5→root lat (ms)",     's5_to_root_lat'),
    ("LOCAL s4→s5 lat (ms)", 'local_s4s5_lat'),
    ("LOCAL s4→s5 P95 (ms)", 'local_s4s5_p95'),
    ("LOCAL s3→s4 lat (ms)", 'local_s3s4_lat'),
    ("Degraded s2 PDR (%)",  'degraded_s2_pdr'),
    ("Degraded s4 PDR (%)",  'degraded_s4_pdr'),
    ("Degraded s5 PDR (%)",  'degraded_s5_pdr'),
]

for label, key in metrics:
    vals = []
    for mode in ['storing', 'nonstoring', 'hybrid']:
        mr = modes.get(mode, [])
        a, s = stats(mr, key)
        vals.append("{:.3f}±{:.3f}".format(a, s) if a is not None else "N/A")
    print("{:<20} {:>14} {:>14} {:>14}".format(label, *vals))

# --- Análise qualitativa ---
print("\n" + "=" * 72)
print("ANÁLISE")
print("=" * 72)

s_local, _ = stats(modes['storing'], 'local_s4s5_lat')
n_local, _ = stats(modes['nonstoring'], 'local_s4s5_lat')
h_local, _ = stats(modes['hybrid'], 'local_s4s5_lat')

s_conv, _ = stats(modes['storing'], 'convergence_s')
n_conv, _ = stats(modes['nonstoring'], 'convergence_s')
h_conv, _ = stats(modes['hybrid'], 'convergence_s')

s_deg, _ = stats(modes['storing'], 'degraded_s2_pdr')
n_deg, _ = stats(modes['nonstoring'], 'degraded_s2_pdr')
h_deg, _ = stats(modes['hybrid'], 'degraded_s2_pdr')

print()
print("1. TRÁFEGO LOCAL (s4→s5):")
print("   Storing:    {:.3f} ms".format(s_local))
print("   NonStoring: {:.3f} ms".format(n_local))
print("   Hybrid:     {:.3f} ms".format(h_local))
if n_local and h_local:
    overhead = ((h_local - s_local) / s_local) * 100
    saving = ((n_local - h_local) / n_local) * 100
    print("   → Hybrid {:.1f}% mais rápido que NonStoring no tráfego local".format(saving))
    print("   → Overhead do Hybrid vs Storing: {:.1f}%".format(overhead))

print()
print("2. CONVERGÊNCIA:")
print("   Storing:    {:.2f}s".format(s_conv))
print("   NonStoring: {:.2f}s".format(n_conv))
print("   Hybrid:     {:.2f}s".format(h_conv))

print()
print("3. ISOLAMENTO DE FALHAS (PDR root→s2 com 20% loss):")
print("   Storing:    {:.1f}%".format(s_deg))
print("   NonStoring: {:.1f}%".format(n_deg))
print("   Hybrid:     {:.1f}%".format(h_deg))

s_s4deg, _ = stats(modes['storing'], 'degraded_s4_pdr')
n_s4deg, _ = stats(modes['nonstoring'], 'degraded_s4_pdr')
h_s4deg, _ = stats(modes['hybrid'], 'degraded_s4_pdr')
print("   root→s4 (S-path, não afetado): S={:.1f}% NS={:.1f}% H={:.1f}%".format(
    s_s4deg, n_s4deg, h_s4deg))
