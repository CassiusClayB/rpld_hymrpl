#!/bin/bash
# ============================================================
# HyMRPL — Safe cleanup (replaces mn -c)
# Does NOT kill bash processes.
# ============================================================

# Kill rpld
killall -9 rpld 2>/dev/null

# Kill mininet-related python processes (but not our orchestrator)
# NOTE: we do NOT pkill python here — net.stop() already handles cleanup.
# Killing python processes matching 'mininet' would kill the benchmark itself.

# Remove network namespaces
ip -all netns delete 2>/dev/null

# Remove virtual interfaces
for iface in $(ip link show 2>/dev/null | grep -oE '(sensor[0-9]+-pan[0-9]+|lowpan[0-9]+|wpan[0-9]+)' | sort -u); do
    ip link delete "$iface" 2>/dev/null
done

# Clean tmp files
rm -f /tmp/vconn* /tmp/vlogs* /tmp/*.out 2>/dev/null

# Remove 802.15.4 module
rmmod mac802154_hwsim 2>/dev/null

sleep 1
echo "cleanup done"
