# HyMRPL — Integrated Adaptive Decision + Secure FIFO

## Overview

This document describes the integration of two modules into rpld:

1. **Adaptive Decision Engine** — replaces the external `hymrpl_monitor.py`
2. **Secure FIFO** — HMAC-SHA256 authentication + replay protection + rate limiting

## Architecture

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
│  │         hymrpl_periodic_cb (ev_timer, 1s)        │     │
│  │                                                   │     │
│  │  1. Check FIFO (authenticated external command)   │     │
│  │  2. If no command → query the adaptive engine     │     │
│  │  3. Apply switch if hysteresis is satisfied       │     │
│  │  4. Shared rate limiting                          │     │
│  └───────────────────────┬───────────────────────────┘     │
│                          │                                  │
│                          ▼                                  │
│              dag->node_class = new_class                    │
│              (1 byte, zero network overhead)                │
└─────────────────────────────────────────────────────────────┘
```

## Created Files

| File | Function |
|---------|--------|
| `hymrpl_adaptive.h` | Header with structs, defines and API |
| `hymrpl_adaptive.c` | Adaptive engine + secure FIFO implementation |
| `hymrpl_cmd.c` | CLI utility for sending authenticated commands |
| `rpld_adaptive_integration.patch` | Integration patch for rpld.c |

## Adaptive Decision Engine

### Formula

```
Score = 0.4 × (PDR/100) + 0.3 × (Energy/100) + 0.3 × (parent_stable ? 1 : 0)
```

- Score ≥ 0.75 → Class S (storing-like)
- Score < 0.75 → Class N (non-storing-like)

### Metric Collection (internal to the daemon)

| Metric | Source | How |
|---------|-------|------|
| PDR | DAO-ACK success/failure | Sliding window of 20 samples |
| Energy | `/tmp/hymrpl_battery_<ifname>` or `/sys/class/power_supply/BAT0/capacity` | Periodic per-interface reading |
| Mobility | Parent change detection in `process_dio()` | Flag + timestamp |

Each node reads its own battery file (e.g. `/tmp/hymrpl_battery_sensor5-pan0`),
allowing independent per-node energy simulation in test environments.

### Hysteresis

- 3 consecutive cycles recommending the same change before applying it
- Avoids oscillation under boundary conditions (score ~0.75)

### Advantages over the External Monitor

- Zero Python dependency
- Decision latency: 1 event loop cycle (vs 5s for the external script)
- PDR measured directly from DAO-ACKs (vs external ping)
- No additional IPC — the decision is internal to the process

## Secure FIFO

### Original Problem

The old FIFO (`mkfifo 0666`) allowed **any process** on the system
to send class-switch commands. This is an attack vector:

- Malicious process forces Class N → node loses local routes
- Rapid S↔N oscillation → DODAG instability
- Replay of captured commands

### Solution: 5 Security Layers

#### 1. HMAC-SHA256 Authentication

```
Message: CLASS_S|<nonce_hex>|<hmac_hex>\n
HMAC = SHA256(token, "CLASS_S|<nonce_hex>")
```

- 256-bit token generated with `/dev/urandom`
- Stored in `/etc/hymrpl/fifo.token` (mode 0600)
- Without the correct token, the command is rejected

#### 2. Replay Protection (Nonce)

- Nonce = timestamp in microseconds (monotonically increasing)
- Each command must have a nonce > last accepted nonce
- Prevents resending of captured commands

#### 3. Rate Limiting

- Minimum 10 seconds between switches
- Maximum 3 switches per minute
- Shared between the external FIFO and the adaptive engine

#### 4. Restricted Permissions

- FIFO created with mode `0600` (only the owner can read/write)
- rpld runs as root → only root can send commands

#### 5. Audit Log

- Every switch attempt is logged (success or failure)
- Includes: timestamp, previous/new class, authenticated yes/no, rejection reason

### Compatibility with Existing Scripts

If `/etc/hymrpl/fifo.token` **does not exist**:
- FIFO accepts plain commands (`CLASS_S\n`) — legacy mode
- A warning is logged
- Rate limiting still applies
- Existing test scripts keep working

## Compilation

```bash
# Install dependency
sudo apt install libssl-dev

# Compile rpld with the adaptive module
cd rpld_hymrpl
meson setup build
ninja -C build

# Compile the command utility
gcc -Wall -o hymrpl_cmd hymrpl_cmd.c -lssl -lcrypto
sudo cp hymrpl_cmd /usr/local/bin/
```

## Usage

```bash
# 1. Generate token (once)
sudo hymrpl_cmd --gen-token

# 2. Start rpld (the adaptive engine enables automatically on non-root nodes)
sudo rpld -C /tmp/lowpan-sensor5.conf -m stderr -d 3

# 3. Send authenticated command (manual override)
sudo hymrpl_cmd CLASS_N

# 4. Or let the adaptive engine decide on its own (no external action)
```

## Decision Flow

```
Every 1 second (hymrpl_periodic_cb):
│
├─ Does the FIFO have a command? ─────────────────────────┐
│   │                                                       │
│   ├─ Token loaded? ─── Yes ─── Verify HMAC               │
│   │                                    │                  │
│   │                              HMAC OK? ── No → LOG + reject
│   │                                    │
│   │                              Nonce > last? ── No → LOG + reject
│   │                                    │
│   │                              Rate limit OK? ── No → LOG + reject
│   │                                    │
│   │                              ✓ Apply switch
│   │
│   └─ Token does not exist ─── Accept plain (legacy) + rate limit
│
├─ Adaptive engine enabled?
│   │
│   ├─ Compute score (PDR, energy, mobility)
│   │
│   ├─ Does the score recommend a change?
│   │   │
│   │   ├─ Hysteresis satisfied (3 cycles)? ── No → increment counter
│   │   │                                           │
│   │   │                                     ✓ Apply switch
│   │   │
│   │   └─ No change → reset counter
│   │
│   └─ Not enabled (root node) → nothing
│
└─ End of cycle
```
