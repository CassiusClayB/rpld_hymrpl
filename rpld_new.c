/*
 *   Authors:
 *    Alexander Aring           <alex.aring@gmail.com>
 *
 *   HyMRPL extensions by Cassius Clay:
 *    - Secure FIFO for runtime class switching (HMAC-SHA256)
 *    - Integrated adaptive decision engine
 *    - Parent liveness detection
 *
 *   This software is Copyright 2019 by the above mentioned author(s),
 *   All Rights Reserved.
 */

#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <ev.h>

#include <libmnl/libmnl.h>

#include "process.h"
#include "netlink.h"
#include "helpers.h"
#include "socket.h"
#include "config.h"
#include "send.h"
#include "recv.h"
#include "log.h"
#include "rpl.h"
#include "hymrpl_adaptive.h"

#define VERSION "0.2.0-hymrpl"
#define PATH_RPLD_LOG "/var/log/rpld.log"
#define PATH_RPLD_CONF "/etc/rpld.conf"
#define LOG_FACILITY LOG_DAEMON

/* Parent liveness parameters */
#define PARENT_CHECK_INTERVAL   5.0
#define PARENT_TIMEOUT_FACTOR   8.0

static struct list_head ifaces;
static int sock;

/* HyMRPL: global adaptive and security state */
static struct hymrpl_adaptive g_adaptive;
static struct hymrpl_fifo_sec g_fifo_sec;
static bool g_adaptive_enabled = false;

static char usage_str[] = {
"\n"
"  -C, --config=PATH       Set the config file.  Default is /etc/rpld.conf\n"
"  -d, --debug=NUM         Set the debug level.  Values can be 1, 2, 3, 4 or 5.\n"
"  -h, --help              Show this help screen.\n"
"  -f, --facility=NUM      Set the logging facility.\n"
"  -l, --logfile=PATH      Set the log file.\n"
"  -m, --logmethod=X       Set method to: syslog, stderr, stderr_syslog, logfile,\n"
"  -v, --version           Print the version and quit.\n"
};

static void usage(FILE *o, const char *pname)
{
        fprintf(o, "usage: %s %s\n", pname, usage_str);
}

static void icmpv6_cb(EV_P_ ev_io *w, int revents)
{
        int len, hoplimit;
        struct sockaddr_in6 rcv_addr;
        struct in6_pktinfo *pkt_info = NULL;
        unsigned char msg[MSG_SIZE_RECV];
        unsigned char chdr[CMSG_SPACE(sizeof(struct in6_pktinfo)) + CMSG_SPACE(sizeof(int))];

        len = recv_rs_ra(sock, msg, &rcv_addr, &pkt_info, &hoplimit, chdr);
        if (len > 0 && pkt_info) {
                process(sock, &ifaces, msg, len, &rcv_addr, pkt_info, hoplimit);
        } else if (!pkt_info) {
                dlog(LOG_INFO, 4, "recv_rs_ra returned null pkt_info");
        } else if (len <= 0) {
                dlog(LOG_INFO, 4, "recv_rs_ra returned len <= 0: %d", len);
        }
}

static void trickle_cb(EV_P_ ev_timer *w, int revents)
{
        struct dag *dag = container_of(w, struct dag, trickle_w);

        flog(LOG_INFO, "send dio %p", dag->parent);
        send_dio(sock, dag);
}

static void sigint_cb(struct ev_loop *loop, ev_signal *w, int revents)
{
        ev_break(loop, EVBREAK_ALL);
}

static void send_dis_cb(EV_P_ ev_timer *w, int revents)
{
        struct iface *iface = container_of(w, struct iface, dis_w);
        struct rpl *rpl;
        int has_dag = 0;

        list_for_each_entry(rpl, &iface->rpls, list) {
                if (!list_empty(&rpl->dags)) {
                        has_dag = 1;
                        break;
                }
        }

        if (has_dag) {
                ev_timer_stop(loop, w);
                return;
        }

        send_dis(sock, iface);
}

/*
 * HyMRPL: Parent liveness check.
 * Invalidates parent if no DIO received within timeout.
 */
static void parent_check_cb(EV_P_ ev_timer *w, int revents)
{
        struct dag *dag = container_of(w, struct dag, parent_check_w);
        ev_tstamp now = ev_now(EV_A);
        ev_tstamp timeout;
        char addr_str[INET6_ADDRSTRLEN];

        if (dag->my_rank == 1)
                return;

        if (!dag->parent)
                return;

        if (dag->parent->rank == UINT16_MAX)
                return;

        timeout = PARENT_TIMEOUT_FACTOR * dag->trickle_t;
        if ((now - dag->parent_last_seen) < timeout)
                return;

        addrtostr(&dag->parent->addr, addr_str, sizeof(addr_str));
        flog(LOG_INFO,
             "HYMRPL: parent %s silent for %.1fs (timeout %.1fs), invalidating",
             addr_str, now - dag->parent_last_seen, timeout);

        dag->parent->rank = UINT16_MAX;
        dag->my_rank = UINT16_MAX;

        /* Flush stale routes */
        nl_del_route_via(dag->iface->ifindex, &dag->dest, NULL);

        /* Reset Trickle to accelerate DIO exchange */
        ev_timer_stop(EV_A_ &dag->trickle_w);
        ev_timer_set(&dag->trickle_w, 1.0, dag->trickle_t);
        ev_timer_start(EV_A_ &dag->trickle_w);

        /*
         * Note: the adaptive stability index is NOT updated here.
         * Invalidating a silent parent is only the detection step; the
         * actual parent change is counted once, when a new parent is
         * adopted in process_dio() via hymrpl_adaptive_notify_parent_change().
         * This avoids double-counting a single disruption.
         */

        flog(LOG_INFO,
             "HYMRPL: parent invalidated, accepting next DIO from any neighbor");
}

/*
 * HyMRPL: Periodic callback for FIFO check + adaptive decision.
 * Runs every 1 second.
 */
static void hymrpl_periodic_cb(EV_P_ ev_timer *w, int revents)
{
        struct iface *iface;
        struct rpl *rpl;
        struct dag *dag;
        uint8_t current_class = HYMRPL_CLASS_S;
        ev_tstamp now = ev_now(EV_A);

        /* 1. Check secure FIFO for external commands */
        int fifo_result = hymrpl_fifo_sec_check(&g_fifo_sec, &ifaces);
        if (fifo_result >= 0) {
                flog(LOG_INFO,
                     "HYMRPL: external FIFO command applied (class=%s)",
                     fifo_result == HYMRPL_CLASS_S ? "S" : "N");
                return;
        }

        /* 2. If adaptive engine is enabled, check for recommendations */
        if (!g_adaptive_enabled)
                return;

        /* Get current class from first dag */
        list_for_each_entry(iface, &ifaces, list) {
                list_for_each_entry(rpl, &iface->rpls, list) {
                        list_for_each_entry(dag, &rpl->dags, list) {
                                current_class = dag->node_class;
                                goto found_class;
                        }
                }
        }
found_class:

        /* Check adaptive recommendation */
        {
                int rec = hymrpl_adaptive_get_recommendation(
                        &g_adaptive, current_class);

                if (rec < 0)
                        return;

                /* Rate limiting (shared with FIFO) */
                if ((now - g_fifo_sec.last_switch_time) <
                    HYMRPL_MIN_SWITCH_INTERVAL) {
                        flog(LOG_INFO,
                             "HYMRPL adaptive: switch deferred (rate limit)");
                        return;
                }

                uint8_t new_class = (uint8_t)rec;
                list_for_each_entry(iface, &ifaces, list) {
                        iface->node_class = new_class;
                        list_for_each_entry(rpl, &iface->rpls, list) {
                                list_for_each_entry(dag, &rpl->dags, list) {
                                        if (dag->node_class != new_class) {
                                                flog(LOG_INFO,
                                                     "HYMRPL adaptive: switch "
                                                     "%s -> %s (score=%.3f)",
                                                     dag->node_class == HYMRPL_CLASS_S ? "S" : "N",
                                                     new_class == HYMRPL_CLASS_S ? "S" : "N",
                                                     g_adaptive.last_score);
                                                dag->node_class = new_class;
                                        }
                                }
                        }
                }
                g_fifo_sec.last_switch_time = now;
        }
}

/* TODO move somewhere else */
struct ev_loop *foo;
void dag_init_timer(struct dag *dag)
{
        ev_timer_init(&dag->trickle_w, trickle_cb,
                      dag->trickle_t, dag->trickle_t);
        ev_timer_start(foo, &dag->trickle_w);

        /* HyMRPL: start parent liveness timer */
        dag->parent_last_seen = ev_now(foo);
        ev_timer_init(&dag->parent_check_w, parent_check_cb,
                      PARENT_CHECK_INTERVAL, PARENT_CHECK_INTERVAL);
        ev_timer_start(foo, &dag->parent_check_w);
}

static int rpld_setup(struct ev_loop *loop, struct list_head *ifaces)
{
        struct iface *iface;
        struct rpl *rpl;
        struct dag *dag;

        list_for_each_entry(iface, ifaces, list) {
                ev_timer_init(&iface->dis_w, send_dis_cb, 1, 1);
                ev_timer_start(loop, &iface->dis_w);

                list_for_each_entry(rpl, &iface->rpls, list) {
                        list_for_each_entry(dag, &rpl->dags, list) {
                                ev_timer_init(&dag->trickle_w, trickle_cb,
                                              dag->trickle_t, dag->trickle_t);
                                ev_timer_start(loop, &dag->trickle_w);

                                /* HyMRPL: parent liveness timer */
                                dag->parent_last_seen = ev_now(loop);
                                ev_timer_init(&dag->parent_check_w,
                                              parent_check_cb,
                                              PARENT_CHECK_INTERVAL,
                                              PARENT_CHECK_INTERVAL);
                                ev_timer_start(loop, &dag->parent_check_w);

                                if (iface->dodag_root)
                                        nl_add_addr(iface->ifindex, &dag->dodagid);
                        }
                }
        }

        return 0;
}

static double read_trickle_config(const char *path)
{
    FILE *f;
    double val = -1.0;

    f = fopen(path, "r");
    if (!f)
        return -1.0;

    if (fscanf(f, "trickle_t=%lf", &val) != 1) {
        rewind(f);
        if (fscanf(f, "%lf", &val) != 1)
            val = -1.0;
    }

    fclose(f);
    return val;
}

static void reload_cb(struct ev_loop *loop, ev_signal *w, int revents)
{
    struct iface *iface;
    struct rpl *rpl;
    struct dag *dag;
    double new_trickle_t;
    const char *conf = "/tmp/rpld_trickle.conf";

    flog(LOG_INFO, "Received SIGUSR1: reloading trickle configuration from %s", conf);

    new_trickle_t = read_trickle_config(conf);
    if (new_trickle_t <= 0.0) {
        flog(LOG_WARNING, "Invalid trickle value in %s; ignoring", conf);
        return;
    }

    list_for_each_entry(iface, &ifaces, list) {
        list_for_each_entry(rpl, &iface->rpls, list) {
            list_for_each_entry(dag, &rpl->dags, list) {
                dag->trickle_t = new_trickle_t;
                ev_timer_stop(loop, &dag->trickle_w);
                ev_timer_set(&dag->trickle_w, dag->trickle_t, dag->trickle_t);
                ev_timer_start(loop, &dag->trickle_w);
                flog(LOG_INFO, "Updated dag %p trickle_t to %f", dag->parent, dag->trickle_t);
            }
        }
    }

    flog(LOG_INFO, "Trickle reload complete");
}


int main(int argc, char *argv[])
{
        char const *conf_path = PATH_RPLD_CONF;
        struct ev_loop *loop = EV_DEFAULT;
        char *logfile = PATH_RPLD_LOG;
        const char *pname = argv[0];
        int facility = LOG_FACILITY;
        int log_method = L_UNSPEC;
        ev_io sock_watcher;
        ev_signal exitsig;
        int opt;
        int rc;

        foo = loop;

        while ((opt = getopt(argc, argv, "C:m:f:l:d:h")) != -1) {
                switch (opt) {
                case 'C':
                        conf_path = optarg;
                        break;
                case 'm':
                        if (!strcmp(optarg, "syslog")) {
                                log_method = L_SYSLOG;
                        } else if (!strcmp(optarg, "stderr_syslog")) {
                                log_method = L_STDERR_SYSLOG;
                        } else if (!strcmp(optarg, "stderr")) {
                                log_method = L_STDERR;
                        } else if (!strcmp(optarg, "stderr_clean")) {
                                log_method = L_STDERR_CLEAN;
                        } else if (!strcmp(optarg, "logfile")) {
                                log_method = L_LOGFILE;
                        } else if (!strcmp(optarg, "none")) {
                                log_method = L_NONE;
                        } else {
                                fprintf(stderr, "%s: unknown log method: %s\n",
                                        pname, optarg);
                                exit(1);
                        }
                        break;
                case 'f':
                        facility = atoi(optarg);
                        break;
                case 'l':
                        logfile = optarg;
                        break;
                case 'd':
                        set_debuglevel(atoi(optarg));
                        break;
                case 'h':
                        usage(stdout, argv[0]);
                        exit(0);
                default:
                        usage(stderr, argv[0]);
                        exit(1);
                }
        }

        init_random_gen();
        ev_signal_init(&exitsig, sigint_cb, SIGINT);
        ev_signal_start(loop, &exitsig);

        static ev_signal reloadsig;
        ev_signal_init(&reloadsig, reload_cb, SIGUSR1);
        ev_signal_start(loop, &reloadsig);

        if (log_method == L_UNSPEC)
                log_method = L_STDERR;
        if (log_open(log_method, pname, logfile, facility) < 0) {
                perror("log_open");
                exit(1);
        }

        flog(LOG_INFO, "version %s started", VERSION);

        rc = netlink_open();
        if (rc == -1) {
                perror("mnl_socket_open");
                exit(1);
        }

        rc = config_load(conf_path, &ifaces);
        if (rc < 0) {
                netlink_close();
                flog(LOG_ERR, "Failed to parse config: %s", conf_path);
                exit(1);
        }

        /* HyMRPL: initialize secure FIFO */
        hymrpl_fifo_sec_init(&g_fifo_sec);

        /* HyMRPL: initialize adaptive engine for non-root nodes */
        {
                struct iface *iface_tmp;
                list_for_each_entry(iface_tmp, &ifaces, list) {
                        if (!iface_tmp->dodag_root) {
                                g_adaptive_enabled = true;
                                hymrpl_adaptive_init(&g_adaptive, iface_tmp->ifname, loop);
                                flog(LOG_INFO,
                                     "HYMRPL: adaptive engine enabled (non-root node)");
                                break;
                        }
                }
                if (!g_adaptive_enabled)
                        flog(LOG_INFO, "HYMRPL: adaptive engine disabled (root node)");
        }

        rc = rpld_setup(loop, &ifaces);
        if (rc != 0) {
                netlink_close();
                config_free(&ifaces);
                exit(1);
        }

        rc = set_var(PROC_SYS_IP6_MAX_HBH_OPTS_NUM, 99);
        if (rc == -1) {
                flog(LOG_ERR, "Failed to set hbh max value");
                return -1;
        }

        sock = open_icmpv6_socket(&ifaces);
        if (sock < 0) {
                perror("open_icmpv6_socket");
                netlink_close();
                config_free(&ifaces);
                exit(1);
        }

        ev_io_init(&sock_watcher, icmpv6_cb, sock, EV_READ);
        ev_io_start(loop, &sock_watcher);

        /* HyMRPL: periodic timer for FIFO check + adaptive decisions */
        static ev_timer hymrpl_periodic_w;
        ev_timer_init(&hymrpl_periodic_w, hymrpl_periodic_cb, 1.0, 1.0);
        ev_timer_start(loop, &hymrpl_periodic_w);

        flog(LOG_INFO, "HYMRPL: entering main loop (FIFO=%s, adaptive=%s)",
             HYMRPL_FIFO_PATH,
             g_adaptive_enabled ? "enabled" : "disabled");

        ev_run(loop, 0);

        netlink_close();
        close_icmpv6_socket(sock, &ifaces);
        config_free(&ifaces);
        log_close();

        flog(LOG_INFO, "exited");

        return 0;
}
