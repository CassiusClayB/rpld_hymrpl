#!/usr/bin/env python3
"""
HyMRPL — Gera tabelas e dados para pgfplots a partir dos CSVs coletados.

Uso: python3 hymrpl_gen_latex.py /tmp/hymrpl_results/

Gera:
  - tabela_comparativa.tex (tabela LaTeX pronta)
  - pdr_data.csv e latency_data.csv (para pgfplots)
"""

import csv
import os
import sys
import statistics
import glob


def load_results(directory):
    """Carrega todos os CSVs do diretório, agrupados por modo"""
    modes = {}
    for f in glob.glob(os.path.join(directory, "*.csv")):
        basename = os.path.basename(f)
        mode = basename.split("_")[0]  # storing, nonstoring, hybrid
        if mode not in modes:
            modes[mode] = []
        with open(f, 'r') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                modes[mode].append(row)
    return modes


def compute_stats(rows, field):
    """Calcula média e desvio padrão de um campo"""
    values = [float(r[field]) for r in rows if field in r]
    if not values:
        return 0, 0
    avg = statistics.mean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0
    return avg, std


def gen_latex_table(modes, output_dir):
    """Gera tabela comparativa em LaTeX"""
    tex = r"""\begin{table}[H]
\centering
\footnotesize
\caption{Comparativo entre Storing, Non-Storing e HyMRPL (MOP~=~6) -- %d execuções}
\label{tab:comparativo_hymrpl}
\renewcommand{\arraystretch}{1.2}
\begin{tabular}{lcccccc}
\hline
\textbf{Modo} &
\textbf{PDR (\%%)} &
\textbf{Latência (ms)} &
\textbf{Lat. p95 (ms)} &
\textbf{CPU (\%%)} &
\textbf{Mem (MB)} &
\textbf{DIO/DAO} \\
\hline
"""
    total_runs = 0
    for mode_name, display_name in [("storing", "Storing"),
                                      ("nonstoring", "Non-Storing"),
                                      ("hybrid", "HyMRPL")]:
        rows = modes.get(mode_name, [])
        if not rows:
            continue
        total_runs = max(total_runs, len(rows))

        pdr_avg, pdr_std = compute_stats(rows, "pdr")
        lat_avg, lat_std = compute_stats(rows, "lat_avg")
        p95_avg, _ = compute_stats(rows, "lat_p95")
        cpu_avg, _ = compute_stats(rows, "sensor1_cpu")
        mem_avg, _ = compute_stats(rows, "sensor1_mem")
        dio_avg, _ = compute_stats(rows, "dio_count")
        dao_avg, _ = compute_stats(rows, "dao_count")

        tex += (f"{display_name} & "
                f"${pdr_avg:.1f} \\pm {pdr_std:.1f}$ & "
                f"${lat_avg:.2f} \\pm {lat_std:.2f}$ & "
                f"${p95_avg:.2f}$ & "
                f"${cpu_avg:.1f}$ & "
                f"${mem_avg:.1f}$ & "
                f"${dio_avg:.0f}/{dao_avg:.0f}$ \\\\\n")

    tex += r"""\hline
\end{tabular}
\end{table}
""" % total_runs

    with open(os.path.join(output_dir, "tabela_comparativa.tex"), 'w') as f:
        f.write(tex)


def gen_pgfplots_data(modes, output_dir):
    """Gera CSVs formatados para pgfplots"""
    # PDR por modo
    with open(os.path.join(output_dir, "pdr_data.csv"), 'w') as f:
        f.write("mode,pdr_avg,pdr_std\n")
        for mode_name in ["storing", "nonstoring", "hybrid"]:
            rows = modes.get(mode_name, [])
            if rows:
                avg, std = compute_stats(rows, "pdr")
                f.write(f"{mode_name},{avg:.2f},{std:.2f}\n")

    # Latência por modo
    with open(os.path.join(output_dir, "latency_data.csv"), 'w') as f:
        f.write("mode,lat_avg,lat_std,lat_p50,lat_p95\n")
        for mode_name in ["storing", "nonstoring", "hybrid"]:
            rows = modes.get(mode_name, [])
            if rows:
                avg, std = compute_stats(rows, "lat_avg")
                p50, _ = compute_stats(rows, "lat_p50")
                p95, _ = compute_stats(rows, "lat_p95")
                f.write(f"{mode_name},{avg:.2f},{std:.2f},{p50:.2f},{p95:.2f}\n")


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 hymrpl_gen_latex.py /tmp/hymrpl_results/")
        sys.exit(1)

    directory = sys.argv[1]
    modes = load_results(directory)

    if not modes:
        print(f"Nenhum CSV encontrado em {directory}")
        sys.exit(1)

    print(f"Modos encontrados: {list(modes.keys())}")
    for m, rows in modes.items():
        print(f"  {m}: {len(rows)} execuções")

    gen_latex_table(modes, directory)
    gen_pgfplots_data(modes, directory)
    print(f"\nArquivos gerados em {directory}:")
    print("  - tabela_comparativa.tex")
    print("  - pdr_data.csv")
    print("  - latency_data.csv")


if __name__ == '__main__':
    main()
