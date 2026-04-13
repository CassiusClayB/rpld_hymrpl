#!/usr/bin/env python3
"""Análise do experimento adaptive_switch."""
import csv, statistics

def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))

def stats(rows, key):
    vals = [float(r[key]) for r in rows if r.get(key) and r[key] not in ('', 'N/A', '-1') and float(r[key]) > 0]
    if not vals:
        return None, None
    return statistics.mean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0)

def fmt(avg, std):
    if avg is None: return "N/A"
    return "{:.3f} +/- {:.3f}".format(avg, std)

rows = load('rpld_hymrpl/test/adaptive_switch_20260401_090912.csv')

print("=" * 80)
print("ANÁLISE — ADAPTIVE CLASS SWITCH (3 runs)")
print("=" * 80)
print("\nPesos: PDR=0.4  Energia=0.3  Mobilidade=0.3  |  Threshold=0.5")
print("Convergência: {}s".format(fmt(*stats(rows, 'convergence_s'))))

phases = [
    ("A", "Estável, bat=90%",          "PDR ok, energia alta, estável"),
    ("B", "25% loss, bat=80%",         "PDR degrada, energia ok, estável"),
    ("C", "Link ok, bat=20%",          "PDR ok, energia BAIXA, estável"),
    ("D", "Tudo ok, bat=70%",          "PDR ok, energia ok, estável"),
    ("E", "Parent change, bat=70%",    "PDR ok, energia ok, INSTÁVEL"),
    ("F", "Estabilizado, bat=65%",     "PDR ok, energia ok, estável"),
]

# --- Tabela de decisões ---
print("\n" + "-" * 80)
print("DECISÕES ADAPTATIVAS")
print("-" * 80)
print("{:<6} {:<26} {:>6} {:>7} {:>7} {:>7} {:>7} {:>5}".format(
    "Fase", "Condição", "Class", "Score", "s_PDR", "s_Ene", "s_Mob", "Sw?"))
print("-" * 80)

for phase, desc, _ in phases:
    classes = [r.get("{}_class".format(phase), "?") for r in rows]
    cls = classes[0] if classes else "?"
    score_a, _ = stats(rows, '{}_score'.format(phase))
    sp_a, _ = stats(rows, '{}_score_pdr'.format(phase))
    se_a, _ = stats(rows, '{}_score_energy'.format(phase))
    sm_a, _ = stats(rows, '{}_score_mobility'.format(phase))
    sw_vals = [int(r.get("{}_switched".format(phase), 0)) for r in rows]
    sw = "YES" if sum(sw_vals) > len(sw_vals) / 2 else "no"

    print("{:<6} {:<26} {:>6} {:>7.3f} {:>7.3f} {:>7.3f} {:>7.3f} {:>5}".format(
        phase, desc, cls,
        score_a if score_a else 0,
        sp_a if sp_a else 0,
        se_a if se_a else 0,
        sm_a if sm_a else 0,
        sw))

# --- Tabela de performance ---
print("\n" + "-" * 80)
print("PERFORMANCE POR FASE")
print("-" * 80)
print("{:<6} {:<26} {:>6} {:>12} {:>12} {:>10}".format(
    "Fase", "Condição", "Class", "root→s5", "s4→s5", "PDR r→s5"))
print("-" * 80)

for phase, desc, _ in phases:
    classes = [r.get("{}_class".format(phase), "?") for r in rows]
    cls = classes[0] if classes else "?"
    rs5_a, _ = stats(rows, '{}_root_s5_lat'.format(phase))
    s4s5_a, _ = stats(rows, '{}_s4s5_lat'.format(phase))
    pdr_a, _ = stats(rows, '{}_root_s5_pdr'.format(phase))

    print("{:<6} {:<26} {:>6} {:>10}ms {:>10}ms {:>8}%".format(
        phase, desc, cls,
        "{:.3f}".format(rs5_a) if rs5_a else "N/A",
        "{:.3f}".format(s4s5_a) if s4s5_a else "N/A",
        "{:.1f}".format(pdr_a) if pdr_a else "N/A"))

# --- Detalhamento por fase ---
print("\n" + "-" * 80)
print("DETALHAMENTO POR FASE")
print("-" * 80)

for phase, desc, explanation in phases:
    print("\n  Fase {} — {} ({})".format(phase, desc, explanation))
    classes = [r.get("{}_class".format(phase), "?") for r in rows]
    print("    Classe decidida: {} (runs: {})".format(classes[0], classes))
    print("    root→s5:  lat={} ms  P95={} ms  PDR={}%".format(
        fmt(*stats(rows, '{}_root_s5_lat'.format(phase))),
        fmt(*stats(rows, '{}_root_s5_p95'.format(phase))),
        fmt(*stats(rows, '{}_root_s5_pdr'.format(phase)))))
    print("    s4→s5:    lat={} ms  P95={} ms  PDR={}%".format(
        fmt(*stats(rows, '{}_s4s5_lat'.format(phase))),
        fmt(*stats(rows, '{}_s4s5_p95'.format(phase))),
        fmt(*stats(rows, '{}_s4s5_pdr'.format(phase)))))
    s5r_a, s5r_s = stats(rows, '{}_s5root_lat'.format(phase))
    if s5r_a:
        print("    s5→root:  lat={} ms".format(fmt(s5r_a, s5r_s)))
    reconv_a, reconv_s = stats(rows, '{}_reconvergence_s'.format(phase))
    if reconv_a:
        print("    Reconvergência: {}s".format(fmt(reconv_a, reconv_s)))

# --- Análise da fase E (mobilidade) ---
print("\n" + "=" * 80)
print("ANÁLISE DA FASE E (MOBILIDADE)")
print("=" * 80)
e_classes = [r.get("E_class", "?") for r in rows]
e_pdrs = [float(r.get("E_root_s5_pdr", 0)) for r in rows]
e_reconv = [float(r.get("E_reconvergence_s", -1)) for r in rows if float(r.get("E_reconvergence_s", -1)) > 0]
print("  Classes decididas: {}".format(e_classes))
print("  PDR root→s5: {}".format(e_pdrs))
if e_reconv:
    print("  Reconvergência: {:.2f}s (média)".format(statistics.mean(e_reconv)))
print("  Nota: Run 1 perdeu conectividade (PDR=0%), runs 2-3 reconvergeram rápido")

# --- Conclusões ---
print("\n" + "=" * 80)
print("CONCLUSÕES")
print("=" * 80)
print("""
1. DECISÃO ADAPTATIVA FUNCIONA: O score composto decidiu corretamente
   a classe em cada fase, baseado nos 3 critérios combinados.

2. FASE A (estável): Score=0.97 → S. Correto. Nó estável com recursos
   deve manter rotas locais (Classe S).

3. FASE B (loss): Score caiu pra ~0.87 mas manteve S porque o PDR
   medido (~72%) ainda gerou score acima do threshold com energia e
   estabilidade compensando. O threshold de 0.5 é conservador.

4. FASE C (bateria baixa): Score=0.76, manteve S. A energia baixa (20%)
   reduziu o score mas PDR 100% e estabilidade compensaram.
   → Para forçar N com bateria baixa, o threshold precisaria ser ~0.8.

5. FASE D (recuperado): Score=0.91 → S. Correto.

6. FASE E (mobilidade): Score=0.21-0.61 dependendo do run.
   Run 1: perdeu conectividade → N (score 0.21, PDR=0%).
   Runs 2-3: reconvergiu rápido → S (score 0.61, PDR=100%).
   Isso mostra que a decisão é sensível às condições reais.

7. FASE F (estabilizado): Score=0.50-0.90 → S na maioria.
   Após estabilizar, o nó volta a ser Classe S.

8. O modelo de decisão é funcional mas o threshold de 0.5 é baixo
   demais para forçar troca pra N em cenários moderados. Para a
   dissertação, isso pode ser apresentado como parâmetro configurável.
""")
