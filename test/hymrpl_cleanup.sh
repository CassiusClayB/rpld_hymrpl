#!/bin/bash
# ============================================================
# HyMRPL — Safe cleanup (replaces mn -c)
# Does NOT kill bash processes.
# ============================================================

# Kill rpld
killall -9 rpld 2>/dev/null

# Kill mininet-related python processes (but not our orchestrator)
pkill -9 -f "python3.*hymrpl_topology\|python3.*mn_wifi\|python3.*mininet" 2>/dev/null

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
