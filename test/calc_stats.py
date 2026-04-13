#!/usr/bin/env python3
"""Calcula estatísticas dos CSVs do benchmark."""
import csv, statistics, sys

def load_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))

def s(rows, key):
    vals = [float(r[key]) for r in rows if r.get(key) and r[key] not in ('', 'N/A', '-1')]
    if not vals:
        return None, None
    avg = statistics.mean(vals)
    std = statistics.stdev(vals) if len(vals) > 1 else 0
    return avg, std

def fmt(avg, std):
    if avg is None:
        return "N/A"
    return "{:.3f} +/- {:.3f}".format(avg, std)

# 10-run benchmark
print("=" * 70)
print("BENCHMARK 10 RUNS")
print("=" * 70)
rows = load_csv('rpld_hymrpl/test/benchmark_20260331_225915.csv')
modes = {}
for r in rows:
    modes.setdefault(r['mode'], []).append(r)

for mode in ['storing', 'nonstoring', 'hybrid']:
    mr = modes.get(mode, [])
    if not mr:
        continue
    print("\n--- {} ({} runs) ---".format(mode.upper(), len(mr)))
    print("  Convergence:    {}s".format(fmt(*s(mr, 'convergence_s'))))
    print("  PDR 1-hop:      {}%".format(fmt(*s(mr, '1to2_pdr'))))
    print("  PDR 2-hop:      {}%".format(fmt(*s(mr, '1to4_pdr'))))
    print("  PDR 3-hop:      {}%".format(fmt(*s(mr, '1to5_pdr'))))
    print("  Lat 1-hop avg:  {}ms".format(fmt(*s(mr, '1to2_lat_avg'))))
    print("  Lat 2-hop avg:  {}ms".format(fmt(*s(mr, '1to4_lat_avg'))))
    print("  Lat 3-hop avg:  {}ms".format(fmt(*s(mr, '1to5_lat_avg'))))
    print("  Lat 3-hop p95:  {}ms".format(fmt(*s(mr, '1to5_lat_p95'))))
    print("  Lat 3-hop up:   {}ms".format(fmt(*s(mr, '5to1_lat_avg'))))
    print("  Routes SRH:     {}".format(fmt(*s(mr, 'routes_srh'))))
    print("  Routes via:     {}".format(fmt(*s(mr, 'routes_via'))))
    print("  CPU root:       {}%".format(fmt(*s(mr, 'sensor1_cpu'))))
    print("  Mem per node:   {}MB".format(fmt(*s(mr, 'sensor1_mem_mb'))))

# Full experiment (static + mobility)
print("\n" + "=" * 70)
print("FULL EXPERIMENT (STATIC + MOBILITY)")
print("=" * 70)
rows2 = load_csv('rpld_hymrpl/test/full_experiment_20260331_233815.csv')
static = [r for r in rows2 if r.get('experiment') == 'static']
mobility = [r for r in rows2 if r.get('experiment') == 'mobility']

print("\n--- STATIC EXTRAS ---")
modes2 = {}
for r in static:
    modes2.setdefault(r['mode'], []).append(r)
for mode in ['storing', 'nonstoring', 'hybrid']:
    mr = modes2.get(mode, [])
    if not mr:
        continue
    print("\n  {} ({} runs):".format(mode.upper(), len(mr)))
    print("    DIO count:    {}".format(fmt(*s(mr, 'root_dio_count'))))
    print("    DAO count:    {}".format(fmt(*s(mr, 'root_dao_count'))))
    encaps = [r.get('encap_type', '') for r in mr if r.get('encap_type')]
    if encaps:
        print("    Encap type:   {}".format(encaps[0]))
    print("    Traceroute:   {} hops".format(fmt(*s(mr, 'traceroute_hops'))))

print("\n--- MOBILITY ---")
modes3 = {}
for r in mobility:
    modes3.setdefault(r['mode'], []).append(r)
for mode in ['storing', 'nonstoring', 'hybrid']:
    mr = modes3.get(mode, [])
    if not mr:
        continue
    print("\n  {} ({} runs):".format(mode.upper(), len(mr)))
    print("    Initial conv: {}s".format(fmt(*s(mr, 'phase_A_convergence'))))
    for phase in ['A', 'B', 'C', 'D', 'E']:
        pdr_a, pdr_s = s(mr, '{}_pdr'.format(phase))
        lat_a, lat_s = s(mr, '{}_lat_avg'.format(phase))
        p95_a, p95_s = s(mr, '{}_lat_p95'.format(phase))
        rc_a, rc_s = s(mr, '{}_reconv_s'.format(phase))
        line = "    Phase {}: ".format(phase)
        if pdr_a is not None:
            line += "PDR={:.1f}% ".format(pdr_a)
        if lat_a is not None:
            line += "lat={:.3f}ms ".format(lat_a)
        if p95_a is not None:
            line += "p95={:.3f}ms ".format(p95_a)
        if rc_a is not None:
            line += "reconv={:.2f}s ".format(rc_a)
        print(line)
    # Local traffic
    dl_a, dl_s = s(mr, 'D_local_lat_avg')
    if dl_a is not None:
        print("    Local s4->s5: lat={:.3f}ms".format(dl_a))
