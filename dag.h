/*
 *   Authors:
 *    Alexander Aring           <alex.aring@gmail.com>
 *
 *   HyMRPL extensions by Cassius Clay
 */

#ifndef __RPLD_DAG_H__
#define __RPLD_DAG_H__

#include <stdint.h>
#include <string.h>
#include <ev.h>

#include "buffer.h"
#include "list.h"
#include "tree.h"

struct peer {
        struct in6_addr addr;
        uint16_t rank;
        struct list list;
};

struct child {
        struct in6_addr addr;
        struct in6_addr from;
        struct list list;
};

struct dag_daoack {
        uint8_t dsn;
        struct list list;
};

struct dag {
        uint8_t version;
        uint8_t dtsn;
        uint8_t dsn;
        struct in6_addr dodagid;
        uint8_t mop;

        /* HyMRPL: functional profile of this node (HYMRPL_CLASS_S or HYMRPL_CLASS_N) */
        uint8_t node_class;

        struct t_root root;
        struct in6_prefix dest;

        uint16_t my_rank;
        struct peer *parent;

        struct in6_addr self;
        struct list_head childs;

        ev_tstamp trickle_t;
        ev_timer trickle_w;

        const struct iface *iface;
        const struct rpl *rpl;

        struct list_head pending_acks;
        struct list list;
};

struct rpl {
        uint8_t instance_id;
        struct list_head dags;
        struct list list;
};

struct dag *dag_create(struct iface *iface, uint8_t instanceid,
                       const struct in6_addr *dodagid,
                       uint16_t my_rank, uint8_t version,
                       uint8_t mop, const struct in6_prefix *dest);
void dag_free(struct dag *dag);
void dag_build_dio(struct dag *dag, struct safe_buffer *sb);
struct dag *dag_lookup(const struct iface *iface, uint8_t instance_id,
                       const struct in6_addr *dodagid);
void dag_process_dio(struct dag *dag);
struct peer *dag_peer_create(const struct in6_addr *addr);
void dag_build_dao(struct dag *dag, struct safe_buffer *sb);
void dag_build_dao_ack(struct dag *dag, struct safe_buffer *sb);
void dag_build_dis(struct safe_buffer *sb);
struct child *dag_lookup_child_or_create(struct dag *dag,
                                         const struct in6_addr *addr,
                                         const struct in6_addr *from);
bool dag_is_peer(const struct peer *peer, const struct in6_addr *addr);

#endif /* __RPLD_DAG_H__ */
