# HyMRPL — Guia de Execução dos Experimentos

Todos os scripts ficam em `test/` e devem ser executados como **root** na VM com o ambiente configurado (kernel com SRH, Mininet-WiFi com 6LoWPAN, rpld compilado com patches HyMRPL).

Os resultados são salvos em CSV no diretório `/tmp/hymrpl_results/`.

## Pré-requisitos

- VM com kernel 6.11+ e `CONFIG_IPV6_RPL_LWTUNNEL=y`
- Mininet-WiFi com suporte a 6LoWPAN
- rpld compilado com os patches HyMRPL (ver [README.md](README.md))
- Para captura de pacotes: `sudo apt install -y wireshark-cli`

## Cleanup antes de cada experimento

```bash
sudo killall -9 rpld 2>/dev/null
sudo mn -c 2>/dev/null
```

---

## 1. Topologia Interativa (5 nós)

Cria a topologia base e abre a CLI do Mininet-WiFi para interação manual.

```bash
sudo python3 test/hymrpl_topology.py
```

A partir da CLI: `ip -6 route`, `traceroute6`, `tcpdump`, troca de classe via FIFO.

---

## 2. Benchmark Estático (5 nós, 3 modos)

Compara Storing, Non-Storing e HyMRPL. Coleta PDR, latência por hop, convergência, CPU e memória.

```bash
sudo python3 test/hymrpl_benchmark.py --runs 10 --modes storing nonstoring hybrid
```

Alternativa com topologia persistente:

```bash
sudo python3 test/hymrpl_run_mode.py --runs 10 --modes storing nonstoring hybrid
```

---

## 3. Todos os Modos em Sequência (shell)

Execução sequencial dos 3 modos com cleanup automático entre eles:

```bash
sudo bash test/hymrpl_run_all.sh 5    # 5 runs por modo
```

---

## 4. Hybrid Advantage (5 nós, classes mistas + degradação)

Isolamento de falhas entre classes S e N, com 20% de perda no enlace do sensor2:

```bash
sudo python3 test/hymrpl_hybrid_advantage.py --runs 5
```

---

## 5. Troca Dinâmica de Classe via FIFO

Troca de classe em runtime (N→S→N) no sensor5, medindo PDR e latência por fase:

```bash
sudo python3 test/hymrpl_dynamic_switch.py --runs 3
```

---

## 6. Decisão Adaptativa de Classe

Modelo de score composto (PDR, energia, estabilidade do parent) em 6 fases:

```bash
sudo python3 test/hymrpl_adaptive_switch.py --runs 3
```

---

## 7. Mobilidade (degradação, desconexão, reentrada)

Degradação progressiva via `tc netem`, desconexão via `ip link down/up` e reentrada:

```bash
sudo python3 test/hymrpl_mobility_v2.py --runs 3 --modes storing nonstoring hybrid
```

---

## 8. Captura e Análise de Pacotes

Captura via `tcpdump` + análise com `tshark` (DIO, DAO, SRH, MOP):

```bash
sudo apt install -y wireshark-cli
sudo python3 test/hymrpl_pcap_analysis.py --runs 1
```

Pcaps salvos em `/tmp/hymrpl_pcaps/`.

---

## 9. Escalabilidade (10, 15, 20 e 50 nós)

```bash
sudo python3 test/hymrpl_scalability_10.py --runs 3 --modes storing nonstoring hybrid
sudo python3 test/hymrpl_scalability_15.py --runs 3 --modes storing nonstoring hybrid
sudo python3 test/hymrpl_scalability_20.py --runs 3 --modes storing nonstoring hybrid
sudo python3 test/hymrpl_scalability_50.py --runs 3 --modes storing nonstoring hybrid
```

---

## 10. Churn (20 nós, entrada/saída simultânea)

8 fases de complexidade crescente (folhas, intermediários, nós 1-hop):

```bash
sudo python3 test/hymrpl_churn_mobility.py --runs 3 --modes storing nonstoring hybrid
```

---

## 11. Resiliência em Topologia Mesh (15 nós)

Topologia mesh, mobilidade via `tc netem`, churn e reconvergência em 12 fases:

```bash
sudo python3 test/hymrpl_mesh_resilience.py --runs 3 --modes storing nonstoring hybrid
```

---

## 12. Experimento Completo (estático + mobilidade)

Métricas extras (DIO/DAO via tcpdump, traceroute6, latência upward/downward) + mobilidade:

```bash
sudo python3 test/hymrpl_full_experiment.py --runs 3
sudo python3 test/hymrpl_full_experiment.py --runs 3 --skip-static
sudo python3 test/hymrpl_full_experiment.py --runs 3 --skip-mobility
```

---

## 13. Coleta Automatizada de Métricas

Gera CSVs prontos para LaTeX (pgfplots):

```bash
sudo python3 test/hymrpl_collect_metrics.py --mode hybrid --runs 10
sudo python3 test/hymrpl_collect_metrics.py --mode storing --runs 10
sudo python3 test/hymrpl_collect_metrics.py --mode nonstoring --runs 10
```

---

## 14. Scripts de Análise de Resultados

Processam os CSVs e geram estatísticas consolidadas:

```bash
python3 test/analyze_all.py
python3 test/analyze_hybrid_advantage.py
python3 test/analyze_dynamic_switch.py
python3 test/analyze_adaptive_switch.py
```

---

## Resumo

| Experimento | Script | Nós | Runs | Modos | Tempo aprox. |
|---|---|---|---|---|---|
| Topologia interativa | `hymrpl_topology.py` | 5 | — | hybrid | manual |
| Benchmark estático | `hymrpl_benchmark.py` | 5 | 10 | 3 | ~30 min |
| Hybrid Advantage | `hymrpl_hybrid_advantage.py` | 5 | 5 | 3 | ~20 min |
| Troca dinâmica | `hymrpl_dynamic_switch.py` | 5 | 3 | hybrid | ~10 min |
| Decisão adaptativa | `hymrpl_adaptive_switch.py` | 5 | 3 | hybrid | ~15 min |
| Mobilidade | `hymrpl_mobility_v2.py` | 5 | 3 | 3 | ~20 min |
| Captura de pacotes | `hymrpl_pcap_analysis.py` | 5 | 1 | 3 | ~10 min |
| Escalabilidade 10 | `hymrpl_scalability_10.py` | 10 | 3 | 3 | ~20 min |
| Escalabilidade 15 | `hymrpl_scalability_15.py` | 15 | 3 | 3 | ~30 min |
| Escalabilidade 20 | `hymrpl_scalability_20.py` | 20 | 3 | 3 | ~40 min |
| Escalabilidade 50 | `hymrpl_scalability_50.py` | 50 | 3 | 3 | ~90 min |
| Churn | `hymrpl_churn_mobility.py` | 20 | 3 | 3 | ~45 min |
| Mesh resilience | `hymrpl_mesh_resilience.py` | 15 | 3 | 3 | ~40 min |
| Experimento completo | `hymrpl_full_experiment.py` | 5 | 3 | 3 | ~25 min |
