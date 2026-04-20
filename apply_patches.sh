#!/bin/bash
# HyMRPL — Aplica as modificações no rpld original
# Rodar dentro da VM, na pasta do rpld clonado
# Uso: bash apply_patches.sh /caminho/para/rpld /caminho/para/rpld_hymrpl

set -e

RPLD_DIR="${1:-$HOME/rpld}"
HYMRPL_DIR="${2:-$HOME/rpld_hymrpl}"

if [ ! -f "${RPLD_DIR}/rpld.c" ]; then
    echo "ERRO: rpld não encontrado em ${RPLD_DIR}"
    exit 1
fi

echo "=== Aplicando patches HyMRPL no rpld ==="
echo "rpld: ${RPLD_DIR}"
echo "hymrpl: ${HYMRPL_DIR}"
echo ""

# Backup dos originais
echo "[1/6] Backup dos originais..."
mkdir -p "${RPLD_DIR}/backup_original"
cp "${RPLD_DIR}/rpl.h" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/dag.h" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/dag.c" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/config.h" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/config.c" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/process.c" "${RPLD_DIR}/backup_original/"

# Substituir arquivos completos
echo "[2/6] Substituindo rpl.h..."
cp "${HYMRPL_DIR}/rpl.h" "${RPLD_DIR}/rpl.h"

echo "[3/6] Substituindo dag.h..."
cp "${HYMRPL_DIR}/dag.h" "${RPLD_DIR}/dag.h"

echo "[4/6] Substituindo config.h..."
cp "${HYMRPL_DIR}/config.h" "${RPLD_DIR}/config.h"

echo "[5/6] Substituindo process.c..."
cp "${HYMRPL_DIR}/process.c" "${RPLD_DIR}/process.c"

# Aplicar patches manuais no dag.c e config.c
echo "[6/6] Aplicando patches no dag.c e config.c..."

# --- dag.c: adicionar node_class init ---
sed -i 's/dag->mop = mop;/dag->mop = mop;\n\tdag->node_class = HYMRPL_CLASS_S;  \/* HyMRPL: default storing-like *\//' \
    "${RPLD_DIR}/dag.c"

# --- dag.c: substituir switch no dag_build_dao ---
# Trocar o switch case de build_dao para incluir RPL_DIO_HYBRID
sed -i '/switch (dag->mop) {/{
N;N;N;N;N;N;N;N;N;N;N;N
/case RPL_DIO_STORING_NO_MULTICAST.*case RPL_DIO_STORING_MULTICAST.*list_for_each_entry(child, &dag->childs.*default:/c\
\tswitch (dag->mop) {\
\tcase RPL_DIO_STORING_NO_MULTICAST:\
\tcase RPL_DIO_STORING_MULTICAST:\
\t\tlist_for_each_entry(child, \&dag->childs, list) {\
\t\t\tprefix.prefix = child->addr;\
\t\t\tprefix.len = 128;\
\t\t\tappend_target(\&prefix, sb);\
\t\t}\
\t\tbreak;\
\tcase RPL_DIO_HYBRID:\
\t\tif (dag->node_class == HYMRPL_CLASS_S) {\
\t\t\tlist_for_each_entry(child, \&dag->childs, list) {\
\t\t\t\tprefix.prefix = child->addr;\
\t\t\t\tprefix.len = 128;\
\t\t\t\tappend_target(\&prefix, sb);\
\t\t\t}\
\t\t\tflog(LOG_INFO, "HYMRPL: class-S DAO includes child targets");\
\t\t} else {\
\t\t\tflog(LOG_INFO, "HYMRPL: class-N DAO (own target only)");\
\t\t}\
\t\tbreak;\
\tdefault:
}' "${RPLD_DIR}/dag.c"

# --- config.c: adicionar leitura de node_class no config_load_dags ---
sed -i '/mop = RPL_DIO_STORING_NO_MULTICAST;/{
N;N;
s/lua_pop(L, 1);/lua_pop(L, 1);\
\n\t\t\t\t\/* HyMRPL: read node_class *\/\
\t\t\t\tuint8_t dag_node_class = HYMRPL_CLASS_S;\
\t\t\t\tlua_getfield(L, -1, "node_class");\
\t\t\t\tif (lua_isstring(L, -1)) {\
\t\t\t\t\tconst char *cls = lua_tostring(L, -1);\
\t\t\t\t\tif (cls[0] == '"'"'N'"'"' || cls[0] == '"'"'n'"'"')\
\t\t\t\t\t\tdag_node_class = HYMRPL_CLASS_N;\
\t\t\t\t}\
\t\t\t\tlua_pop(L, 1);/
}' "${RPLD_DIR}/config.c"

# --- config.c: set node_class after dag_create ---
sed -i 's/if (!dag)/if (dag) dag->node_class = dag_node_class;\n\t\t\t\tif (!dag)/' \
    "${RPLD_DIR}/config.c"

# --- config.c: adicionar leitura de node_class na iface ---
sed -i '/iface->mop = lua_tonumber(L, -1);/{
N;
s/lua_pop(L, 1);/lua_pop(L, 1);\
\n\t\t\/* HyMRPL: read node_class from iface config *\/\
\t\tlua_getfield(L, -1, "node_class");\
\t\tif (lua_isstring(L, -1)) {\
\t\t\tconst char *cls = lua_tostring(L, -1);\
\t\t\tif (cls[0] == '"'"'N'"'"' || cls[0] == '"'"'n'"'"')\
\t\t\t\tiface->node_class = HYMRPL_CLASS_N;\
\t\t\telse\
\t\t\t\tiface->node_class = HYMRPL_CLASS_S;\
\t\t} else {\
\t\t\tiface->node_class = HYMRPL_CLASS_S;\
\t\t}\
\t\tlua_pop(L, 1);/
}' "${RPLD_DIR}/config.c"

echo ""
echo "=== Patches aplicados! ==="
echo ""
echo "NOTA: O patch rpld_parent_liveness.patch precisa ser aplicado"
echo "manualmente no rpld.c (ver instruções no arquivo do patch)."
echo "Ele adiciona detecção de parent liveness para reconvergência"
echo "em topologias mesh."
echo ""
echo "Compilar:"
echo "  cd ${RPLD_DIR}"
echo "  rm -rf build"
echo "  meson build"
echo "  ninja -C build"
echo ""
echo "Testar:"
echo "  sudo ./build/rpld -c /etc/rpld/lowpan0_hybrid.conf"
