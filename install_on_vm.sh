#!/bin/bash
# HyMRPL — Script para instalar kernel + rpld na VM
# Rodar DENTRO da VM após copiar os arquivos
# Uso: bash install_on_vm.sh

set -e

echo "=== HyMRPL VM Installer ==="

# 1. Instalar kernel compilado
echo "[1/5] Instalando kernel..."
if ls ~/linux-image-6.11*.deb 1>/dev/null 2>&1; then
    sudo dpkg -i ~/linux-image-6.11*.deb
    sudo dpkg -i ~/linux-headers-6.11*.deb 2>/dev/null || true
    sudo update-grub
    echo "Kernel instalado. Reboot necessário."
else
    echo "ERRO: linux-image-6.11*.deb não encontrado em ~/."
    echo "Copie os .deb da máquina host primeiro."
    exit 1
fi

# 2. Instalar dependências do rpld
echo "[2/5] Instalando dependências do rpld..."
sudo apt update
sudo apt install -y meson ninja-build liblua5.3-dev libev-dev \
    libnl-3-dev libnl-genl-3-dev git tcpdump traceroute

# 3. Clonar e preparar rpld
echo "[3/5] Clonando rpld..."
if [ ! -d ~/rpld ]; then
    git clone https://github.com/ramonfontes/rpld.git ~/rpld
fi

# 4. Copiar configs HyMRPL
echo "[4/5] Instalando configs..."
sudo mkdir -p /etc/rpld
if [ -d ~/rpld_hymrpl/test ]; then
    sudo cp ~/rpld_hymrpl/test/lowpan0_hybrid.conf /etc/rpld/
    sudo cp ~/rpld_hymrpl/test/lowpan_hybrid_classS.conf /etc/rpld/
    sudo cp ~/rpld_hymrpl/test/lowpan_hybrid_classN.conf /etc/rpld/
fi

echo "[5/5] Pronto!"
echo ""
echo "Próximos passos:"
echo "  1. sudo reboot  (para usar o novo kernel)"
echo "  2. Após reboot, verificar: grep RPL_LWTUNNEL /boot/config-\$(uname -r)"
echo "  3. Aplicar patches do HyMRPL no rpld e compilar:"
echo "     cd ~/rpld && meson build && ninja -C build"
echo "  4. Testar: sudo python3 ~/rpld_hymrpl/test/hymrpl_topology.py"
