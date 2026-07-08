# HyMRPL — Decisão Adaptativa Integrada + FIFO Seguro

## Visão Geral

Este documento descreve a integração de dois módulos no rpld:

1. **Motor de Decisão Adaptativa** — substitui o `hymrpl_monitor.py` externo
2. **FIFO Seguro** — autenticação HMAC-SHA256 + proteção contra replay + rate limiting

## Arquitetura

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
│  │         hymrpl_periodic_cb (ev_timer, 1s)        │     │
│  │                                                   │     │
│  │  1. Verifica FIFO (comando externo autenticado)   │     │
│  │  2. Se não há comando → consulta motor adaptativo │     │
│  │  3. Aplica troca se histerese satisfeita          │     │
│  │  4. Rate limiting compartilhado                   │     │
│  └───────────────────────┬───────────────────────────┘     │
│                          │                                  │
│                          ▼                                  │
│              dag->node_class = new_class                    │
│              (1 byte, zero overhead na rede)                │
└─────────────────────────────────────────────────────────────┘
```

## Arquivos Criados

| Arquivo | Função |
|---------|--------|
| `hymrpl_adaptive.h` | Header com structs, defines e API |
| `hymrpl_adaptive.c` | Implementação do motor adaptativo + FIFO seguro |
| `hymrpl_cmd.c` | Utilitário CLI para enviar comandos autenticados |
| `rpld_adaptive_integration.patch` | Patch de integração no rpld.c |

## Motor de Decisão Adaptativa

### Fórmula

```
Score = 0.4 × (PDR/100) + 0.3 × (Energia/100) + 0.3 × (parent_estável ? 1 : 0)
```

- Score ≥ 0.75 → Classe S (storing-like)
- Score < 0.75 → Classe N (non-storing-like)

### Coleta de Métricas (interna ao daemon)

| Métrica | Fonte | Como |
|---------|-------|------|
| PDR | DAO-ACK success/failure | Janela deslizante de 20 amostras |
| Energia | `/tmp/hymrpl_battery_<ifname>` ou `/sys/class/power_supply/BAT0/capacity` | Leitura periódica per-interface |
| Mobilidade | Detecção de parent change no `process_dio()` | Flag + timestamp |

Cada nó lê seu próprio arquivo de bateria (ex: `/tmp/hymrpl_battery_sensor5-pan0`),
permitindo simulação independente de energia por nó em ambientes de teste.

### Histerese

- 3 ciclos consecutivos recomendando a mesma mudança antes de aplicar
- Evita oscilação em condições de fronteira (score ~0.75)

### Vantagens sobre o monitor externo

- Zero dependência de Python
- Latência de decisão: 1 ciclo do event loop (vs 5s do script externo)
- PDR medido diretamente dos DAO-ACKs (vs ping externo)
- Sem IPC adicional — decisão é interna ao processo

## FIFO Seguro

### Problema Original

O FIFO antigo (`mkfifo 0666`) permitia que **qualquer processo** no sistema
enviasse comandos de troca de classe. Isso é um vetor de ataque:

- Processo malicioso força Classe N → nó perde rotas locais
- Oscilação rápida S↔N → instabilidade na DODAG
- Replay de comandos capturados

### Solução: 5 Camadas de Segurança

#### 1. Autenticação HMAC-SHA256

```
Mensagem: CLASS_S|<nonce_hex>|<hmac_hex>\n
HMAC = SHA256(token, "CLASS_S|<nonce_hex>")
```

- Token de 256 bits gerado com `/dev/urandom`
- Armazenado em `/etc/hymrpl/fifo.token` (modo 0600)
- Sem o token correto, o comando é rejeitado

#### 2. Proteção contra Replay (Nonce)

- Nonce = timestamp em microsegundos (monotonicamente crescente)
- Cada comando deve ter nonce > último nonce aceito
- Impede reenvio de comandos capturados

#### 3. Rate Limiting

- Mínimo 10 segundos entre trocas
- Máximo 3 trocas por minuto
- Compartilhado entre FIFO externo e motor adaptativo

#### 4. Permissões Restritas

- FIFO criado com modo `0600` (apenas owner pode ler/escrever)
- rpld roda como root → apenas root pode enviar comandos

#### 5. Audit Log

- Toda tentativa de troca é logada (sucesso ou falha)
- Inclui: timestamp, classe anterior/nova, autenticado sim/não, motivo de rejeição

### Compatibilidade com Scripts Existentes

Se `/etc/hymrpl/fifo.token` **não existir**:
- FIFO aceita comandos plain (`CLASS_S\n`) — modo legado
- Warning é logado
- Rate limiting ainda se aplica
- Scripts de teste existentes continuam funcionando

## Compilação

```bash
# Instalar dependência
sudo apt install libssl-dev

# Compilar rpld com módulo adaptativo
cd rpld_hymrpl
meson setup build
ninja -C build

# Compilar utilitário de comando
gcc -Wall -o hymrpl_cmd hymrpl_cmd.c -lssl -lcrypto
sudo cp hymrpl_cmd /usr/local/bin/
```

## Uso

```bash
# 1. Gerar token (uma vez)
sudo hymrpl_cmd --gen-token

# 2. Iniciar rpld (motor adaptativo ativa automaticamente em nós não-root)
sudo rpld -C /tmp/lowpan-sensor5.conf -m stderr -d 3

# 3. Enviar comando autenticado (override manual)
sudo hymrpl_cmd CLASS_N

# 4. Ou deixar o motor adaptativo decidir sozinho (sem ação externa)
```

## Fluxo de Decisão

```
Cada 1 segundo (hymrpl_periodic_cb):
│
├─ FIFO tem comando? ──────────────────────────────────────┐
│   │                                                       │
│   ├─ Token carregado? ─── Sim ─── Verificar HMAC         │
│   │                                    │                  │
│   │                              HMAC OK? ── Não → LOG + rejeitar
│   │                                    │
│   │                              Nonce > último? ── Não → LOG + rejeitar
│   │                                    │
│   │                              Rate limit OK? ── Não → LOG + rejeitar
│   │                                    │
│   │                              ✓ Aplicar troca
│   │
│   └─ Token não existe ─── Aceitar plain (legacy) + rate limit
│
├─ Motor adaptativo habilitado?
│   │
│   ├─ Calcular score (PDR, energia, mobilidade)
│   │
│   ├─ Score recomenda mudança?
│   │   │
│   │   ├─ Histerese satisfeita (3 ciclos)? ── Não → incrementar contador
│   │   │                                           │
│   │   │                                     ✓ Aplicar troca
│   │   │
│   │   └─ Sem mudança → resetar contador
│   │
│   └─ Não habilitado (nó root) → nada
│
└─ Fim do ciclo
```
