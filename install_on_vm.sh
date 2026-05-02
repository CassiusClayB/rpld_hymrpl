#!/bin/bash
# HyMRPL — Script to install kernel + rpld on the VM
# Run INSIDE the VM after copying the files
# Usage: bash install_on_vm.sh

set -e

echo "=== HyMRPL VM Installer ==="

# 1. Install compiled kernel
echo "[1/5] Installing kernel..."
if ls ~/linux-image-6.11*.deb 1>/dev/null 2>&1; then
    sudo dpkg -i ~/linux-image-6.11*.deb
    sudo dpkg -i ~/linux-headers-6.11*.deb 2>/dev/null || true
    sudo update-grub
    echo "Kernel installed. Reboot required."
else
    echo "ERROR: linux-image-6.11*.deb not found in ~/."
    echo "Copy the .deb files from the host machine first."
    exit 1
fi

# 2. Install rpld dependencies
echo "[2/5] Installing rpld dependencies..."
sudo apt update
sudo apt install -y meson ninja-build liblua5.3-dev libev-dev \
    libnl-3-dev libnl-genl-3-dev git tcpdump traceroute

# 3. Clone and prepare rpld
echo "[3/5] Cloning rpld..."
if [ ! -d ~/rpld ]; then
    git clone https://github.com/ramonfontes/rpld.git ~/rpld
fi

# 4. Copy HyMRPL configs
echo "[4/5] Installing configs..."
sudo mkdir -p /etc/rpld
if [ -d ~/rpld_hymrpl/test ]; then
    sudo cp ~/rpld_hymrpl/test/lowpan0_hybrid.conf /etc/rpld/
    sudo cp ~/rpld_hymrpl/test/lowpan_hybrid_classS.conf /etc/rpld/
    sudo cp ~/rpld_hymrpl/test/lowpan_hybrid_classN.conf /etc/rpld/
fi

echo "[5/5] Done!"
echo ""
echo "Next steps:"
echo "  1. sudo reboot  (to use the new kernel)"
echo "  2. After reboot, verify: grep RPL_LWTUNNEL /boot/config-\$(uname -r)"
echo "  3. Apply HyMRPL patches to rpld and compile:"
echo "     cd ~/rpld && meson build && ninja -C build"
echo "  4. Test: sudo python3 ~/rpld_hymrpl/test/hymrpl_topology.py"
