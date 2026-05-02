# HyMRPL — Hybrid Mode of Operation for RPL (MOP=6)

Repository: https://github.com/CassiusClayB/rpld_hymrpl.git

Implementation of HyMRPL, an experimental hybrid mode for the RPL protocol that enables the simultaneous coexistence of Storing (Class S) and Non-Storing (Class N) behaviors within a single DODAG. Based on the [rpld](https://github.com/ramonfontes/rpld) daemon by Ramon Fontes / Alexander Aring.

## Overview

RPL (RFC 6550) defines the Mode of Operation (MOP) as a global DODAG parameter, forcing all nodes to operate in the same mode. HyMRPL introduces the experimental value MOP=6 (permitted by the RFC, which reserves values 4-7), where each node locally decides whether to operate as:

- **Class S** (storing-like): maintains downward routing tables, forwards hop-by-hop
- **Class N** (non-storing-like): maintains no state, delegates to the root via Source Routing Header (SRH)

Class switching can be performed at runtime via FIFO (`/tmp/hymrpl_cmd`), without restarting the daemon and without emitting any extra messages on the network.

## Prerequisites

- Ubuntu 22.04+ (tested on a VM with Mininet-WiFi)
- Linux Kernel 6.x compiled with RPL SRH support (see section below)
- Mininet-WiFi with 6LoWPAN support
- Dependencies: `meson`, `ninja-build`, `liblua5.3-dev`, `libev-dev`, `libnl-3-dev`, `libnl-genl-3-dev`

---

## 1. Kernel Compilation with SRH Support

HyMRPL depends on the `CONFIG_IPV6_RPL_LWTUNNEL` Linux kernel module to process the RPL Source Routing Header (SRH). Without this module, Non-Storing forwarding and hybrid mode do not work. The default Ubuntu kernel does **not** ship with this module enabled, so recompilation is required.

### What `build_kernel.sh` does

The script automates the entire kernel 6.11 compilation process:

```
bash build_kernel.sh
```

**Steps performed by the script:**

1. **Installs build dependencies** (`build-essential`, `libncurses-dev`, `bison`, `flex`, `libssl-dev`, `libelf-dev`, `bc`, `dwarves`)
2. **Downloads kernel 6.11** from kernel.org (if not already downloaded)
3. **Extracts** the tarball to `~/linux-6.11/`
4. **Configures the kernel** starting from the current machine's `.config` and enabling the required modules:
   - `CONFIG_IPV6_RPL_LWTUNNEL=y` — RPL SRH support (main module)
   - `CONFIG_6LOWPAN=m` — 6LoWPAN adaptation layer
   - `CONFIG_IEEE802154=m` — IEEE 802.15.4 support
   - `CONFIG_IEEE802154_HWSIM=m` — Virtual 802.15.4 interfaces (for emulation)
   - `CONFIG_MAC802154=m` — 802.15.4 MAC layer
   - `CONFIG_LWTUNNEL=y` — Lightweight tunnel infrastructure (SRH dependency)
5. **Disables Ubuntu certificates** that cause build errors (`SYSTEM_TRUSTED_KEYS`, `SYSTEM_REVOCATION_KEYS`)
6. **Compiles** the kernel as `.deb` packages using all available cores (15-30 min)
7. **Lists the generated packages** (`linux-image-6.11*.deb`, `linux-headers-6.11*.deb`)

### Installing the kernel on the VM

After compilation, copy the `.deb` files to the VM and install:

```bash
# On the host machine
scp ~/linux-image-6.11*.deb ~/linux-headers-6.11*.deb user@VM_IP:~/

# On the VM
sudo dpkg -i ~/linux-image-6.11*.deb ~/linux-headers-6.11*.deb
sudo update-grub
sudo reboot
```

### Verification

After reboot, confirm the module is enabled:

```bash
grep RPL_LWTUNNEL /boot/config-$(uname -r)
# Should return: CONFIG_IPV6_RPL_LWTUNNEL=y
```

### Why these modules are required

| Module | Function |
|--------|----------|
| `CONFIG_IPV6_RPL_LWTUNNEL` | Processes SRH: updates Segments Left, substitutes destination address, re-forwards the packet |
| `CONFIG_6LOWPAN` | IPv6 compression/fragmentation over 802.15.4 |
| `CONFIG_IEEE802154_HWSIM` | Creates virtual 802.15.4 interfaces for Mininet-WiFi emulation |
| `CONFIG_LWTUNNEL` | Lightweight tunnel base, SRH module dependency |

Without `CONFIG_IPV6_RPL_LWTUNNEL`, the command `ip -6 route add ... encap rpl ...` fails and no SRH routes can be installed.

---

## 2. Quick Installation on the VM

The `install_on_vm.sh` script automates the complete installation inside the VM:

```bash
bash install_on_vm.sh
```

**What it does:**
1. Installs the compiled kernel `.deb` packages
2. Installs rpld dependencies (`meson`, `ninja-build`, `liblua5.3-dev`, etc.)
3. Clones the original rpld from GitHub
4. Copies HyMRPL configuration files to `/etc/rpld/`
5. Instructs reboot and next steps

---

## 3. Applying Patches to rpld

The `apply_patches.sh` script applies HyMRPL modifications on top of the original rpld:

```bash
bash apply_patches.sh ~/rpld ~/rpld_hymrpl
```

**What it does:**
1. Backs up original files to `rpld/backup_original/`
2. Replaces complete files: `rpl.h`, `dag.h`, `config.h`, `process.c`
3. Applies patches via `sed` to `dag.c` and `config.c`:
   - Initializes `node_class` in `dag_create()`
   - Adds `RPL_DIO_HYBRID` case in `dag_build_dao()` (per-class target aggregation)
   - Adds `node_class` reading in `config_load()` (iface and DAG)

### Compilation after patching

```bash
cd ~/rpld
rm -rf build
meson build
ninja -C build
```

---

## 4. Modified Files

| File | Modification |
|------|-------------|
| `rpl.h` | Added `RPL_DIO_HYBRID = 0x6` to the `RPL_DIO_MOP` enum and defines `HYMRPL_CLASS_S` / `HYMRPL_CLASS_N` |
| `dag.h` | Added `uint8_t node_class` field to `struct dag` |
| `config.h` | Added `uint8_t node_class` field to `struct iface` |
| `process.c` | Complete hybrid logic: DIO processing with class propagation, DAO processing with 3 behaviors (root/Class S/Class N), multi-target DAO support |
| `dag.c` (via patch) | `node_class` initialization, DAO construction with target aggregation for Class S |
| `config.c` (via patch) | `node_class` reading from Lua configuration file |

---

## 5. Configuration Files

Configuration files use Lua syntax and are located in `/etc/rpld/` or the `test/` folder.

### Root (MOP=6, Class S)
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

### Class S Node (storing-like)
```lua
-- test/lowpan_hybrid_classS.conf
ifaces = { {
    ifname = "lowpan0",
    dodag_root = false,
    node_class = "S",
}, }
```

### Class N Node (non-storing-like)
```lua
-- test/lowpan_hybrid_classN.conf
ifaces = { {
    ifname = "lowpan0",
    dodag_root = false,
    node_class = "N",
}, }
```

---

## 6. Dynamic Class Switching via FIFO

The modified rpld creates a POSIX FIFO at `/tmp/hymrpl_cmd` and checks it periodically. To switch a node's class at runtime:

```bash
# Switch to Class N
echo "CLASS_N" > /tmp/hymrpl_cmd

# Switch to Class S
echo "CLASS_S" > /tmp/hymrpl_cmd
```

The switch is immediate, non-disruptive (100% PDR during transition), reversible, and emits no extra messages on the network.

---

## 7. Adaptive Monitoring Daemon

The `hymrpl_monitor.py` script automates class decisions based on local metrics:

```bash
sudo python3 hymrpl_monitor.py --interval 5 --battery-file /tmp/hymrpl_battery
```

**Collected metrics:**
- CPU (via `/proc/stat`)
- Available memory (via `/proc/meminfo`)
- Simulated battery (via configurable file)

**Decision logic:**
- CPU > 70% OR memory < 20 MB OR battery < 20% → suggests Class N
- CPU < 40% AND memory ok AND battery > 50% → suggests Class S
- Hysteresis of 3 consecutive cycles before applying the switch

The monitor writes the decision to the FIFO `/tmp/hymrpl_cmd` and logs events to `/tmp/hymrpl_monitor.log`.

---

## 8. Running the Experiments

Complete documentation on how to run all experiments (benchmark, mobility, scalability, churn, packet capture, etc.) is available at:

📄 **[EXPERIMENTS.md](EXPERIMENTS.md)**

For a quick interactive topology test:

```bash
sudo python3 test/hymrpl_topology.py
```

---

## 9. Repository Structure

```
rpld_hymrpl/
├── README.md                  # This file
├── EXPERIMENTS.md             # Experiment execution guide
├── rpl.h                      # RPL header with MOP=6 and class defines
├── dag.h                      # dag struct with node_class field
├── config.h                   # iface struct with node_class field
├── process.c                  # Hybrid DIO and DAO logic
├── dag.c.patch                # Patch for dag.c (target aggregation)
├── config.c.patch             # Patch for config.c (node_class reading)
├── rpld_fifo.c.patch          # Patch for FIFO in main loop
├── rpld_dis_retry.patch       # Patch for DIS retry
├── rpld_parent_liveness.patch # Patch for parent liveness detection
├── build_kernel.sh            # Kernel 6.11 compilation with SRH
├── apply_patches.sh           # Patch application to original rpld
├── install_on_vm.sh           # Complete VM installation
├── hymrpl_monitor.py          # Adaptive monitoring daemon
└── test/
    ├── lowpan0_hybrid.conf            # Root config (MOP=6, Class S)
    ├── lowpan_hybrid_classS.conf      # Class S config (non-root)
    ├── lowpan_hybrid_classN.conf      # Class N config (non-root)
    ├── hymrpl_topology.py             # Interactive 5-node topology
    ├── hymrpl_run_mode.py             # Benchmark with persistent topology
    ├── hymrpl_run_all.sh              # Sequential execution of all 3 modes
    ├── hymrpl_benchmark.py            # Static benchmark (5 nodes)
    ├── hymrpl_hybrid_advantage.py     # Hybrid Advantage experiment
    ├── hymrpl_dynamic_switch.py       # Dynamic class switching via FIFO
    ├── hymrpl_adaptive_switch.py      # Adaptive class decision
    ├── hymrpl_mobility_v2.py          # Mobility with tc netem
    ├── hymrpl_pcap_analysis.py        # Packet capture and analysis
    ├── hymrpl_scalability_10.py       # Scalability 10 nodes
    ├── hymrpl_scalability_15.py       # Scalability 15 nodes
    ├── hymrpl_scalability_20.py       # Scalability 20 nodes
    ├── hymrpl_scalability_50.py       # Scalability 50 nodes
    ├── hymrpl_churn_mobility.py       # Churn with 20 nodes
    ├── hymrpl_mesh_resilience.py      # Mesh topology resilience
    ├── hymrpl_full_experiment.py      # Full experiment
    ├── hymrpl_collect_metrics.py      # Automated metric collection
    ├── hymrpl_gen_latex.py            # LaTeX table generation
    ├── analyze_all.py                 # General result analysis
    ├── analyze_hybrid_advantage.py    # Hybrid Advantage analysis
    ├── analyze_dynamic_switch.py      # Dynamic switch analysis
    ├── analyze_adaptive_switch.py     # Adaptive decision analysis
    ├── calc_stats.py                  # Statistics calculation
    └── pcaps/                         # Packet captures (.pcap)
```

---

## 10. Pre-configured VM

A ready-to-use VM with the complete environment configured (kernel 6.11 with SRH, rpld with HyMRPL patches, Mininet-WiFi with 6LoWPAN) is available for download:

**[Download VM (OVA ~7 GB)](https://drive.google.com/file/d/1gqdqieW8yGN4DSRTHHDztnRtNtA46LMs/view?usp=sharing)**

To import:
1. Open VirtualBox and go to `File > Import Appliance`
2. Select the `mn-wifi-vm.ova` file
3. After import, start the VM and verify the kernel: `uname -r` (should be 6.11.x)

---

## References

- RFC 6550 — RPL: IPv6 Routing Protocol for Low-Power and Lossy Networks
- RFC 6554 — An IPv6 Routing Header for Source Routes with RPL
- Original rpld: https://github.com/ramonfontes/rpld
- Mininet-WiFi: https://github.com/intrig-unicamp/mininet-wifi
