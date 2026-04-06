# HyMRPL — Hybrid Mode of Operation for RPL (MOP=6)

Repositório: https://github.com/CassiusClayB/rpld_hymrpl.git

Implementação do HyMRPL, um modo híbrido experimental para o protocolo RPL que permite a coexistência simultânea de comportamentos Storing (Classe S) e Non-Storing (Classe N) em uma mesma DODAG. Baseado no daemon [rpld](https://github.com/ramonfontes/rpld) de Ramon Fontes / Alexander Aring.

## Visão Geral

O RPL (RFC 6550) define o Mode of Operation (MOP) como um parâmetro global da DODAG, forçando todos os nós a operarem no mesmo modo. O HyMRPL introduz o valor experimental MOP=6 (permitido pela RFC, que reserva os valores 4-7), no qual cada nó decide localmente se opera como:

- **Classe S** (storing-like): mantém tabelas de rotas descendentes, encaminha hop-by-hop
- **Classe N** (non-storing-like): não mantém estado, delega ao root via Source Routing Header (SRH)

A troca de classe pode ser feita em runtime via FIFO (`/tmp/hymrpl_cmd`), sem reiniciar o daemon e sem emitir mensagens extras na rede.

## Pré-requisitos

- Ubuntu 22.04+ (testado em VM com Mininet-WiFi)
- Kernel Linux 6.x compilado com suporte a RPL SRH (ver seção abaixo)
- Mininet-WiFi com suporte a 6LoWPAN
- Dependências: `meson`, `ninja-build`, `liblua5.3-dev`, `libev-dev`, `libnl-3-dev`, `libnl-genl-3-dev`

---

## 1. Compilação do Kernel com SRH Habilitado

O HyMRPL depende do módulo `CONFIG_IPV6_RPL_LWTUNNEL` do kernel Linux para processar o Source Routing Header (SRH) do RPL. Sem esse módulo, o encaminhamento Non-Storing e o modo híbrido não funcionam. O kernel padrão do Ubuntu **não** vem com esse módulo habilitado, portanto é necessário recompilar.

### O que o script `build_kernel.sh` faz

O script automatiza todo o processo de compilação do kernel 6.11:

```
bash build_kernel.sh
```

**Etapas executadas pelo script:**

1. **Instala dependências de build** (`build-essential`, `libncurses-dev`, `bison`, `flex`, `libssl-dev`, `libelf-dev`, `bc`, `dwarves`)
2. **Baixa o kernel 6.11** do kernel.org (se ainda não baixado)
3. **Extrai** o tarball em `~/linux-6.11/`
4. **Configura o kernel** partindo do `.config` atual da máquina e habilitando os módulos necessários:
   - `CONFIG_IPV6_RPL_LWTUNNEL=y` — suporte ao SRH do RPL (módulo principal)
   - `CONFIG_6LOWPAN=m` — camada de adaptação 6LoWPAN
   - `CONFIG_IEEE802154=m` — suporte a IEEE 802.15.4
   - `CONFIG_IEEE802154_HWSIM=m` — interfaces 802.15.4 virtuais (para emulação)
   - `CONFIG_MAC802154=m` — camada MAC 802.15.4
   - `CONFIG_LWTUNNEL=y` — infraestrutura de lightweight tunnels (dependência do SRH)
5. **Desabilita certificados Ubuntu** que causam erro de build (`SYSTEM_TRUSTED_KEYS`, `SYSTEM_REVOCATION_KEYS`)
6. **Compila** o kernel como pacotes `.deb` usando todos os cores disponíveis (15-30 min)
7. **Lista os pacotes gerados** (`linux-image-6.11*.deb`, `linux-headers-6.11*.deb`)

### Instalação do kernel na VM

Após a compilação, copie os `.deb` para a VM e instale:

```bash
# Na máquina host
scp ~/linux-image-6.11*.deb ~/linux-headers-6.11*.deb usuario@IP_DA_VM:~/

# Na VM
sudo dpkg -i ~/linux-image-6.11*.deb ~/linux-headers-6.11*.deb
sudo update-grub
sudo reboot
```

### Verificação

Após o reboot, confirme que o módulo está habilitado:

```bash
grep RPL_LWTUNNEL /boot/config-$(uname -r)
# Deve retornar: CONFIG_IPV6_RPL_LWTUNNEL=y
```

### Por que esses módulos são necessários

| Módulo | Função |
|--------|--------|
| `CONFIG_IPV6_RPL_LWTUNNEL` | Processa o SRH: atualiza Segments Left, substitui endereço de destino, reencaminha o pacote |
| `CONFIG_6LOWPAN` | Compressão/fragmentação IPv6 sobre 802.15.4 |
| `CONFIG_IEEE802154_HWSIM` | Cria interfaces 802.15.4 virtuais para emulação no Mininet-WiFi |
| `CONFIG_LWTUNNEL` | Base para lightweight tunnels, dependência do módulo SRH |

Sem o `CONFIG_IPV6_RPL_LWTUNNEL`, o comando `ip -6 route add ... encap rpl ...` falha e nenhuma rota SRH pode ser instalada.

---

## 2. Instalação Rápida na VM

O script `install_on_vm.sh` automatiza a instalação completa dentro da VM:

```bash
bash install_on_vm.sh
```

**O que ele faz:**
1. Instala os pacotes `.deb` do kernel compilado
2. Instala dependências do rpld (`meson`, `ninja-build`, `liblua5.3-dev`, etc.)
3. Clona o rpld original do GitHub
4. Copia os arquivos de configuração HyMRPL para `/etc/rpld/`
5. Instrui o reboot e os próximos passos

---

## 3. Aplicação dos Patches no rpld

O script `apply_patches.sh` aplica as modificações do HyMRPL sobre o rpld original:

```bash
bash apply_patches.sh ~/rpld ~/rpld_hymrpl
```

**O que ele faz:**
1. Faz backup dos arquivos originais em `rpld/backup_original/`
2. Substitui os arquivos completos: `rpl.h`, `dag.h`, `config.h`, `process.c`
3. Aplica patches via `sed` nos arquivos `dag.c` e `config.c`:
   - Inicializa `node_class` no `dag_create()`
   - Adiciona case `RPL_DIO_HYBRID` no `dag_build_dao()` (agregação de targets por classe)
   - Adiciona leitura de `node_class` no `config_load()` (iface e DAG)

### Compilação após os patches

```bash
cd ~/rpld
rm -rf build
meson build
ninja -C build
```

---

## 4. Arquivos Modificados

| Arquivo | Modificação |
|---------|-------------|
| `rpl.h` | Adicionado `RPL_DIO_HYBRID = 0x6` no enum `RPL_DIO_MOP` e defines `HYMRPL_CLASS_S` / `HYMRPL_CLASS_N` |
| `dag.h` | Adicionado campo `uint8_t node_class` na `struct dag` |
| `config.h` | Adicionado campo `uint8_t node_class` na `struct iface` |
| `process.c` | Lógica híbrida completa: processamento de DIO com propagação de classe, processamento de DAO com 3 comportamentos (root/Classe S/Classe N), suporte a múltiplos targets por DAO |
| `dag.c` (via patch) | Inicialização de `node_class`, construção de DAO com agregação de targets para Classe S |
| `config.c` (via patch) | Leitura de `node_class` do arquivo de configuração Lua |

---

## 5. Arquivos de Configuração

Os arquivos de configuração usam sintaxe Lua e ficam em `/etc/rpld/` ou na pasta `test/`.

### Root (MOP=6, Classe S)
```lua
-- test/lowpan0_hybrid.conf
ifaces = { {
    ifname = "lowpan0",
    dodag_root = true,
    node_class = "S",
    rpls = { {
        instance = 1,
        dags = { {
            mode_of_operation = 6,
            node_class = "S",
            dest_prefix = "fd3c:be8a:173f:8e80::/64",
        }, }
    }, }
}, }
```

### Nó Classe S (storing-like)
```lua
-- test/lowpan_hybrid_classS.conf
ifaces = { {
    ifname = "lowpan0",
    dodag_root = false,
    node_class = "S",
}, }
```

### Nó Classe N (non-storing-like)
```lua
-- test/lowpan_hybrid_classN.conf
ifaces = { {
    ifname = "lowpan0",
    dodag_root = false,
    node_class = "N",
}, }
```

---

## 6. Troca Dinâmica de Classe via FIFO

O rpld modificado cria um FIFO POSIX em `/tmp/hymrpl_cmd` e o verifica periodicamente. Para trocar a classe de um nó em runtime:

```bash
# Trocar para Classe N
echo "CLASS_N" > /tmp/hymrpl_cmd

# Trocar para Classe S
echo "CLASS_S" > /tmp/hymrpl_cmd
```

A troca é imediata, não-disruptiva (PDR 100% durante a transição), reversível e não emite nenhuma mensagem extra na rede.

---

## 7. Monitor de Adaptação Dinâmica

O script `hymrpl_monitor.py` automatiza a decisão de classe baseado em métricas locais:

```bash
sudo python3 hymrpl_monitor.py --interval 5 --battery-file /tmp/hymrpl_battery
```

**Métricas coletadas:**
- CPU (via `/proc/stat`)
- Memória disponível (via `/proc/meminfo`)
- Bateria simulada (via arquivo configurável)

**Lógica de decisão:**
- CPU > 70% OU memória < 20 MB OU bateria < 20% → sugere Classe N
- CPU < 40% E memória ok E bateria > 50% → sugere Classe S
- Histerese de 3 ciclos consecutivos antes de efetuar a troca

O monitor escreve a decisão no FIFO `/tmp/hymrpl_cmd` e registra eventos em `/tmp/hymrpl_monitor.log`.

---

## 8. Script de Topologia de Teste

A pasta `test/` contém o script de topologia base e os arquivos de configuração. Os demais scripts de experimento (benchmark, mobilidade, escalabilidade, churn, etc.) foram utilizados exclusivamente durante a avaliação da dissertação e não são incluídos neste repositório.

### `test/hymrpl_topology.py`

Cria a topologia base de 5 nós no Mininet-WiFi com 6LoWPAN e inicia o rpld em modo híbrido (MOP=6):

```bash
sudo python3 test/hymrpl_topology.py
```

**O que o script faz:**

1. Cria 5 sensores com interfaces IEEE 802.15.4 virtuais (`panid=0xbeef`)
2. Estabelece os enlaces: sensor1↔sensor2, sensor1↔sensor3, sensor3↔sensor4, sensor4↔sensor5
3. Gera dinamicamente o arquivo de configuração Lua para cada nó com a classe atribuída:
   - sensor1: Root, Classe S
   - sensor2: Classe N
   - sensor3: Classe S
   - sensor4: Classe N
   - sensor5: Classe N (nó móvel)
4. Inicia o daemon `rpld` em cada nó com MOP=6
5. Aguarda 20s para convergência da DODAG
6. Testa conectividade via `ping6` do root para todos os nós
7. Abre a CLI do Mininet-WiFi para interação manual

A partir da CLI, é possível verificar rotas (`ip -6 route`), executar `traceroute6`, capturar pacotes com `tcpdump` e testar a troca dinâmica de classe via FIFO.

---

## 9. Estrutura do Repositório

```
rpld_hymrpl/
├── README.md                  # Este arquivo
├── rpl.h                      # Header RPL com MOP=6 e defines de classe
├── dag.h                      # Struct dag com campo node_class
├── config.h                   # Struct iface com campo node_class
├── process.c                  # Lógica híbrida de DIO e DAO
├── dag.c.patch                # Patch para dag.c (agregação de targets)
├── config.c.patch             # Patch para config.c (leitura de node_class)
├── rpld_fifo.c.patch          # Patch para FIFO no loop principal
├── build_kernel.sh            # Compilação do kernel 6.11 com SRH
├── apply_patches.sh           # Aplicação dos patches no rpld original
├── install_on_vm.sh           # Instalação completa na VM
├── hymrpl_monitor.py          # Monitor de adaptação dinâmica
└── test/
    ├── lowpan0_hybrid.conf        # Config do root (MOP=6, Classe S)
    ├── lowpan_hybrid_classS.conf  # Config Classe S (non-root)
    ├── lowpan_hybrid_classN.conf  # Config Classe N (non-root)
    └── hymrpl_topology.py         # Topologia base de 5 nós
```

---

## Referências

- RFC 6550 — RPL: IPv6 Routing Protocol for Low-Power and Lossy Networks
- RFC 6554 — An IPv6 Routing Header for Source Routes with RPL
- rpld original: https://github.com/ramonfontes/rpld
- Mininet-WiFi: https://github.com/intrig-unicamp/mininet-wifi
