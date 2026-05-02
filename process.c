/*
 *   Authors:
 *    Alexander Aring           <alex.aring@gmail.com>
 *
 *   HyMRPL extensions by Cassius Clay
 *   - MOP=6 hybrid mode: Classe S (storing-like) and Classe N (non-storing-like)
 *     coexist in the same DODAG. The node_class field determines local behavior.
 */

#include <linux/ipv6.h>
#include <netinet/icmp6.h>

#include "process.h"
#include "netlink.h"
#include "send.h"
#include "dag.h"
#include "log.h"
#include "rpl.h"

static void process_dio(int sock, struct iface *iface, const void *msg,
                        size_t len, struct sockaddr_in6 *addr)
{
        const struct nd_rpl_dio *dio = msg;
        const struct rpl_dio_destprefix *diodp;
        char addr_str[INET6_ADDRSTRLEN];
        struct in6_prefix pfx;
        struct dag *dag;
        uint16_t rank;
        int rc;

        if (len < sizeof(*dio)) {
                flog(LOG_INFO, "dio length mismatch, drop");
                return;
        }
        len -= sizeof(*dio);

        addrtostr(&addr->sin6_addr, addr_str, sizeof(addr_str));
        flog(LOG_INFO, "received dio %s", addr_str);

        dag = dag_lookup(iface, dio->rpl_instanceid, &dio->rpl_dagid);
        if (dag) {
                if (dag->my_rank == 1)
                        return;
        } else {
                diodp = (struct rpl_dio_destprefix *)
                         (((unsigned char *)msg) + sizeof(*dio));

                if (len < sizeof(*diodp) - 16) {
                        flog(LOG_INFO, "diodp length mismatch, drop");
                        return;
                }
                len -= sizeof(*diodp) - 16;

                if (diodp->rpl_dio_type != 0x3) {
                        flog(LOG_INFO, "we assume diodp - not supported, drop");
                        return;
                }

                if (len < bits_to_bytes(diodp->rpl_dio_prefixlen)) {
                        flog(LOG_INFO, "diodp prefix length mismatch, drop");
                        return;
                }
                len -= bits_to_bytes(diodp->rpl_dio_prefixlen);

                pfx.len = diodp->rpl_dio_prefixlen;
                memcpy(&pfx.prefix, &diodp->rpl_dio_prefix,
                       bits_to_bytes(pfx.len));

                flog(LOG_INFO, "received but no dag found %s", addr_str);
                dag = dag_create(iface, dio->rpl_instanceid,
                                 &dio->rpl_dagid, UINT16_MAX, dio->rpl_version,
                                 RPL_DIO_MOP(dio->rpl_mopprf), &pfx);
                if (!dag)
                        return;

                /* HyMRPL: propagate node_class from iface config to dag */
                dag->node_class = iface->node_class;

                addrtostr(&dio->rpl_dagid, addr_str, sizeof(addr_str));
                flog(LOG_INFO, "created dag %s (mop=%d, class=%s)",
                     addr_str, dag->mop,
                     dag->node_class == HYMRPL_CLASS_S ? "S" : "N");
        }

        flog(LOG_INFO, "process dio %s", addr_str);

        rank = ntohs(dio->rpl_dagrank);
        if (!dag->parent) {
                dag->parent = dag_peer_create(&addr->sin6_addr);
                if (!dag->parent)
                        return;
        }

        /*
         * HyMRPL: track parent liveness.
         * If the DIO comes from our current parent, refresh the
         * last-seen timestamp.  If the parent was invalidated
         * (rank == UINT16_MAX) by the liveness timer, accept any
         * neighbor with a valid rank as the new parent.
         */
        if (dag_is_peer(dag->parent, &addr->sin6_addr)) {
                dag->parent_last_seen = ev_now(EV_DEFAULT);
        } else if (dag->parent->rank == UINT16_MAX) {
                /* Parent was invalidated — adopt this neighbor */
                char old_str[INET6_ADDRSTRLEN];
                addrtostr(&dag->parent->addr, old_str, sizeof(old_str));
                flog(LOG_INFO,
                     "HYMRPL: adopting new parent %s (old %s was dead)",
                     addr_str, old_str);
                memcpy(&dag->parent->addr, &addr->sin6_addr,
                       sizeof(dag->parent->addr));
                dag->parent->rank = UINT16_MAX; /* will be set below */
                dag->parent_last_seen = ev_now(EV_DEFAULT);
        }

        if (rank > dag->parent->rank)
                return;

        rc = nl_add_route_default(dag->iface->ifindex, &dag->parent->addr);
        flog(LOG_INFO, "default route %d %s", rc, strerror(errno));

        dag->parent->rank = rank;
        dag->my_rank = rank + 1;

        dag_process_dio(dag);

        /*
         * DAO destination depends on MOP and node class:
         * - Storing (MOP 2/3): send DAO to parent
         * - Non-Storing (MOP 1): send DAO to root (dodagid)
         * - Hybrid (MOP 6): always send DAO toward root so the root
         *   has full topology visibility. Classe S nodes also install
         *   local routes, but the DAO still reaches the root.
         */
        switch (dag->mop) {
        case RPL_DIO_STORING_NO_MULTICAST:
        case RPL_DIO_STORING_MULTICAST:
                send_dao(sock, &dag->parent->addr, dag);
                break;
        case RPL_DIO_NONSTORING:
                send_dao(sock, &dag->dodagid, dag);
                break;
        case RPL_DIO_HYBRID:
                /*
                 * HyMRPL: In hybrid mode, send DAO to BOTH parent and root.
                 * - DAO to parent: allows Classe S intermediate nodes to
                 *   install local downward routes (storing-like behavior).
                 * - DAO to root: allows the root to build the complete
                 *   source routing tree for Classe N paths.
                 * This dual-DAO approach ensures both routing paradigms
                 * work simultaneously in the same DODAG.
                 */
                send_dao(sock, &dag->parent->addr, dag);
                flog(LOG_INFO, "HYMRPL: sent DAO to parent (class=%s)",
                     dag->node_class == HYMRPL_CLASS_S ? "S" : "N");
                /* Also send to root if parent is not root */
                if (dag->parent->rank > 1) {
                        send_dao(sock, &dag->dodagid, dag);
                        flog(LOG_INFO, "HYMRPL: sent DAO to root");
                }
                break;
        default:
                break;
        }
}


static void dag_insert_source_routes(uint32_t ifindex, const struct t_node *node)
{
        char addr_str[INET6_ADDRSTRLEN];
        struct list_head path = {};
        struct t_path *p;

        t_path(node, &path);

        nl_add_source_routes(ifindex, &path);
        list_for_each_entry(p, &path, list) {
                addrtostr(&p->addr, addr_str, sizeof(addr_str));
                flog(LOG_INFO, "HYMRPL source route seg %s", addr_str);
                addrtostr(&p->target, addr_str, sizeof(addr_str));
                flog(LOG_INFO, "HYMRPL source route target %s", addr_str);
        }

        t_path_free(&path);
}

static void process_dao(int sock, struct iface *iface, const void *msg,
                        size_t len, struct sockaddr_in6 *addr)
{
        const struct rpl_dao_transit *transit = NULL;
        const struct rpl_dao_target *target = NULL;
        const struct nd_rpl_dao *dao = msg;
        char addr_str[INET6_ADDRSTRLEN];
        const struct nd_rpl_opt *opt;
        const unsigned char *p;
        struct child *child;
        struct t_node *n;
        struct dag *dag;
        int optlen;
        int rc;

        if (len < sizeof(*dao)) {
                flog(LOG_INFO, "dao length mismatch, drop");
                return;
        }
        len -= sizeof(*dao);

        addrtostr(&addr->sin6_addr, addr_str, sizeof(addr_str));
        flog(LOG_INFO, "received dao %s", addr_str);

        dag = dag_lookup(iface, dao->rpl_instanceid, &dao->rpl_dagid);
        if (!dag) {
                addrtostr(&dao->rpl_dagid, addr_str, sizeof(addr_str));
                flog(LOG_INFO, "can't find dag %s", addr_str);
                return;
        }

        p = msg;
        p += sizeof(*dao);
        optlen = len;
        /*
         * Parse DAO options, collecting ALL target/transit pairs.
         * A DAO may contain multiple RPL Target options followed by
         * a Transit Information option. Each target paired with the
         * subsequent transit must be processed individually.
         *
         * RFC6550 Section 6.7.7: "One or more RPL Target options MUST
         * be followed by one or more Transit Information options."
         */
        #define MAX_DAO_TARGETS 16
        const struct rpl_dao_target *targets[MAX_DAO_TARGETS];
        int target_count = 0;

        flog(LOG_INFO, "dao optlen %d", optlen);
        while (optlen > 0) {
                opt = (const struct nd_rpl_opt *)p;

                if (optlen < sizeof(*opt)) {
                        flog(LOG_INFO, "rpl opt length mismatch, drop");
                        return;
                }

                flog(LOG_INFO, "dao opt %d", opt->type);
                switch (opt->type) {
                case RPL_DAO_TRANSITINFO:
                        transit = (const struct rpl_dao_transit *)p;
                        if (optlen < sizeof(*opt)) {
                                flog(LOG_INFO, "rpl transit length mismatch, drop");
                                return;
                        }
                        addrtostr(&transit->parent, addr_str, sizeof(addr_str));
                        flog(LOG_INFO, "dao transit %s", addr_str);
                        break;
                case RPL_DAO_RPLTARGET:
                        target = (const struct rpl_dao_target *)p;
                        if (optlen < sizeof(*opt)) {
                                flog(LOG_INFO, "rpl target length mismatch, drop");
                                return;
                        }
                        addrtostr(&target->rpl_dao_prefix, addr_str, sizeof(addr_str));
                        flog(LOG_INFO, "dao target %s", addr_str);
                        dag_lookup_child_or_create(dag,
                                                   &target->rpl_dao_prefix,
                                                   &addr->sin6_addr);
                        /* Save target for later source route processing */
                        if (target_count < MAX_DAO_TARGETS)
                                targets[target_count++] = target;
                        break;
                default:
                        break;
                }

                optlen -= (2 + opt->len);
                p += (2 + opt->len);
                flog(LOG_INFO, "dao optlen %d", optlen);
        }

        /*
         * Route installation depends on MOP and node class:
         *
         * Storing (MOP 2/3): install downward routes via Netlink for all children
         * Non-Storing (MOP 1): root builds source routing tree, installs SRH routes
         * Hybrid (MOP 6):
         *   - Root: build source routing tree for ALL targets in the DAO
         *   - Classe S (non-root): install downward routes via Netlink (storing-like)
         *   - Classe N (non-root): no local route installation
         */
        switch (dag->mop) {
        case RPL_DIO_STORING_NO_MULTICAST:
        case RPL_DIO_STORING_MULTICAST:
                list_for_each_entry(child, &dag->childs, list) {
                        rc = nl_add_route_via(dag->iface->ifindex, &child->addr,
                                              &child->from);
                        flog(LOG_INFO, "via route %d %s", rc, strerror(errno));
                }
                break;
        case RPL_DIO_NONSTORING:
                /*
                 * Process ALL targets with the transit info.
                 * Fix: the original rpld only processed the last target
                 * in the DAO (single 'target' variable overwritten in the
                 * parse loop). This iterates over all collected targets,
                 * which is required when a DAO carries multiple RPL Target
                 * options (RFC6550 Section 6.7.7).
                 */
                if (transit) {
                        int i;
                        for (i = 0; i < target_count; i++) {
                                n = t_insert(&dag->root, &transit->parent,
                                             &addr->sin6_addr,
                                             &targets[i]->rpl_dao_prefix);
                                if (n)
                                        dag_insert_source_routes(dag->iface->ifindex, n);
                        }
                }
                break;
        case RPL_DIO_HYBRID:
                if (dag->my_rank == 1) {
                        /*
                         * Root: always use source routing tree.
                         * Process ALL targets from the DAO, not just the last one.
                         * This is critical for Classe S nodes that aggregate
                         * child targets into their DAO messages.
                         */
                        if (transit) {
                                int i;
                                for (i = 0; i < target_count; i++) {
                                        n = t_insert(&dag->root, &transit->parent,
                                                     &addr->sin6_addr,
                                                     &targets[i]->rpl_dao_prefix);
                                        if (n) {
                                                dag_insert_source_routes(
                                                        dag->iface->ifindex, n);
                                                addrtostr(&targets[i]->rpl_dao_prefix,
                                                          addr_str, sizeof(addr_str));
                                                flog(LOG_INFO,
                                                     "HYMRPL: root source route for %s",
                                                     addr_str);
                                        }
                                }
                                flog(LOG_INFO,
                                     "HYMRPL: root processed %d targets from DAO",
                                     target_count);
                        }
                } else if (dag->node_class == HYMRPL_CLASS_S) {
                        /* Classe S (non-root): install local downward routes */
                        list_for_each_entry(child, &dag->childs, list) {
                                rc = nl_add_route_via(dag->iface->ifindex,
                                                      &child->addr, &child->from);
                                flog(LOG_INFO, "HYMRPL class-S: via route %d %s",
                                     rc, strerror(errno));
                        }
                } else {
                        /* Classe N (non-root): no local route installation */
                        flog(LOG_INFO, "HYMRPL class-N: skipping local route install");
                }
                break;
        }

        flog(LOG_INFO, "process dao %s", addr_str);
        send_dao_ack(sock, &addr->sin6_addr, dag);
}

static void process_daoack(int sock, struct iface *iface, const void *msg,
                           size_t len, struct sockaddr_in6 *addr)
{
        const struct nd_rpl_daoack *daoack = msg;
        char addr_str[INET6_ADDRSTRLEN];
        struct dag *dag;

        if (len < sizeof(*daoack)) {
                flog(LOG_INFO, "rpl daoack length mismatch, drop");
                return;
        }

        addrtostr(&addr->sin6_addr, addr_str, sizeof(addr_str));
        flog(LOG_INFO, "received daoack %s", addr_str);

        dag = dag_lookup(iface, daoack->rpl_instanceid, &daoack->rpl_dagid);
        if (!dag) {
                addrtostr(&daoack->rpl_dagid, addr_str, sizeof(addr_str));
                flog(LOG_INFO, "can't find dag %s", addr_str);
                return;
        }
}

static void process_dis(int sock, struct iface *iface, const void *msg,
                        size_t len, struct sockaddr_in6 *addr)
{
        char addr_str[INET6_ADDRSTRLEN];
        struct rpl *rpl;
        struct dag *dag;

        addrtostr(&addr->sin6_addr, addr_str, sizeof(addr_str));
        flog(LOG_INFO, "received dis %s", addr_str);

        list_for_each_entry(rpl, &iface->rpls, list) {
                list_for_each_entry(dag, &rpl->dags, list)
                        send_dio(sock, dag);
        }
}

void process(int sock, struct list_head *ifaces, unsigned char *msg,
             int len, struct sockaddr_in6 *addr, struct in6_pktinfo *pkt_info,
             int hoplimit)
{
        char addr_str[INET6_ADDRSTRLEN];
        char if_namebuf[IFNAMSIZ] = {""};
        char *if_name = if_indextoname(pkt_info->ipi6_ifindex, if_namebuf);
        if (!if_name)
                if_name = "unknown interface";

        dlog(LOG_DEBUG, 4, "%s received a packet", if_name);
        addrtostr(&addr->sin6_addr, addr_str, sizeof(addr_str));

        if (!pkt_info) {
                flog(LOG_WARNING, "%s received packet with no pkt_info from %s!",
                     if_name, addr_str);
                return;
        }

        if (len < 4) {
                flog(LOG_WARNING, "%s received icmpv6 packet with invalid length (%d) from %s",
                     if_name, len, addr_str);
                return;
        }
        len -= 4;

        struct icmp6_hdr *icmph = (struct icmp6_hdr *)msg;
        struct iface *iface = iface_find_by_ifindex(ifaces, pkt_info->ipi6_ifindex);
        if (!iface) {
                dlog(LOG_WARNING, 4, "%s received icmpv6 RS/RA packet on an unknown interface with index %d",
                     if_name, pkt_info->ipi6_ifindex);
                return;
        }

        if (icmph->icmp6_type != ND_RPL_MESSAGE) {
                flog(LOG_ERR, "%s icmpv6 filter failed", if_name);
                return;
        }

        switch (icmph->icmp6_code) {
        case ND_RPL_DAG_IS:
                process_dis(sock, iface, &icmph->icmp6_dataun, len, addr);
                break;
        case ND_RPL_DAG_IO:
                process_dio(sock, iface, &icmph->icmp6_dataun, len, addr);
                break;
        case ND_RPL_DAO:
                process_dao(sock, iface, &icmph->icmp6_dataun, len, addr);
                break;
        case ND_RPL_DAO_ACK:
                process_daoack(sock, iface, &icmph->icmp6_dataun, len, addr);
                break;
        default:
                flog(LOG_ERR, "%s received unsupported RPL code 0x%02x",
                     if_name, icmph->icmp6_code);
                break;
        }
}
