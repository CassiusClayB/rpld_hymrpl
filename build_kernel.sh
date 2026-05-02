#!/bin/bash
# HyMRPL — Script to compile kernel 6.11 with RPL SRH enabled
# Run on the host machine (not on the VM)
# Usage: bash build_kernel.sh

set -e

KERNEL_VERSION="6.11"
KERNEL_URL="https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${KERNEL_VERSION}.tar.xz"
BUILD_DIR="$HOME/linux-${KERNEL_VERSION}"

echo "=== HyMRPL Kernel Builder ==="
echo "Kernel: ${KERNEL_VERSION}"
echo "Build dir: ${BUILD_DIR}"
echo ""

# 1. Install dependencies
echo "[1/7] Installing dependencies..."
sudo apt update
sudo apt install -y build-essential libncurses-dev bison flex libssl-dev \
    libelf-dev bc dwarves wget

# 2. Download kernel
if [ ! -f "$HOME/linux-${KERNEL_VERSION}.tar.xz" ]; then
    echo "[2/7] Downloading kernel ${KERNEL_VERSION}..."
    wget -P "$HOME" "${KERNEL_URL}"
else
    echo "[2/7] Kernel already downloaded, skipping..."
fi

# 3. Extract
if [ ! -d "${BUILD_DIR}" ]; then
    echo "[3/7] Extracting..."
    tar xf "$HOME/linux-${KERNEL_VERSION}.tar.xz" -C "$HOME"
else
    echo "[3/7] Already extracted, skipping..."
fi

cd "${BUILD_DIR}"

# 4. Configure
echo "[4/7] Configuring kernel..."
cp /boot/config-$(uname -r) .config

# Enable RPL SRH (the main module we need)
scripts/config --enable CONFIG_IPV6_RPL_LWTUNNEL

# Ensure 6LoWPAN and IEEE 802.15.4
scripts/config --module CONFIG_6LOWPAN
scripts/config --module CONFIG_IEEE802154
scripts/config --module CONFIG_IEEE802154_6LOWPAN
scripts/config --module CONFIG_IEEE802154_HWSIM
scripts/config --module CONFIG_MAC802154
scripts/config --enable CONFIG_LWTUNNEL

# Clean Ubuntu certificates (causes build errors)
scripts/config --disable SYSTEM_TRUSTED_KEYS
scripts/config --disable SYSTEM_REVOCATION_KEYS
scripts/config --set-str SYSTEM_TRUSTED_KEYS ""
scripts/config --set-str SYSTEM_REVOCATION_KEYS ""

# Resolve dependencies
make olddefconfig

# Verify
echo ""
echo "Verifying configuration:"
grep CONFIG_IPV6_RPL_LWTUNNEL .config
grep CONFIG_6LOWPAN= .config
grep CONFIG_IEEE802154= .config
grep CONFIG_IEEE802154_HWSIM .config
echo ""

# 5. Compile as .deb
echo "[5/7] Compiling kernel (this takes 15-30 min)..."
CORES=$(nproc)
echo "Using ${CORES} cores..."
make -j${CORES} bindeb-pkg

# 6. List generated packages
echo ""
echo "[6/7] Generated packages:"
ls -lh "$HOME"/linux-image-${KERNEL_VERSION}*.deb 2>/dev/null
ls -lh "$HOME"/linux-headers-${KERNEL_VERSION}*.deb 2>/dev/null

echo ""
echo "[7/7] Done!"
echo ""
echo "To install on the VM, copy the .deb files and run:"
echo "  scp ~/linux-image-${KERNEL_VERSION}*.deb ~/linux-headers-${KERNEL_VERSION}*.deb wifi@VM_IP:~/"
echo "  ssh wifi@VM_IP"
echo "  sudo dpkg -i linux-image-${KERNEL_VERSION}*.deb linux-headers-${KERNEL_VERSION}*.deb"
echo "  sudo update-grub"
echo "  sudo reboot"
