#!/bin/bash
# HyMRPL — Script para compilar kernel 6.11 com RPL SRH habilitado
# Rodar na máquina host (não na VM)
# Uso: bash build_kernel.sh

set -e

KERNEL_VERSION="6.11"
KERNEL_URL="https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${KERNEL_VERSION}.tar.xz"
BUILD_DIR="$HOME/linux-${KERNEL_VERSION}"

echo "=== HyMRPL Kernel Builder ==="
echo "Kernel: ${KERNEL_VERSION}"
echo "Build dir: ${BUILD_DIR}"
echo ""

# 1. Instalar dependências
echo "[1/7] Instalando dependências..."
sudo apt update
sudo apt install -y build-essential libncurses-dev bison flex libssl-dev \
    libelf-dev bc dwarves wget

# 2. Baixar kernel
if [ ! -f "$HOME/linux-${KERNEL_VERSION}.tar.xz" ]; then
    echo "[2/7] Baixando kernel ${KERNEL_VERSION}..."
    wget -P "$HOME" "${KERNEL_URL}"
else
    echo "[2/7] Kernel já baixado, pulando..."
fi

# 3. Extrair
if [ ! -d "${BUILD_DIR}" ]; then
    echo "[3/7] Extraindo..."
    tar xf "$HOME/linux-${KERNEL_VERSION}.tar.xz" -C "$HOME"
else
    echo "[3/7] Já extraído, pulando..."
fi

cd "${BUILD_DIR}"

# 4. Configurar
echo "[4/7] Configurando kernel..."
cp /boot/config-$(uname -r) .config

# Habilitar RPL SRH (o módulo principal que precisamos)
scripts/config --enable CONFIG_IPV6_RPL_LWTUNNEL

# Garantir 6LoWPAN e IEEE 802.15.4
scripts/config --module CONFIG_6LOWPAN
scripts/config --module CONFIG_IEEE802154
scripts/config --module CONFIG_IEEE802154_6LOWPAN
scripts/config --module CONFIG_IEEE802154_HWSIM
scripts/config --module CONFIG_MAC802154
scripts/config --enable CONFIG_LWTUNNEL

# Limpar certificados Ubuntu (causa erro de build)
scripts/config --disable SYSTEM_TRUSTED_KEYS
scripts/config --disable SYSTEM_REVOCATION_KEYS
scripts/config --set-str SYSTEM_TRUSTED_KEYS ""
scripts/config --set-str SYSTEM_REVOCATION_KEYS ""

# Resolver dependências
make olddefconfig

# Verificar
echo ""
echo "Verificando configuração:"
grep CONFIG_IPV6_RPL_LWTUNNEL .config
grep CONFIG_6LOWPAN= .config
grep CONFIG_IEEE802154= .config
grep CONFIG_IEEE802154_HWSIM .config
echo ""

# 5. Compilar como .deb
echo "[5/7] Compilando kernel (isso demora 15-30 min)..."
CORES=$(nproc)
echo "Usando ${CORES} cores..."
make -j${CORES} bindeb-pkg

# 6. Listar pacotes gerados
echo ""
echo "[6/7] Pacotes gerados:"
ls -lh "$HOME"/linux-image-${KERNEL_VERSION}*.deb 2>/dev/null
ls -lh "$HOME"/linux-headers-${KERNEL_VERSION}*.deb 2>/dev/null

echo ""
echo "[7/7] Pronto!"
echo ""
echo "Para instalar na VM, copie os .deb e rode:"
echo "  scp ~/linux-image-${KERNEL_VERSION}*.deb ~/linux-headers-${KERNEL_VERSION}*.deb wifi@IP_DA_VM:~/"
echo "  ssh wifi@IP_DA_VM"
echo "  sudo dpkg -i linux-image-${KERNEL_VERSION}*.deb linux-headers-${KERNEL_VERSION}*.deb"
echo "  sudo update-grub"
echo "  sudo reboot"
