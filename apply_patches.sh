#!/bin/bash
# HyMRPL — Apply modifications to the original rpld
# Run inside the VM, in the cloned rpld directory
# Usage: bash apply_patches.sh /path/to/rpld /path/to/rpld_hymrpl

set -e

RPLD_DIR="${1:-$HOME/rpld}"
HYMRPL_DIR="${2:-$HOME/rpld_hymrpl}"

if [ ! -f "${RPLD_DIR}/rpld.c" ]; then
    echo "ERROR: rpld not found at ${RPLD_DIR}"
    exit 1
fi

echo "=== Applying HyMRPL patches to rpld ==="
echo "rpld: ${RPLD_DIR}"
echo "hymrpl: ${HYMRPL_DIR}"
echo ""

# Back up originals
echo "[1/6] Backing up originals..."
mkdir -p "${RPLD_DIR}/backup_original"
cp "${RPLD_DIR}/rpl.h" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/dag.h" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/dag.c" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/config.h" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/config.c" "${RPLD_DIR}/backup_original/"
cp "${RPLD_DIR}/process.c" "${RPLD_DIR}/backup_original/"

# Replace complete files
echo "[2/6] Replacing rpl.h..."
cp "${HYMRPL_DIR}/rpl.h" "${RPLD_DIR}/rpl.h"

echo "[3/6] Replacing dag.h..."
cp "${HYMRPL_DIR}/dag.h" "${RPLD_DIR}/dag.h"

echo "[4/6] Replacing config.h..."
cp "${HYMRPL_DIR}/config.h" "${RPLD_DIR}/config.h"

echo "[5/6] Replacing process.c..."
cp "${HYMRPL_DIR}/process.c" "${RPLD_DIR}/process.c"

# Apply manual patches to dag.c and config.c
echo "[6/6] Applying patches to dag.c and config.c..."

# --- dag.c: add node_class init ---
sed -i 's/dag->mop = mop;/dag->mop = mop;\n\tdag->node_class = HYMRPL_CLASS_S;  \/* HyMRPL: default storing-like *\/\n\tdag->parent_last_seen = ev_now(EV_DEFAULT);  \/* HyMRPL: avoid false liveness timeout *\//' \
    "${RPLD_DIR}/dag.c"

# --- dag.c: replace switch in dag_build_dao ---
# Replace the build_dao switch case to include RPL_DIO_HYBRID
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

# --- config.c: add node_class reading in config_load_dags ---
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

# --- config.c: add node_class reading in iface ---
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
echo "=== Patches applied! ==="
echo ""
echo "NOTE: The rpld_parent_liveness.patch needs to be applied"
echo "manually to rpld.c (see instructions in the patch file)."
echo "It adds parent liveness detection for reconvergence"
echo "in mesh topologies."
echo ""
echo "Compile:"
echo "  cd ${RPLD_DIR}"
echo "  rm -rf build"
echo "  meson build"
echo "  ninja -C build"
echo ""
echo "Test:"
echo "  sudo ./build/rpld -c /etc/rpld/lowpan0_hybrid.conf"
