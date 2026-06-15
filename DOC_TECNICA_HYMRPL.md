# HyMRPL — Complete Technical Documentation

## 1. Overview

HyMRPL (Hybrid Mode RPL) is an experimental extension of the RPL protocol (RFC 6550) that allows the simultaneous coexistence of Storing (Class S) and Non-Storing (Class N) behaviors within a single DODAG, using the experimental value MOP=6.

The implementation is based on the `rpld` daemon and integrates three main subsystems:

1. **Hybrid Routing** — dual DAO (parent + root), SRH tree at the root, hop-by-hop routes on S nodes
2. **Adaptive Decision Engine** — in-daemon decision based on PDR, energy and mobility
3. **Secure FIFO** — external control interface with HMAC-SHA256 authentication

---

## 2. Hybrid Routing (MOP=6)

### 2.1 Dual DAO Transmission

In hybrid mode, each node sends a DAO to **two destinations**:

```c
case RPL_DIO_HYBRID:
    // DAO to the parent: allows intermediate Class S nodes
    // to install local downward routes (storing-like)
    send_dao(sock, &dag->parent->addr, dag);

    // DAO to the root: allows the root to build the complete
    // source routing tree for Class N paths
    if (dag->parent->rank > 1)
        send_dao(sock, &dag->dodagid, dag);
    break;
```

This ensures that both routing paradigms work simultaneously.

### 2.2 Building the Source Routing Tree (SRH)

The root maintains a source routing tree (`t_node`) that maps all nodes in the DODAG. When it receives a DAO with multiple targets (aggregated by Class S nodes), the root processes ALL of them:

```c
case RPL_DIO_HYBRID:
    if (dag->my_rank == 1) {
        // Root: process ALL targets in the DAO
        for (i = 0; i < target_count; i++) {
            n = t_insert(&dag->root, &transit->parent,
                         &addr->sin6_addr,
                         &targets[i]->rpl_dao_prefix);
            if (n)
                dag_insert_source_routes(dag->iface->ifindex, n);
        }
    }
```

The `dag_insert_source_routes()` function installs SRH routes via Netlink (`ip -6 route add ... encap rpl segs ...`), allowing the root to forward downward packets using the IPv6 Source Routing Header.

### 2.3 Route Installation by Class

| Role | Behavior |
|-------|---------------|
| Root (rank=1) | Always builds the SRH tree for all targets |
| Class S (non-root) | Installs downward routes via Netlink (`nl_add_route_via`) — hop-by-hop |
| Class N (non-root) | Installs no local routes — delegates to the root via SRH |

### 2.4 Target Aggregation in the DAO (Class S)

Class S nodes include their children's targets in the DAO they send to the parent/root:

```
DAO from sensor5 (N): [target=sensor5]
DAO from sensor4 (S): [target=sensor4, target=sensor5]  ← aggregates child
```

This allows the root to build the complete tree even when intermediate nodes are Class S.

### 2.5 Kernel Dependency: SRH (CONFIG_IPV6_RPL_LWTUNNEL)

Non-Storing forwarding depends on the Linux kernel module `CONFIG_IPV6_RPL_LWTUNNEL`, which:
- Processes the RPL Source Routing Header
- Decrements Segments Left
- Replaces the destination address with the next segment
- Re-forwards the packet

Without this module, `ip -6 route add ... encap rpl ...` fails and Non-Storing mode does not work.

### 2.6 SRH Tree Enrichment in Mesh Topologies

An emergent property of the hybrid design is **SRH tree enrichment** at the root. In pure Non-Storing, each node sends a DAO containing only its own address — the root builds exactly 14 SRH routes (one per node). In HyMRPL, Class S nodes **aggregate their children's addresses** into the DAOs, providing the root with redundant topological information.

#### Mechanism

Consider sensor2 (Class S) with children sensor5 and sensor6 (Class N):

- **Non-Storing:** sensor5 → DAO(target=sensor5), sensor6 → DAO(target=sensor6), sensor2 → DAO(target=sensor2). Root receives 3 independent DAOs → 3 SRH routes.

- **HyMRPL:** sensor5 → DAO(target=sensor5), sensor6 → DAO(target=sensor6), sensor2 (S) → DAO(target=sensor2, target=sensor5, target=sensor6). Root receives redundant information about sensor5/sensor6 through two paths → can build alternative SRH routes.

#### Quantitative Result

| Metric | Storing | Non-Storing | HyMRPL |
|---------|---------|-------------|--------|
| SRH routes at root | 0 | 14 | 16–21 |
| Hop-by-hop routes | 14 | 0 | 0 |

HyMRPL keeps 15–50% more SRH routes than Non-Storing, expanding the alternative path options under failure.

#### Impact on Resilience

The enriched SRH tree gives the root **more path options** when a node fails:
- When sensor5 goes down, the root already has alternative SRH routes via sensor6/sensor7 (built from the DAOs aggregated by Class S nodes)
- When sensor7 goes down after sensor5 is restored, the root has already rebuilt routes including paths via sensor5

This property is **emergent** from the hybrid design and was not explored by prior proposals (DualMOP, ARPL, 2-Colorable DODAG).

### 2.7 Dual Reconvergence Mechanism (Mesh Resilience)

In mesh topologies with redundant links, HyMRPL combines two reconvergence mechanisms that operate simultaneously:

1. **Centralized reconvergence (via SRH):** The root maintains the complete SRH tree and can redirect traffic through alternative paths immediately, without waiting for new DAOs.

2. **Local reconvergence (via hop-by-hop):** Class S nodes neighboring the failed node keep local routes to direct neighbors, offering immediate failover while global reconvergence happens.

#### Experimental Result (Phase P4b — kill sensor7 after restoring sensor5)

| Mode | PDR | Reachable Nodes |
|------|-----|-----------------|
| Storing | 79.5% | 10.3 |
| Non-Storing | 64.1% | 8.3 |
| **HyMRPL** | **87.2%** | **11.3** |

An advantage of 23.1 percentage points over Non-Storing and 7.7 over Storing.

#### Why the Pure Modes Fail

- **Storing:** Hop-by-hop routes become *stale* — packets keep being forwarded to the dead node until the Trickle timer detects the inconsistency (Boubekeur et al., ARPL 2019).
- **Non-Storing:** Rebuilding the SRH tree depends on new DAOs, which can be slow when multiple nodes compete for the same alternative parent (Ko et al., DualMOP 2015).
- **HyMRPL:** The root already has alternative SRH routes (centralized view) + Class S nodes have local routes (immediate failover). Reconvergence faster than any isolated mode.

#### Full Recovery

In all restoration phases (P4a, P5a, P6, P8, P9), the three modes returned to 100% PDR. This confirms that HyMRPL does not interfere with RPL's native reconvergence (Trickle timer, RFC 6206).

#### Double Churn as an Edge Case

In phase P7 (simultaneous removal of sensor9 and sensor10), the three modes showed equivalent PDR (~58–64%). The limitation is topological, not protocol-related — when removal partitions the network, no mechanism can deliver packets to unreachable destinations. HyMRPL introduces no additional vulnerability.

---

## 3. Adaptive Decision Engine (Integrated)

### 3.1 Architecture

The adaptive engine is integrated directly into the rpld daemon, eliminating the dependency on the external `hymrpl_monitor.py` script. It runs as a periodic timer in the libev event loop.

```
┌─────────────────────────────────────────────────────────┐
│                        rpld (daemon)                      │
│                                                          │
│  ┌──────────────────┐    ┌─────────────────────────┐    │
│  │  Adaptive Engine  │    │     Secure FIFO          │    │
│  │                    │    │                          │    │
│  │  • PDR (DAO-ACK)  │    │  • HMAC-SHA256 auth     │    │
│  │  • Energy (sysfs)  │    │  • Nonce (anti-replay)  │    │
│  │  • Mobility        │    │  • Rate limiting        │    │
│  │    (parent change) │    │  • Permissions 0600     │    │
│  │                    │    │  • Audit log            │    │
│  │  Score → Decision  │    │                          │    │
│  │  with hysteresis   │    │  /tmp/hymrpl_cmd         │    │
│  └────────┬───────────┘    └──────────┬──────────────┘    │
│           │                           │                   │
│           ▼                           ▼                   │
│  ┌─────────────────────────────────────────────────┐     │
│  │         hymrpl_periodic_cb (ev_timer, 5s)        │     │
│  │                                                   │     │
│  │  1. Check FIFO (authenticated external command)   │     │
│  │  2. If no command → query the adaptive engine     │     │
│  │  3. Apply switch if hysteresis is satisfied       │     │
│  │  4. Shared rate limiting                          │     │
│  └───────────────────────────────────────────────────┘     │
│                                                            │
│              dag->node_class = new_class                    │
│              (1 byte, zero network overhead)                │
└────────────────────────────────────────────────────────────┘
```

### 3.2 Decision Formula

```
Score = 0.4 × (PDR/100) + 0.3 × (Energy/100) + 0.3 × (parent_stable ? 1 : 0)
```

- Score ≥ 0.75 → Class S (stable node, with resources)
- Score < 0.75 → Class N (unstable or constrained node)

### 3.3 Metric Collection

| Metric | Source | Method |
|---------|-------|--------|
| PDR | DAO-ACK success/failure | Sliding window of 20 samples |
| Energy | `/tmp/hymrpl_battery_<ifname>` or `/sys/class/power_supply/BAT0/capacity` | Periodic per-interface reading |
| Mobility | Parent change detection in `process_dio()` | Flag + timestamp, stabilizes after 15s |

### 3.4 Per-Interface Battery

Each node reads its own battery file based on the interface name:

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

Examples: `/tmp/hymrpl_battery_sensor4-pan0`, `/tmp/hymrpl_battery_sensor5-pan0`

This allows independent per-node energy simulation in test environments.

### 3.5 Hysteresis

- 3 consecutive cycles (15s) recommending the same change before applying it
- Avoids oscillation under boundary conditions (score ~0.75)
- The counter resets if the recommendation changes direction

### 3.6 Advantages over the External Monitor

| Aspect | External Monitor (Python) | Integrated Engine (C) |
|---------|--------------------------|---------------------|
| Dependency | Python 3 + psutil | None |
| Decision latency | 5s (script interval) | 5s (libev timer) |
| PDR measurement | external ping (imprecise) | direct DAO-ACK (precise) |
| IPC | plain text FIFO | internal to the process |
| Security | None | HMAC + nonce + rate limit |
| Overhead | separate process | Zero (same process) |

---

## 4. Secure FIFO

### 4.1 Original Problem

The old FIFO (`mkfifo 0666`) allowed any process to send commands:
- Malicious process forces Class N → node loses local routes
- Rapid S↔N oscillation → DODAG instability
- Replay of captured commands

### 4.2 Solution: 5 Security Layers

#### Layer 1: HMAC-SHA256 Authentication

```
Format: CLASS_S|<nonce_hex_16>|<hmac_hex_64>\n
HMAC = SHA256(token, "CLASS_S|<nonce_hex>")
```

- 256-bit token generated with `/dev/urandom`
- Stored in `/etc/hymrpl/fifo.token` (mode 0600)

#### Layer 2: Replay Protection (Nonce)

- Nonce = timestamp in microseconds (monotonically increasing)
- Each command must have a nonce > last accepted nonce

#### Layer 3: Rate Limiting

- Minimum 10 seconds between switches
- Maximum 3 switches per minute
- Shared between the external FIFO and the adaptive engine

#### Layer 4: Restricted Permissions

- FIFO created with mode `0600` (root only)

#### Layer 5: Audit Log

- Every attempt is logged (success/failure, rejection reason)

### 4.3 Command Utility: hymrpl_cmd

```bash
# Generate token (once)
sudo hymrpl_cmd --gen-token

# Send authenticated command
sudo hymrpl_cmd CLASS_S
sudo hymrpl_cmd CLASS_N
```

### 4.4 Legacy Compatibility

If `/etc/hymrpl/fifo.token` does not exist:
- FIFO accepts plain commands (`CLASS_S\n`)
- A warning is logged
- Rate limiting still applies

---

## 5. Complete Decision Flow

```
Every 5 seconds (hymrpl_periodic_cb):
│
├─ Does the FIFO have a command?
│   ├─ Token loaded? → Verify HMAC → Nonce OK? → Rate limit OK? → Apply
│   └─ No token → Accept plain (legacy) + rate limit → Apply
│
├─ Adaptive engine enabled? (non-root nodes only)
│   ├─ Read energy from /tmp/hymrpl_battery_<ifname>
│   ├─ Check parent stability (15s without change)
│   ├─ Compute score
│   ├─ Does the score recommend a change?
│   │   ├─ Hysteresis satisfied (3 cycles)? → Rate limit OK? → Apply
│   │   └─ No → increment counter
│   └─ No change → reset counter
│
└─ End of cycle
```

---

## 6. Project Files

| File | Function |
|---------|--------|
| `rpl.h` | Enum `RPL_DIO_HYBRID=6`, defines `HYMRPL_CLASS_S/N` |
| `dag.h` | `uint8_t node_class` field in `struct dag` |
| `config.h` | `uint8_t node_class` field in `struct iface` |
| `process.c` | Hybrid logic: DIO with class propagation, dual DAO, route installation by class |
| `hymrpl_adaptive.h` | Adaptive engine + secure FIFO header |
| `hymrpl_adaptive.c` | Complete implementation (decision + security) |
| `hymrpl_cmd.c` | CLI utility for authenticated commands |
| `rpld.c` (rpld_new.c) | Integration: `hymrpl_periodic_cb`, adaptive and FIFO init |

---

## 7. Compilation

```bash
# On the VM (192.168.0.101)
cd /home/wifi/rpld
sudo apt install libssl-dev libev-dev liblua5.3-dev libnl-3-dev libnl-genl-3-dev meson ninja-build

# Compile rpld
meson setup build   # (first time only)
ninja -C build
sudo cp build/rpld /usr/local/bin/rpld

# Compile hymrpl_cmd
gcc -Wall -o hymrpl_cmd hymrpl_cmd.c -lssl -lcrypto
sudo cp hymrpl_cmd /usr/local/bin/
```

---

## 8. Experimental Tests

All tests were updated to work with the current rpld (adaptive engine + secure FIFO + per-interface battery).

### 8.1 Shared Helper: `test/hymrpl_helpers.py`

```python
from hymrpl_helpers import setup_battery_for_topology, send_authenticated_cmd, ensure_token
```

Functions:
- `setup_battery_for_topology(sensors, HYBRID_CLASSES)` — sets per-interface battery before starting rpld
- `send_authenticated_cmd(sensor, "CLASS_N")` — sends via `hymrpl_cmd`
- `set_battery_level(sensor, 10)` — sets a specific level

### 8.2 Execution

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

### 8.3 Validated Results

Test `hymrpl_dynamic_switch.py`:
- Phase A (N, bat=10%): root→s5 = 0.277ms, PDR=100%
- Phase B (S, bat=100%): root→s5 = 0.170ms, PDR=100% (38.6% reduction)
- Phase C (N, bat=10%): root→s5 = 0.189ms, PDR=100%
- Authenticated FIFO: `Sent (authenticated): CLASS_S [nonce=000651cfccf9679d]`
- Adaptive engine: score=1.000 (S) / score=0.730 (N) according to battery

---

## 9. Change History

| Date | Change |
|------|-----------|
| 2026-05-12 | Integration of the adaptive engine + secure FIFO into rpld |
| 2026-05-14 | Per-interface battery (`/tmp/hymrpl_battery_<ifname>`) |
| 2026-05-14 | Update of all tests for authenticated FIFO |
| 2026-05-14 | Fix of `hymrpl_mesh_resilience.py` (indentation) |
