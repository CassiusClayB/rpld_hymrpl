# HyMRPL — Visão 360° do Projeto e Registro de Alterações

> Documento de referência consolidado. Cobre o que é o projeto, como ele
> funciona de ponta a ponta e o que foi aplicado nas últimas alterações de
> código. Os documentos técnicos do repositório (`README.md`,
> `EXPERIMENTS.md`, `DOC_TECNICA_HYMRPL.md`, `ADAPTIVE_SECURITY_DESIGN.md`)
> estão em inglês; este resumo executivo está em português.

---

## 1. O que é o HyMRPL

O **HyMRPL (Hybrid Mode RPL)** é uma extensão experimental do protocolo RPL
(RFC 6550) que permite a **coexistência simultânea** de comportamentos
*Storing* (Classe S) e *Non-Storing* (Classe N) dentro de uma mesma DODAG,
usando o valor experimental **MOP = 6** (a RFC reserva os valores 4–7).

A implementação é uma extensão do daemon em espaço de usuário **`rpld`**
(Alexander Aring / Ramon Fontes), em C, sobre Linux com interfaces
IEEE 802.15.4 + 6LoWPAN, avaliada em emulação com Mininet-WiFi.

### Problema que resolve

No RPL tradicional, o Modo de Operação (MOP) é **global por DODAG**: todos os
nós são obrigados a usar o mesmo modo.

- **Storing**: cada nó mantém rotas descendentes → baixa latência local, mas
  custo de memória/estado nos nós restritos.
- **Non-Storing**: o estado é centralizado na raiz via *Source Routing Header*
  (SRH) → nós leves, porém caminhos mais longos e dependência do root.

Redes IoT reais são **heterogêneas** (energia, memória, mobilidade variam por
nó). Um único modo global não acomoda essa assimetria. O HyMRPL deixa **cada
nó escolher localmente sua classe**, e ainda **trocar de classe em tempo de
execução**, sem reiniciar o daemon e sem emitir mensagens extras na rede.

---

## 2. Conceito central

| Classe | Comportamento | Rotas | DAO |
|--------|---------------|-------|-----|
| **S** (storing-like) | Mantém estado downward | Instala rotas hop-by-hop via Netlink | Envia ao parent (e agrega filhos) |
| **N** (non-storing-like) | Sem estado local | Delega ao SRH construído pela raiz | Envia ao root |

A classe de cada nó é um único campo de 1 byte (`node_class` em `struct dag` e
`struct iface`). Mudar a classe é uma operação **puramente local** — nenhum bit
adicional trafega na rede.

---

## 3. Arquitetura: três subsistemas integrados

```
┌──────────────────────────────────────────────────────────────┐
│                          rpld (daemon)                          │
│                                                                 │
│  1) Roteamento Híbrido      2) Motor Adaptativo   3) FIFO Seguro │
│     (MOP=6)                    (score + histerese)   (HMAC+nonce)│
│       │                          │                      │        │
│       ▼                          ▼                      ▼        │
│  process.c                  hymrpl_adaptive.c     hymrpl_adaptive.c
│  (DIO/DAO/SRH)              (PDR/energia/estab.)  (autenticação) │
│                                                                 │
│            Event loop libev  ─  hymrpl_periodic_cb (1 s)         │
│                                                                 │
│                    dag->node_class  (1 byte)                    │
└──────────────────────────────────────────────────────────────┘
```

### 3.1 Roteamento híbrido (MOP=6) — `process.c`

- **DAO dual**: no modo híbrido o nó envia DAO ao **parent** (para nós Classe S
  intermediários instalarem rotas locais) e ao **root** (para a raiz montar a
  árvore SRH completa dos caminhos Classe N).
- **Árvore SRH no root**: ao receber DAO com múltiplos *targets*, a raiz
  processa **todos** e instala rotas SRH via Netlink
  (`ip -6 route ... encap rpl segs ...`).
- **Agregação de targets (Classe S)**: nós S incluem os endereços dos filhos no
  DAO, **enriquecendo** a árvore SRH da raiz e ampliando os caminhos
  alternativos sob falha.
- **Dependência de kernel**: `CONFIG_IPV6_RPL_LWTUNNEL` (processa o SRH). Sem
  ele, o encaminhamento Non-Storing não funciona.

### 3.2 Motor de decisão adaptativa — `hymrpl_adaptive.c/.h`

Integrado ao event loop (timer `libev`), decide a classe do nó (S/N) sem
processo Python externo. Detalhado na Seção 4.

### 3.3 FIFO seguro — `hymrpl_adaptive.c` + `hymrpl_cmd.c`

Canal de controle externo autenticado em `/tmp/hymrpl_cmd`. Detalhado na
Seção 5.

---

## 4. Modelo de decisão adaptativa (detalhado)

### 4.1 Score composto

```
S = w_pdr · s_pdr + w_energia · s_energia + w_estab · s_estab
  = 0,4 · s_pdr + 0,3 · s_energia + 0,3 · s_estab

S ≥ 0,75 (θ)  ⇒  Classe S        S < 0,75  ⇒  Classe N
```

### 4.2 Normalização dos dados — por que importa

As três métricas têm **naturezas e unidades diferentes**:

- PDR → razão de sucesso do protocolo (%)
- Energia → carga de bateria (%)
- Estabilidade → contagem de eventos topológicos

Somá-las diretamente seria incorreto: uma métrica poderia dominar apenas por
causa da sua escala. Por isso, **cada componente é mapeado para o intervalo
comum [0, 1]**:

| Componente | Fórmula | Faixa |
|------------|---------|-------|
| `s_pdr` | `sucessos / W` | [0, 1] |
| `s_energia` | `EMA( E(t)/100 )` | [0, 1] |
| `s_estab` | `1 − min(1, N_trocas / Nmax)` | [0, 1] |

Como **todo `s_k ∈ [0,1]` e `Σ w_k = 1,0`**, o score resultante também fica em
[0, 1] e é **diretamente comparável ao limiar θ = 0,75**. Os pesos passam a
expressar **prioridade**, não magnitude — robusto a mudanças de unidade ou
faixa do sensor.

**Exemplo** (só a bateria varia, nó estável):
- Bateria 100%: `S = 0,4·1 + 0,3·1,0 + 0,3·1 = 1,00 → S`
- Bateria 10%: `S = 0,4·1 + 0,3·0,10 + 0,3·1 = 0,73 → N`

### 4.3 Janela deslizante (PDR) — por que importa

O PDR é estimado a partir dos **últimos W = 20** resultados de DAO-ACK
(sucesso = 1, timeout = 0):

```
s_pdr = (1/W) · Σ 1[DAO-ACK_k recebido]
```

- Implementada como **buffer circular**: a amostra mais nova sobrescreve a mais
  antiga (índice em módulo W).
- É uma **média móvel** sobre um histórico recente de tamanho fixo.
- **Por que janela e não média acumulada**: a média desde o início reage cada
  vez mais devagar (amostras antigas nunca saem); a janela mantém a estimativa
  **atual** e filtra perdas pontuais. `W = 20 ≈ 20 s` é estável o bastante e
  ainda responsivo.

### 4.4 Suavização e janelas por componente

| Componente | Fonte | Janela | Suavização |
|------------|-------|--------|------------|
| `s_pdr` | DAO-ACK (sucesso/timeout) | 20 amostras (~20 s) | Média aritmética deslizante |
| `s_energia` | sysfs ou arquivo simulado | leitura a cada 5 s | EMA (α = 0,3) |
| `s_estab` | detecção de parent change | 60 s | contagem normalizada |

Cada janela combina com a dinâmica da grandeza: PDR varia em segundos, energia
em minutos/horas, estabilidade em eventos esporádicos de impacto prolongado.

### 4.5 Histerese e rate limiting

- A troca só é aplicada após **3 ciclos consecutivos** recomendando a mesma
  nova classe (evita *flapping* perto de θ); o contador zera se a recomendação
  inverte.
- **Rate limiting** compartilhado com o FIFO: ≥ 10 s entre trocas, ≤ 3 por
  minuto.

---

## 5. FIFO seguro — 5 camadas

1. **HMAC-SHA256** com token de 256 bits (`/etc/hymrpl/fifo.token`, modo 0600).
   Formato: `CLASS_S|<nonce_hex>|<hmac_hex>`.
2. **Nonce** monotônico (timestamp µs) → bloqueia *replay*.
3. **Rate limiting** (compartilhado com o motor adaptativo).
4. **Permissões 0600** no FIFO → só root escreve.
5. **Log de auditoria** de toda tentativa (aceita/rejeitada, com motivo).

Compatibilidade legada: sem o arquivo de token, aceita comandos em texto plano
(`CLASS_S\n`), com aviso no log; o rate limiting continua valendo. Utilitário
de linha de comando: `hymrpl_cmd` (gera token e envia comandos autenticados).

---

## 6. O que foi aplicado (registro de alterações)

### 6.1 Padronização para inglês
- Traduzidos para inglês os documentos que estavam em português
  (`DOC_TECNICA_HYMRPL.md`, `ADAPTIVE_SECURITY_DESIGN.md`), comentários,
  *docstrings* e strings dos scripts Python de teste, além de comentários
  "Classe S/N" → "Class S/N" no `process.c` e no `lowpan0_hybrid.conf`.
- `README.md` e `EXPERIMENTS.md` já estavam em inglês.

### 6.2 Integração das métricas adaptativas no caminho do protocolo
Antes, os hooks de PDR e de troca de parent existiam **apenas no patch** de
integração — não no `process.c` versionado. Consequência: `s_pdr` ficava preso
em 1,0 e `s_estab` só mudava em timeout de *liveness*. Foi corrigido:

- **`hymrpl_adaptive.c/.h`**:
  - Ponteiro de instância ativa (`g_active`) definido no `init`.
  - Wrappers de módulo `hymrpl_adaptive_notify_dao_sent()`,
    `notify_dao_ack()` e `notify_parent_change()` — *no-op* em nós root.
  - Detecção de **falha de PDR**: um DAO sem DAO-ACK dentro de
    `HYMRPL_DAO_ACK_TIMEOUT` (6 s) registra uma amostra 0 na janela.
  - Novos campos `dao_awaiting` / `dao_sent_time`.
- **`process.c`**:
  - `notify_dao_sent()` após cada `send_dao()` (Storing, Non-Storing, Híbrido).
  - `notify_dao_ack()` ao receber DAO-ACK (`process_daoack`).
  - `notify_parent_change()` na adoção real de novo parent (`process_dio`).
- **`rpld_new.c`**: removida a notificação duplicada de parent change no timer
  de *parent liveness* — a troca passa a ser contada **uma única vez**, na
  adoção, evitando saturar `s_estab` (Nmax = 3) com um só evento.

> **Importante:** nenhuma mensagem padrão do RPL foi alterada. Os hooks são
> **observadores passivos** de eventos já existentes; não há pacote novo, nem
> mudança de formato ou *timing* de DIO/DAO/DAO-ACK/DIS. A detecção de timeout
> é local (leitura de relógio). O **zero overhead de rede** se mantém.

### 6.3 Patch de integração atualizado
`rpld_adaptive_integration.patch` ajustado para refletir a implementação real:
`hymrpl_adaptive_init(engine, ifname, loop)` (3 args, com bateria
por-interface) e passos 6/7 reescritos para o modelo de wrappers.

### 6.4 Teste alinhado à dissertação
`test/hymrpl_adaptive_switch.py`: a Fase C (bateria 100%, perda 30%) passou a
esperar **Classe S** (score ≈ 0,76, *borderline*), conforme a tabela de
sensibilidade de pesos da dissertação.

### 6.5 Verificações executadas
- `getDiagnostics` limpo em `hymrpl_adaptive.c/.h`, `process.c`, `rpld_new.c`.
- `hymrpl_cmd.c` compila com `-Wall -Wextra` sem avisos.
- Todos os scripts Python passam em `py_compile`.
- Compilação completa do daemon depende do ambiente da VM (`libev-dev`,
  `libmnl`, `liblua`, `libssl`), fora do escopo desta máquina.

---

## 7. Validação experimental (resumo)

Ambiente: Mininet-WiFi + 6LoWPAN, kernel com `CONFIG_IPV6_RPL_LWTUNNEL`.

- **Decisão adaptativa (6 fases)**: classe correta em todas — estável + bateria
  cheia → S (score ≈ 1,00); bateria baixa → N (score ≈ 0,73); recuperação → S.
- **Troca dinâmica via FIFO seguro**: N→S→N em execução com 100% de PDR;
  latência local s4→s5 reduzida ~38,6% após N→S; comandos autenticados.
- **Resiliência em mesh** (falha após restauração): PDR HyMRPL **87,2%** vs
  Storing 79,5% vs Non-Storing 64,1% (+23,1 pp sobre Non-Storing, +7,7 sobre
  Storing), graças à árvore SRH enriquecida.
- **Custo**: CPU, memória e tráfego de controle **idênticos** aos modos
  tradicionais. Adaptação sem overhead mensurável.

Scripts de experimento e instruções completas: ver `EXPERIMENTS.md`.

---

## 8. Estrutura do repositório (principais arquivos)

```
rpld_hymrpl/
├── README.md                       # Setup, kernel, instalação
├── EXPERIMENTS.md                  # Guia de execução dos experimentos
├── DOC_TECNICA_HYMRPL.md           # Documentação técnica completa (EN)
├── ADAPTIVE_SECURITY_DESIGN.md     # Design do motor adaptativo + FIFO (EN)
├── VISAO_360_HYMRPL.md             # Este documento
├── rpl.h / dag.h / config.h        # MOP=6, node_class
├── process.c                       # Lógica híbrida DIO/DAO/SRH + hooks
├── hymrpl_adaptive.c / .h          # Motor adaptativo + FIFO seguro
├── hymrpl_cmd.c                    # Utilitário CLI autenticado
├── rpld_new.c                      # main() + periodic_cb + parent liveness
├── *.patch                         # Patches sobre o rpld original
├── build_kernel.sh / install_on_vm.sh / apply_patches.sh
└── test/                           # Experimentos (benchmark, mobilidade,
                                    #  escalabilidade, mesh, troca dinâmica…)
```

---

## 9. Limitações e trabalhos futuros

- **Escala**: validado até ~20 nós; falta avaliação em larga escala com tráfego
  de fundo concorrente (contenção de canal).
- **PDR por DAO-ACK**: a falha depende de timeout local; um modelo com janela de
  *retransmissões* por DAO daria granularidade maior.
- **Distribuição de chave do FIFO**: investigar PKI leve ou derivação a partir
  de credenciais de provisionamento para cenários de larga escala.
- **RSSI como preditor**: usar variação de RSSI como indicador antecipado de
  instabilidade (complementar à troca de parent observada).

---

## 10. Referências

- RFC 6550 — RPL: IPv6 Routing Protocol for Low-Power and Lossy Networks
- RFC 6554 — IPv6 Routing Header for Source Routes with RPL (SRH)
- RFC 6206 — The Trickle Algorithm
- rpld original — https://github.com/ramonfontes/rpld
- Mininet-WiFi — https://github.com/intrig-unicamp/mininet-wifi
