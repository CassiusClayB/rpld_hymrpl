# HyMRPL — Documentação Técnica Completa

## 1. Visão Geral

O HyMRPL (Hybrid Mode RPL) é uma extensão experimental do protocolo RPL (RFC 6550) que permite a coexistência simultânea de comportamentos Storing (Classe S) e Non-Storing (Classe N) dentro de uma mesma DODAG, utilizando o valor experimental MOP=6.

A implementação é baseada no daemon `rpld` e integra três subsistemas principais:

1. **Roteamento Híbrido** — DAO dual (parent + root), árvore SRH no root, rotas hop-by-hop em nós S
2. **Motor de Decisão Adaptativa** — decisão interna ao daemon baseada em PDR, energia e mobilidade
3. **FIFO Seguro** — interface de controle externo com autenticação HMAC-SHA256

---

## 2. Roteamento Híbrido (MOP=6)

### 2.1 Envio de DAO Dual

No modo híbrido, cada nó envia DAO para **dois destinos**:

```c
case RPL_DIO_HYBRID:
    // DAO para o parent: permite nós Classe S intermediários
    // instalarem rotas downward locais (storing-like)
    send_dao(sock, &dag->parent->addr, dag);

    // DAO para o root: permite o root construir a árvore
    // de source routing completa para caminhos Classe N
    if (dag->parent->rank > 1)
        send_dao(sock, &dag->dodagid, dag);
    break;
```

Isso garante que ambos os paradigmas de roteamento funcionem simultaneamente.

### 2.2 Construção da Árvore de Source Routing (SRH)

O root mantém uma árvore de source routing (`t_node`) que mapeia todos os nós da DODAG. Quando recebe um DAO com múltiplos targets (agregados por nós Classe S), o root processa TODOS:

```c
case RPL_DIO_HYBRID:
    if (dag->my_rank == 1) {
        // Root: processa TODOS os targets do DAO
        for (i = 0; i < target_count; i++) {
            n = t_insert(&dag->root, &transit->parent,
                         &addr->sin6_addr,
                         &targets[i]->rpl_dao_prefix);
            if (n)
                dag_insert_source_routes(dag->iface->ifindex, n);
        }
    }
```

A função `dag_insert_source_routes()` instala rotas SRH via Netlink (`ip -6 route add ... encap rpl segs ...`), permitindo que o root encaminhe pacotes downward usando o Source Routing Header do IPv6.

### 2.3 Instalação de Rotas por Classe

| Papel | Comportamento |
|-------|---------------|
| Root (rank=1) | Sempre constrói árvore SRH para todos os targets |
| Classe S (non-root) | Instala rotas downward via Netlink (`nl_add_route_via`) — hop-by-hop |
| Classe N (non-root) | Não instala rotas locais — delega ao root via SRH |

### 2.4 Agregação de Targets no DAO (Classe S)

Nós Classe S incluem os targets dos seus filhos no DAO que enviam ao parent/root:

```
DAO de sensor5 (N): [target=sensor5]
DAO de sensor4 (S): [target=sensor4, target=sensor5]  ← agrega filho
```

Isso permite que o root construa a árvore completa mesmo quando nós intermediários são Classe S.

### 2.5 Dependência do Kernel: SRH (CONFIG_IPV6_RPL_LWTUNNEL)

O encaminhamento Non-Storing depende do módulo `CONFIG_IPV6_RPL_LWTUNNEL` do kernel Linux, que:
- Processa o RPL Source Routing Header
- Decrementa Segments Left
- Substitui o endereço de destino pelo próximo segmento
- Re-encaminha o pacote

Sem este módulo, `ip -6 route add ... encap rpl ...` falha e o modo Non-Storing não funciona.

### 2.6 Enriquecimento da Árvore SRH em Topologias Mesh

Uma propriedade emergente do design híbrido é o **enriquecimento da árvore SRH** no root. No Non-Storing puro, cada nó envia um DAO contendo apenas seu próprio endereço — o root constrói exatamente 14 rotas SRH (uma por nó). No HyMRPL, os nós Classe S **agregam os endereços dos seus filhos** nos DAOs, fornecendo ao root informação topológica redundante.

#### Mecanismo

Considere sensor2 (Classe S) com filhos sensor5 e sensor6 (Classe N):

- **Non-Storing:** sensor5 → DAO(target=sensor5), sensor6 → DAO(target=sensor6), sensor2 → DAO(target=sensor2). Root recebe 3 DAOs independentes → 3 rotas SRH.

- **HyMRPL:** sensor5 → DAO(target=sensor5), sensor6 → DAO(target=sensor6), sensor2 (S) → DAO(target=sensor2, target=sensor5, target=sensor6). Root recebe informação redundante sobre sensor5/sensor6 por dois caminhos → pode construir rotas SRH alternativas.

#### Resultado Quantitativo

| Métrica | Storing | Non-Storing | HyMRPL |
|---------|---------|-------------|--------|
| Rotas SRH no root | 0 | 14 | 16–21 |
| Rotas hop-by-hop | 14 | 0 | 0 |

O HyMRPL mantém 15–50% mais rotas SRH que o Non-Storing, ampliando as opções de caminhos alternativos sob falha.

#### Impacto na Resiliência

A árvore SRH enriquecida dá ao root **mais opções de caminhos** quando um nó falha:
- Quando sensor5 cai, o root já possui rotas SRH alternativas via sensor6/sensor7 (construídas a partir dos DAOs agregados pelos nós Classe S)
- Quando sensor7 cai após sensor5 ser restaurado, o root já reconstruiu rotas incluindo caminhos via sensor5

Essa propriedade é **emergente** do design híbrido e não foi explorada por propostas anteriores (DualMOP, ARPL, 2-Colorable DODAG).

### 2.7 Mecanismo de Reconvergência Dual (Mesh Resilience)

Em topologias mesh com enlaces redundantes, o HyMRPL combina dois mecanismos de reconvergência que operam simultaneamente:

1. **Reconvergência centralizada (via SRH):** O root mantém a árvore SRH completa e pode redirecionar tráfego por caminhos alternativos imediatamente, sem esperar novos DAOs.

2. **Reconvergência local (via hop-by-hop):** Nós Classe S vizinhos do nó falho mantêm rotas locais para vizinhos diretos, oferecendo failover imediato enquanto a reconvergência global acontece.

#### Resultado Experimental (Fase P4b — kill sensor7 após restore sensor5)

| Modo | PDR | Nós Alcançáveis |
|------|-----|-----------------|
| Storing | 79,5% | 10,3 |
| Non-Storing | 64,1% | 8,3 |
| **HyMRPL** | **87,2%** | **11,3** |

Vantagem de 23,1 pontos percentuais sobre Non-Storing e 7,7 sobre Storing.

#### Por que os modos puros falham

- **Storing:** Rotas hop-by-hop ficam *stale* — pacotes continuam sendo encaminhados para o nó morto até o Trickle timer detectar a inconsistência (Boubekeur et al., ARPL 2019).
- **Non-Storing:** Reconstrução da árvore SRH depende de novos DAOs, que podem ser lentos quando múltiplos nós competem pelo mesmo parent alternativo (Ko et al., DualMOP 2015).
- **HyMRPL:** Root já tem rotas SRH alternativas (visão centralizada) + nós Classe S têm rotas locais (failover imediato). Reconvergência mais rápida que qualquer modo isolado.

#### Recuperação Total

Em todas as fases de restauração (P4a, P5a, P6, P8, P9), os três modos voltaram a 100% de PDR. Isso confirma que o HyMRPL não interfere na reconvergência nativa do RPL (Trickle timer, RFC 6206).

#### Double Churn como Caso Limite

Na fase P7 (remoção simultânea de sensor9 e sensor10), os três modos apresentaram PDR equivalente (~58–64%). A limitação é topológica, não protocolar — quando a remoção particiona a rede, nenhum mecanismo pode entregar pacotes a destinos inalcançáveis. O HyMRPL não introduz vulnerabilidade adicional.

---

## 3. Motor de Decisão Adaptativa (Integrado)

### 3.1 Arquitetura

O motor adaptativo é integrado diretamente no daemon rpld, eliminando a dependência do script externo `hymrpl_monitor.py`. Roda como um timer periódico no event loop do libev.

```
┌─────────────────────────────────────────────────────────┐
│                        rpld (daemon)                      │
│                                                          │
│  ┌──────────────────┐    ┌─────────────────────────┐    │
│  │  Motor Adaptativo │    │     FIFO Seguro          │    │
│  │                    │    │                          │    │
│  │  • PDR (DAO-ACK)  │    │  • HMAC-SHA256 auth     │    │
│  │  • Energia (sysfs) │    │  • Nonce (anti-replay)  │    │
│  │  • Mobilidade      │    │  • Rate limiting        │    │
│  │    (parent change) │    │  • Permissões 0600      │    │
│  │                    │    │  • Audit log            │    │
│  │  Score → Decisão   │    │                          │    │
│  │  com histerese     │    │  /tmp/hymrpl_cmd         │    │
│  └────────┬───────────┘    └──────────┬──────────────┘    │
│           │                           │                   │
│           ▼                           ▼                   │
│  ┌─────────────────────────────────────────────────┐     │
│  │         hymrpl_periodic_cb (ev_timer, 5s)        │     │
│  │                                                   │     │
│  │  1. Verifica FIFO (comando externo autenticado)   │     │
│  │  2. Se não há comando → consulta motor adaptativo │     │
│  │  3. Aplica troca se histerese satisfeita          │     │
│  │  4. Rate limiting compartilhado                   │     │
│  └───────────────────────────────────────────────────┘     │
│                                                            │
│              dag->node_class = new_class                    │
│              (1 byte, zero overhead na rede)                │
└────────────────────────────────────────────────────────────┘
```

### 3.2 Fórmula de Decisão

```
Score = 0.4 × (PDR/100) + 0.3 × (Energia/100) + 0.3 × (parent_estável ? 1 : 0)
```

- Score ≥ 0.75 → Classe S (nó estável, com recursos)
- Score < 0.75 → Classe N (nó instável ou restrito)

### 3.3 Coleta de Métricas

| Métrica | Fonte | Método |
|---------|-------|--------|
| PDR | DAO-ACK success/failure | Janela deslizante de 20 amostras |
| Energia | `/tmp/hymrpl_battery_<ifname>` ou `/sys/class/power_supply/BAT0/capacity` | Leitura periódica per-interface |
| Mobilidade | Detecção de parent change em `process_dio()` | Flag + timestamp, estabiliza após 15s |

### 3.4 Bateria Per-Interface

Cada nó lê seu próprio arquivo de bateria baseado no nome da interface:

```c
void hymrpl_adaptive_init(struct hymrpl_adaptive *adp, const char *ifname,
                           struct ev_loop *loop)
{
    // Per-interface battery path
    if (ifname && ifname[0])
        snprintf(adp->battery_path, sizeof(adp->battery_path),
                 "/tmp/hymrpl_battery_%s", ifname);
    else
        snprintf(adp->battery_path, sizeof(adp->battery_path),
                 "/tmp/hymrpl_battery");
}
```

Exemplos: `/tmp/hymrpl_battery_sensor4-pan0`, `/tmp/hymrpl_battery_sensor5-pan0`

Isso permite simulação independente de energia por nó em ambientes de teste.

### 3.5 Histerese

- 3 ciclos consecutivos (15s) recomendando a mesma mudança antes de aplicar
- Evita oscilação em condições de fronteira (score ~0.75)
- Contador reseta se a recomendação muda de direção

### 3.6 Vantagens sobre o Monitor Externo

| Aspecto | Monitor Externo (Python) | Motor Integrado (C) |
|---------|--------------------------|---------------------|
| Dependência | Python 3 + psutil | Nenhuma |
| Latência de decisão | 5s (intervalo do script) | 5s (timer libev) |
| Medição de PDR | ping externo (impreciso) | DAO-ACK direto (preciso) |
| IPC | FIFO plain text | Interno ao processo |
| Segurança | Nenhuma | HMAC + nonce + rate limit |
| Overhead | Processo separado | Zero (mesmo processo) |

---

## 4. FIFO Seguro

### 4.1 Problema Original

O FIFO antigo (`mkfifo 0666`) permitia que qualquer processo enviasse comandos:
- Processo malicioso força Classe N → nó perde rotas locais
- Oscilação rápida S↔N → instabilidade na DODAG
- Replay de comandos capturados

### 4.2 Solução: 5 Camadas de Segurança

#### Camada 1: Autenticação HMAC-SHA256

```
Formato: CLASS_S|<nonce_hex_16>|<hmac_hex_64>\n
HMAC = SHA256(token, "CLASS_S|<nonce_hex>")
```

- Token de 256 bits gerado com `/dev/urandom`
- Armazenado em `/etc/hymrpl/fifo.token` (modo 0600)

#### Camada 2: Proteção contra Replay (Nonce)

- Nonce = timestamp em microsegundos (monotonicamente crescente)
- Cada comando deve ter nonce > último nonce aceito

#### Camada 3: Rate Limiting

- Mínimo 10 segundos entre trocas
- Máximo 3 trocas por minuto
- Compartilhado entre FIFO externo e motor adaptativo

#### Camada 4: Permissões Restritas

- FIFO criado com modo `0600` (apenas root)

#### Camada 5: Audit Log

- Toda tentativa logada (sucesso/falha, motivo de rejeição)

### 4.3 Utilitário de Comando: hymrpl_cmd

```bash
# Gerar token (uma vez)
sudo hymrpl_cmd --gen-token

# Enviar comando autenticado
sudo hymrpl_cmd CLASS_S
sudo hymrpl_cmd CLASS_N
```

### 4.4 Compatibilidade Legado

Se `/etc/hymrpl/fifo.token` não existir:
- FIFO aceita comandos plain (`CLASS_S\n`)
- Warning é logado
- Rate limiting ainda se aplica

---

## 5. Fluxo de Decisão Completo

```
Cada 5 segundos (hymrpl_periodic_cb):
│
├─ FIFO tem comando?
│   ├─ Token carregado? → Verificar HMAC → Nonce OK? → Rate limit OK? → Aplicar
│   └─ Sem token → Aceitar plain (legacy) + rate limit → Aplicar
│
├─ Motor adaptativo habilitado? (apenas nós não-root)
│   ├─ Ler energia de /tmp/hymrpl_battery_<ifname>
│   ├─ Verificar estabilidade do parent (15s sem mudança)
│   ├─ Calcular score
│   ├─ Score recomenda mudança?
│   │   ├─ Histerese satisfeita (3 ciclos)? → Rate limit OK? → Aplicar
│   │   └─ Não → incrementar contador
│   └─ Sem mudança → resetar contador
│
└─ Fim do ciclo
```

---

## 6. Arquivos do Projeto

| Arquivo | Função |
|---------|--------|
| `rpl.h` | Enum `RPL_DIO_HYBRID=6`, defines `HYMRPL_CLASS_S/N` |
| `dag.h` | Campo `uint8_t node_class` em `struct dag` |
| `config.h` | Campo `uint8_t node_class` em `struct iface` |
| `process.c` | Lógica híbrida: DIO com propagação de classe, DAO dual, instalação de rotas por classe |
| `hymrpl_adaptive.h` | Header do motor adaptativo + FIFO seguro |
| `hymrpl_adaptive.c` | Implementação completa (decisão + segurança) |
| `hymrpl_cmd.c` | Utilitário CLI para comandos autenticados |
| `rpld.c` (rpld_new.c) | Integração: `hymrpl_periodic_cb`, init do adaptativo e FIFO |

---

## 7. Compilação

```bash
# Na VM (192.168.0.101)
cd /home/wifi/rpld
sudo apt install libssl-dev libev-dev liblua5.3-dev libnl-3-dev libnl-genl-3-dev meson ninja-build

# Compilar rpld
meson setup build   # (apenas na primeira vez)
ninja -C build
sudo cp build/rpld /usr/local/bin/rpld

# Compilar hymrpl_cmd
gcc -Wall -o hymrpl_cmd hymrpl_cmd.c -lssl -lcrypto
sudo cp hymrpl_cmd /usr/local/bin/
```

---

## 8. Testes Experimentais

Todos os testes foram atualizados para funcionar com o rpld atual (motor adaptativo + FIFO seguro + bateria per-interface).

### 8.1 Helper Compartilhado: `test/hymrpl_helpers.py`

```python
from hymrpl_helpers import setup_battery_for_topology, send_authenticated_cmd, ensure_token
```

Funções:
- `setup_battery_for_topology(sensors, HYBRID_CLASSES)` — seta bateria per-interface antes de iniciar rpld
- `send_authenticated_cmd(sensor, "CLASS_N")` — envia via `hymrpl_cmd`
- `set_battery_level(sensor, 10)` — seta nível específico

### 8.2 Execução

```bash
sudo python3 -c "
import subprocess
tests = [
    'hymrpl_dynamic_switch.py',
    'hymrpl_adaptive_switch.py',
    'hymrpl_benchmark.py',
    'hymrpl_mobility_v2.py',
    'hymrpl_scalability_10.py',
    'hymrpl_scalability_15.py',
    'hymrpl_mesh_resilience.py',
]
for t in tests:
    print(f'\n{\"=\"*50}\n  Running: {t}\n{\"=\"*50}')
    subprocess.run(['python3', '/home/wifi/rpld_hymrpl/test/' + t, '--runs', '3'])
    subprocess.run(['killall', '-9', 'rpld'], capture_output=True)
print('\nDONE! Results in /tmp/hymrpl_results/')
"
```

### 8.3 Resultados Validados

Teste `hymrpl_dynamic_switch.py`:
- Fase A (N, bat=10%): root→s5 = 0.277ms, PDR=100%
- Fase B (S, bat=100%): root→s5 = 0.170ms, PDR=100% (redução de 38.6%)
- Fase C (N, bat=10%): root→s5 = 0.189ms, PDR=100%
- FIFO autenticado: `Sent (authenticated): CLASS_S [nonce=000651cfccf9679d]`
- Motor adaptativo: score=1.000 (S) / score=0.730 (N) conforme bateria

---

## 9. Histórico de Alterações

| Data | Alteração |
|------|-----------|
| 2026-05-12 | Integração do motor adaptativo + FIFO seguro no rpld |
| 2026-05-14 | Bateria per-interface (`/tmp/hymrpl_battery_<ifname>`) |
| 2026-05-14 | Atualização de todos os testes para FIFO autenticado |
| 2026-05-14 | Correção do `hymrpl_mesh_resilience.py` (indentação) |
