/*
 * HyMRPL — Adaptive Decision Engine (integrated in rpld)
 *
 * Replaces the external hymrpl_monitor.py with an in-daemon
 * adaptive decision module. Collects local metrics and decides
 * the node's functional class (S or N) without external processes.
 *
 * Security: FIFO commands are authenticated via HMAC-SHA256 token.
 *
 * Authors:
 *   Cassius Clay Batista da Silva Filho
 */

#ifndef __HYMRPL_ADAPTIVE_H__
#define __HYMRPL_ADAPTIVE_H__

#include <stdint.h>
#include <stdbool.h>
#include <netinet/in.h>
#include <ev.h>

#include "list.h"

/* --- Adaptive Decision Parameters --- */
#define HYMRPL_W_PDR        0.4f    /* PDR weight */
#define HYMRPL_W_ENERGY     0.3f    /* Energy weight */
#define HYMRPL_W_STABILITY  0.3f    /* Topological stability weight */
#define HYMRPL_THRESHOLD    0.75f   /* Score >= threshold → Class S */

#define HYMRPL_HYSTERESIS_CYCLES  3  /* Consecutive cycles before switching */
#define HYMRPL_ADAPTIVE_INTERVAL  5.0 /* Seconds between evaluations */

/* PDR tracking window */
#define HYMRPL_PDR_WINDOW   20  /* Number of DAO-ACKs to track */

/* Stability tracking */
#define HYMRPL_STABILITY_WINDOW  60.0  /* Seconds to track parent changes */
#define HYMRPL_STABILITY_NMAX   3      /* Max changes before s_stab = 0 */

/* Energy EMA smoothing */
#define HYMRPL_ENERGY_EMA_ALPHA  0.3f  /* EMA factor for energy readings */

/* --- FIFO Security --- */
#define HYMRPL_FIFO_PATH       "/tmp/hymrpl_cmd"
#define HYMRPL_TOKEN_PATH      "/etc/hymrpl/fifo.token"
#define HYMRPL_TOKEN_LEN       32   /* 256-bit token */
#define HYMRPL_NONCE_LEN       8    /* 64-bit nonce (replay protection) */
#define HYMRPL_HMAC_LEN        32   /* SHA-256 HMAC output */

/* Maximum FIFO message size: CMD(7) + "|" + NONCE(16hex) + "|" + HMAC(64hex) + \n */
#define HYMRPL_FIFO_MSG_MAX    128

/* Rate limiting */
#define HYMRPL_MIN_SWITCH_INTERVAL  10.0  /* Min seconds between class switches */
#define HYMRPL_MAX_SWITCHES_PER_MIN 3     /* Max switches per minute */

/* --- Adaptive State --- */
struct hymrpl_adaptive {
        /* Metrics (all normalized to [0, 1]) */
        float pdr;                  /* Current PDR estimate (0-100) */
        float energy_pct;           /* Raw battery/energy level (0-100) */
        float energy_ema;           /* EMA-smoothed energy [0, 1] */

        /* PDR tracking via DAO-ACK success/failure */
        uint8_t pdr_window[HYMRPL_PDR_WINDOW];
        int pdr_idx;
        int pdr_count;

        /* Topological stability tracking (continuous index) */
        ev_tstamp parent_change_times[HYMRPL_STABILITY_NMAX + 1];
        int parent_change_count;    /* Total changes in window */
        float s_stab;               /* Stability index [0, 1] */

        /* Decision state */
        float last_score;
        int hysteresis_counter;     /* Consecutive cycles favoring change */
        uint8_t pending_class;      /* Class being considered */

        /* Timer */
        ev_timer eval_w;            /* Periodic evaluation timer */

        /* Parent tracking */
        ev_tstamp last_parent_change; /* When parent last changed */
        struct in6_addr last_parent_addr;

        /* Energy source */
        char battery_path[128];     /* Path to battery file/sysfs */
};

/* --- FIFO Security State --- */
struct hymrpl_fifo_sec {
        uint8_t token[HYMRPL_TOKEN_LEN];  /* Shared secret */
        bool token_loaded;                 /* Token successfully loaded */
        uint64_t last_nonce;               /* Last accepted nonce (replay) */
        ev_tstamp last_switch_time;        /* Rate limiting */
        int switches_this_minute;          /* Rate limiting counter */
        ev_tstamp minute_start;            /* Start of current minute window */
};

/* --- API --- */

/*
 * Initialize the adaptive decision engine.
 * Called once at daemon startup.
 */
void hymrpl_adaptive_init(struct hymrpl_adaptive *adp, const char *ifname,
                           struct ev_loop *loop);

/*
 * Record a DAO-ACK reception (success) or timeout (failure).
 * Updates the PDR estimate.
 */
void hymrpl_adaptive_record_dao(struct hymrpl_adaptive *adp, bool success);

/*
 * Notify the adaptive engine that the parent has changed.
 * Resets the stability indicator.
 */
void hymrpl_adaptive_parent_changed(struct hymrpl_adaptive *adp,
                                     const struct in6_addr *new_parent);

/*
 * Get the current recommendation with hysteresis applied.
 * Returns: HYMRPL_CLASS_S, HYMRPL_CLASS_N, or -1 (no change needed)
 */
int hymrpl_adaptive_get_recommendation(struct hymrpl_adaptive *adp,
                                        uint8_t current_class);

/*
 * Initialize FIFO security.
 * Loads the shared token from HYMRPL_TOKEN_PATH.
 * Creates the FIFO with restricted permissions (0600).
 */
int hymrpl_fifo_sec_init(struct hymrpl_fifo_sec *sec);

/*
 * Secure FIFO check: reads, validates HMAC, checks nonce, rate-limits.
 * Returns the new class (HYMRPL_CLASS_S/N) or -1 if no valid command.
 */
int hymrpl_fifo_sec_check(struct hymrpl_fifo_sec *sec,
                           struct list_head *ifaces);

/*
 * Generate a token file (utility for setup).
 */
int hymrpl_generate_token(const char *path);

#endif /* __HYMRPL_ADAPTIVE_H__ */
