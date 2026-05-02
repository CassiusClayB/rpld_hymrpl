#!/usr/bin/env python3
"""
HyMRPL — Scalability Test with 50 nodes

Tree topology with 5 main branches, maximum depth of 6 hops.
Each branch has sub-branches to reach 50 nodes.

Structure:
    sensor1 (Root, S)
    ├── Branch A: sensor2(S) -> sensor7(N) -> sensor12(N) -> sensor22(N) -> sensor32(N) -> sensor42(N)
    ├── Branch B: sensor3(S) -> sensor8(N) -> sensor13(S) -> sensor23(N) -> sensor33(N) -> sensor43(N)
    ├── Branch C: sensor4(S) -> sensor9(N) -> sensor14(N) -> sensor24(N) -> sensor34(N) -> sensor44(N)
    ├── Branch D: sensor5(S) -> sensor10(N) -> sensor15(S) -> sensor25(N) -> sensor35(N) -> sensor45(N)
    └── Branch E: sensor6(N) -> sensor11(N) -> sensor16(N) -> sensor26(N) -> sensor36(N) -> sensor46(N)
    + lateral sub-branches to complete 50 nodes

Usage: sudo python3 hymrpl_scalability_50.py [--runs 3] [--modes storing nonstoring hybrid]
"""

import time, re, csv, os, sys, statistics, subprocess
from datetime import datetime
from mininet.log import setLogLevel, info
from mn_wifi.sixLoWPAN.link import LoWPAN
from mn_wifi.net import Mininet_wifi

PREFIX = "fd3c:be8a:173f:8e80"
DODAGID = PREFIX + "::1"
RESULTS_DIR = "/tmp/hymrpl_results"
PING_COUNT = 20
NUM_NODES = 50

CONVERGENCE_ADDR_TIMEOUT = 300
CONVERGENCE_PING_TIMEOUT = 400

# Topology: tree with 5 main branches + sub-branches
# 0-based indices. sensor1=idx0, sensor50=idx49
LINKS = [
    # Level 1: root -> 5 children (1-hop)
    (0, 1), (0, 2), (0, 3), (0, 4), (0, 5),
    # Level 2: each root child -> 2 children (2-hop)
    (1, 6), (1, 7),
    (2, 8), (2, 9),
    (3, 10), (3, 11),
    (4, 12), (4, 13),
    (5, 14), (5, 15),
    # Level 3: (3-hop)
    (6, 16), (7, 17),
    (8, 18), (9, 19),
    (10, 20), (11, 21),
    (12, 22), (13, 23),
    (14, 24), (15, 25),
    # Level 4: (4-hop)
    (16, 26), (17, 27),
    (18, 28), (19, 29),
    (20, 30), (21, 31),
    (22, 32), (23, 33),
    (24, 34), (25, 35),
    # Level 5: (5-hop)
    (26, 36), (27, 37),
    (28, 38), (29, 39),
    (30, 40), (31, 41),
    (32, 42), (33, 43),
    (34, 44), (35, 45),
    # Level 6: (6-hop) — 4 farthest leaf nodes    (36, 46), (38, 47),
    (40, 48), (42, 49),
]

# Compute depth of each node
DEPTH = {0: 0}
for p, c in LINKS:
    DEPTH[c] = DEPTH[p] + 1

# Classes: root and level-1 nodes = S, even intermediate nodes = S, rest = N
HYBRID_CLASSES = {}
for i in range(NUM_NODES):
    name = 'sensor{}'.format(i + 1)
    d = DEPTH.get(i, 99)
    if i == 0:
        HYBRID_CLASSES[name] = 'S'
    elif d <= 1:
        HYBRID_CLASSES[name] = 'S'
    elif i in (8, 12, 15, 20, 22):
        HYBRID_CLASSES[name] = 'S'  # some intermediate nodes with resources
    else:
        HYBRID_CLASSES[name] = 'N'

# Farthest node
FARTHEST_IDX = max(DEPTH, key=DEPTH.get)

# Test pairs by depth
TEST_PAIRS = []
# Find a representative node per depth
for target_depth in range(1, max(DEPTH.values()) + 1):
    candidates = [i for i, d in DEPTH.items() if d == target_depth]
    if candidates:
        idx = candidates[0]
        TEST_PAIRS.append((0, idx, "{}-hop".format(target_depth)))
# Upward from farthest
TEST_PAIRS.append((FARTHEST_IDX, 0, "{}-hop-up".format(DEPTH[FARTHEST_IDX])))
