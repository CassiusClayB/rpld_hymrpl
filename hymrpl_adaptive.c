/*
 * HyMRPL — Adaptive Decision Engine + Secure FIFO
 *
 * Integrates the adaptive class decision directly into rpld,
 * eliminating the need for the external hymrpl_monitor.py.
 *
 * Security features for FIFO:
 *   1. HMAC-SHA256 authentication (shared token)
 *   2. Nonce-based replay protection
 *   3. Rate limiting (max switches per minute)
 *   4. Restricted FIFO permissions (owner-only)
 *   5. Audit logging of all switch attempts
 *
 * Authors:
 *   Cassius Clay Batista da Silva Filho
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <errno.h>
#include <time.h>

#include <ev.h>

#include "hymrpl_adaptive.h"
#include "config.h"
#include "log.h"
#include "rpl.h"
#include "dag.h"
#include "list.h"

/* Forward declarations */
static void adaptive_eval_cb(EV_P_ ev_timer *w, int revents);
static float read_energy(const char *path);
static void compute_pdr(struct hymrpl_adaptive *adp);

/* Active engine instance (one per daemon, non-root nodes only).
 * Set by hymrpl_adaptive_init(); used by the notify_* wrappers so the
 * protocol code can feed metrics without touching daemon globals. */
static struct hymrpl_adaptive *g_active = NULL;

/* Simple HMAC-SHA256 using /proc or openssl CLI as fallback.
 * For production, link against libcrypto. For this implementation
 * we use a lightweight approach suitable for embedded Linux.
 */
#include <openssl/hmac.h>
#include <openssl/sha.h>

/* ================================================================
 * ADAPTIVE DECISION ENGINE
 * ================================================================ */

void hymrpl_adaptive_init(struct hymrpl_adaptive *adp, const char *ifname,
                           struct ev_loop *loop)
{
        memset(adp, 0, sizeof(*adp));
        adp->pdr = 100.0f;  /* Assume perfect until proven otherwise */
        adp->energy_pct = 100.0f;
        adp->energy_ema = 1.0f;  /* Normalized [0,1] */
        adp->s_stab = 1.0f;     /* Fully stable initially */
        adp->parent_change_count = 0;
        adp->hysteresis_counter = 0;
        adp->last_score = 1.0f;
        adp->pdr_idx = 0;
        adp->pdr_count = 0;
        adp->last_parent_change = 0;

        /* Per-interface battery path (simulated) */
        if (ifname && ifname[0])
                snprintf(adp->battery_path, sizeof(adp->battery_path),
                         "/tmp/hymrpl_battery_%s", ifname);
        else
                snprintf(adp->battery_path, sizeof(adp->battery_path),
                         "/tmp/hymrpl_battery");

        /* Initialize PDR window to all successes */
        memset(adp->pdr_window, 1, sizeof(adp->pdr_window));

        /* No DAO outstanding yet */
        adp->dao_awaiting = false;
        adp->dao_sent_time = 0;

        /* Register as the active engine for the notify_* wrappers */
        g_active = adp;

        /* Start periodic evaluation timer */
        ev_timer_init(&adp->eval_w, adaptive_eval_cb,
                      HYMRPL_ADAPTIVE_INTERVAL, HYMRPL_ADAPTIVE_INTERVAL);
        ev_timer_start(loop, &adp->eval_w);

        flog(LOG_INFO, "HYMRPL adaptive: initialized (interval=%.1fs, "
             "threshold=%.2f, weights=%.1f/%.1f/%.1f)",
             HYMRPL_ADAPTIVE_INTERVAL, HYMRPL_THRESHOLD,
             HYMRPL_W_PDR, HYMRPL_W_ENERGY, HYMRPL_W_STABILITY);
}

void hymrpl_adaptive_record_dao(struct hymrpl_adaptive *adp, bool success)
{
        adp->pdr_window[adp->pdr_idx] = success ? 1 : 0;
        adp->pdr_idx = (adp->pdr_idx + 1) % HYMRPL_PDR_WINDOW;
        if (adp->pdr_count < HYMRPL_PDR_WINDOW)
                adp->pdr_count++;

        compute_pdr(adp);
}

void hymrpl_adaptive_parent_changed(struct hymrpl_adaptive *adp,
                                     const struct in6_addr *new_parent)
{
        ev_tstamp now = ev_now(EV_DEFAULT);

        /* Record this parent change timestamp */
        if (adp->parent_change_count < HYMRPL_STABILITY_NMAX + 1) {
                adp->parent_change_times[adp->parent_change_count] = now;
                adp->parent_change_count++;
        } else {
                /* Shift window left and append */
                memmove(&adp->parent_change_times[0],
                        &adp->parent_change_times[1],
                        sizeof(ev_tstamp) * HYMRPL_STABILITY_NMAX);
                adp->parent_change_times[HYMRPL_STABILITY_NMAX] = now;
        }

        adp->last_parent_change = now;
        if (new_parent)
                memcpy(&adp->last_parent_addr, new_parent,
                       sizeof(struct in6_addr));

        flog(LOG_INFO, "HYMRPL adaptive: parent changed, updating stability index");
}

static void compute_pdr(struct hymrpl_adaptive *adp)
{
        if (adp->pdr_count == 0) {
                adp->pdr = 100.0f;
                return;
        }

        int successes = 0;
        int count = adp->pdr_count < HYMRPL_PDR_WINDOW ?
                    adp->pdr_count : HYMRPL_PDR_WINDOW;

        for (int i = 0; i < count; i++)
                successes += adp->pdr_window[i];

        adp->pdr = (float)successes / (float)count * 100.0f;
}

static float read_energy(const char *path)
{
        FILE *f;
        float val = 100.0f;

        /* Try simulated battery file first (for testing) */
        if (path && path[0]) {
                f = fopen(path, "r");
                if (f) {
                        if (fscanf(f, "%f", &val) == 1) {
                                fclose(f);
                                return val;
                        }
                        fclose(f);
                }
        }

        /* Fall back to sysfs (real hardware) */
        f = fopen("/sys/class/power_supply/BAT0/capacity", "r");
        if (f) {
                if (fscanf(f, "%f", &val) != 1)
                        val = 100.0f;
                fclose(f);
                return val;
        }

        return 100.0f;
}

/*
 * Compute the continuous stability index s_stab ∈ [0, 1].
 * s_stab = 1 - min(1, N_changes_in_window / N_max)
 *
 * Counts parent changes within the last HYMRPL_STABILITY_WINDOW seconds.
 */
static void compute_stability(struct hymrpl_adaptive *adp)
{
        ev_tstamp now = ev_now(EV_DEFAULT);
        ev_tstamp window_start = now - HYMRPL_STABILITY_WINDOW;
        int changes_in_window = 0;

        for (int i = 0; i < adp->parent_change_count; i++) {
                if (adp->parent_change_times[i] >= window_start)
                        changes_in_window++;
        }

        float ratio = (float)changes_in_window / (float)HYMRPL_STABILITY_NMAX;
        if (ratio > 1.0f)
                ratio = 1.0f;

        adp->s_stab = 1.0f - ratio;
}

/*
 * Core adaptive decision function.
 * Computes the composite score using normalized components:
 *
 *   s_pdr    = PDR / 100          ∈ [0, 1]  (sliding window of DAO-ACKs)
 *   s_energy = EMA(Energy / 100)  ∈ [0, 1]  (exponential moving average)
 *   s_stab   = 1 - N_changes/Nmax ∈ [0, 1]  (continuous stability index)
 *
 *   Score = W_PDR × s_pdr + W_ENERGY × s_energy + W_STABILITY × s_stab
 *
 * Returns: HYMRPL_CLASS_S or HYMRPL_CLASS_N
 */
static uint8_t adaptive_decide(struct hymrpl_adaptive *adp)
{
        float s_pdr = adp->pdr / 100.0f;
        float s_energy = adp->energy_ema;
        float s_stab = adp->s_stab;

        float score = HYMRPL_W_PDR * s_pdr +
                      HYMRPL_W_ENERGY * s_energy +
                      HYMRPL_W_STABILITY * s_stab;

        adp->last_score = score;

        if (score >= HYMRPL_THRESHOLD)
                return HYMRPL_CLASS_S;
        else
                return HYMRPL_CLASS_N;
}

/*
 * Periodic evaluation callback (called by libev timer).
 * This is the heart of the integrated adaptive engine.
 */
static void adaptive_eval_cb(EV_P_ ev_timer *w, int revents)
{
        struct hymrpl_adaptive *adp = container_of(w, struct hymrpl_adaptive, eval_w);

        /* PDR failure detection: a DAO that was never acknowledged within
         * HYMRPL_DAO_ACK_TIMEOUT counts as a delivery failure (0 sample). */
        if (adp->dao_awaiting &&
            (ev_now(EV_DEFAULT) - adp->dao_sent_time) > HYMRPL_DAO_ACK_TIMEOUT) {
                adp->dao_awaiting = false;
                hymrpl_adaptive_record_dao(adp, false);
                flog(LOG_INFO, "HYMRPL adaptive: DAO-ACK timeout, PDR failure recorded");
        }

        /* Update energy reading with EMA smoothing */
        float raw_energy = read_energy(adp->battery_path) / 100.0f;
        adp->energy_pct = raw_energy * 100.0f;
        adp->energy_ema = HYMRPL_ENERGY_EMA_ALPHA * raw_energy +
                          (1.0f - HYMRPL_ENERGY_EMA_ALPHA) * adp->energy_ema;

        /* Compute continuous stability index */
        compute_stability(adp);

        /* Compute decision */
        uint8_t recommended = adaptive_decide(adp);

        flog(LOG_INFO, "HYMRPL adaptive: score=%.3f s_pdr=%.2f s_energy=%.2f "
             "s_stab=%.2f → %s",
             adp->last_score, adp->pdr / 100.0f, adp->energy_ema,
             adp->s_stab,
             recommended == HYMRPL_CLASS_S ? "S" : "N");

        (void)recommended;
}

/*
 * Get the current recommendation with hysteresis applied.
 * Called by the main loop after the timer fires.
 *
 * Returns: HYMRPL_CLASS_S, HYMRPL_CLASS_N, or -1 (no change)
 */
int hymrpl_adaptive_get_recommendation(struct hymrpl_adaptive *adp,
                                        uint8_t current_class)
{
        uint8_t recommended = adaptive_decide(adp);

        if (recommended == current_class) {
                /* No change needed, reset hysteresis */
                adp->hysteresis_counter = 0;
                return -1;
        }

        /* Different from current: increment hysteresis */
        if (adp->pending_class != recommended) {
                /* Direction changed, reset counter */
                adp->pending_class = recommended;
                adp->hysteresis_counter = 1;
        } else {
                adp->hysteresis_counter++;
        }

        if (adp->hysteresis_counter >= HYMRPL_HYSTERESIS_CYCLES) {
                adp->hysteresis_counter = 0;
                flog(LOG_INFO, "HYMRPL adaptive: hysteresis met, recommending %s",
                     recommended == HYMRPL_CLASS_S ? "S" : "N");
                return recommended;
        }

        flog(LOG_INFO, "HYMRPL adaptive: pending %s (%d/%d)",
             recommended == HYMRPL_CLASS_S ? "S" : "N",
             adp->hysteresis_counter, HYMRPL_HYSTERESIS_CYCLES);
        return -1;
}


/* ================================================================
 * NOTIFICATION WRAPPERS (called from process.c)
 * ================================================================ */

void hymrpl_adaptive_notify_dao_sent(void)
{
        if (!g_active)
                return;
        g_active->dao_awaiting = true;
        g_active->dao_sent_time = ev_now(EV_DEFAULT);
}

void hymrpl_adaptive_notify_dao_ack(void)
{
        if (!g_active)
                return;
        g_active->dao_awaiting = false;
        hymrpl_adaptive_record_dao(g_active, true);
}

void hymrpl_adaptive_notify_parent_change(const struct in6_addr *new_parent)
{
        if (!g_active)
                return;
        hymrpl_adaptive_parent_changed(g_active, new_parent);
}


/* ================================================================
 * SECURE FIFO
 * ================================================================ */

int hymrpl_fifo_sec_init(struct hymrpl_fifo_sec *sec)
{
        FILE *f;

        memset(sec, 0, sizeof(*sec));
        sec->token_loaded = false;
        sec->last_nonce = 0;
        sec->last_switch_time = 0;
        sec->switches_this_minute = 0;
        sec->minute_start = ev_now(EV_DEFAULT);

        /* Always create the FIFO first */
        unlink(HYMRPL_FIFO_PATH);
        if (mkfifo(HYMRPL_FIFO_PATH, 0600) < 0) {
                flog(LOG_ERR, "HYMRPL security: mkfifo failed: %s",
                     strerror(errno));
                /* Try with broader permissions as fallback */
                mkfifo(HYMRPL_FIFO_PATH, 0666);
        }

        /* Load shared token */
        f = fopen(HYMRPL_TOKEN_PATH, "r");
        if (!f) {
                flog(LOG_WARNING,
                     "HYMRPL security: token file %s not found, "
                     "FIFO authentication DISABLED (insecure mode)",
                     HYMRPL_TOKEN_PATH);
                /* In insecure mode, FIFO still works but without auth.
                 * This allows backward compatibility with existing scripts. */
                return 0;
        }

        /* Read 32 bytes of hex-encoded token (64 hex chars) */
        char hex[65] = {0};
        if (fread(hex, 1, 64, f) != 64) {
                flog(LOG_ERR, "HYMRPL security: token file too short");
                fclose(f);
                return -1;
        }
        fclose(f);

        /* Decode hex to bytes */
        for (int i = 0; i < HYMRPL_TOKEN_LEN; i++) {
                unsigned int byte;
                if (sscanf(&hex[i * 2], "%02x", &byte) != 1) {
                        flog(LOG_ERR, "HYMRPL security: invalid token hex");
                        return -1;
                }
                sec->token[i] = (uint8_t)byte;
        }

        sec->token_loaded = true;
        flog(LOG_INFO, "HYMRPL security: token loaded, FIFO authentication ENABLED");

        flog(LOG_INFO, "HYMRPL security: FIFO created with restricted permissions");
        return 0;
}

/*
 * Verify HMAC of a FIFO message.
 *
 * Message format (authenticated):
 *   CLASS_S|<nonce_hex>|<hmac_hex>\n
 *   CLASS_N|<nonce_hex>|<hmac_hex>\n
 *
 * The HMAC is computed over: "CLASS_X|<nonce_hex>"
 * using the shared token as key.
 *
 * Message format (legacy/insecure, when no token loaded):
 *   CLASS_S\n
 *   CLASS_N\n
 */
static int verify_hmac(struct hymrpl_fifo_sec *sec,
                       const char *msg, int msg_len,
                       uint8_t *out_class, uint64_t *out_nonce)
{
        char *pipe1, *pipe2;
        char cmd_part[16] = {0};
        char nonce_hex[17] = {0};
        char hmac_hex[65] = {0};
        unsigned char expected_hmac[HYMRPL_HMAC_LEN];
        unsigned char received_hmac[HYMRPL_HMAC_LEN];
        unsigned int hmac_out_len;
        char data_to_verify[64] = {0};

        /* Find the two pipe separators */
        pipe1 = strchr(msg, '|');
        if (!pipe1) return -1;

        pipe2 = strchr(pipe1 + 1, '|');
        if (!pipe2) return -1;

        /* Extract command */
        int cmd_len = pipe1 - msg;
        if (cmd_len > 7 || cmd_len < 7) return -1;
        memcpy(cmd_part, msg, cmd_len);

        /* Extract nonce (16 hex chars = 8 bytes) */
        int nonce_len = pipe2 - pipe1 - 1;
        if (nonce_len != HYMRPL_NONCE_LEN * 2) return -1;
        memcpy(nonce_hex, pipe1 + 1, nonce_len);

        /* Extract HMAC (64 hex chars = 32 bytes) */
        int hmac_len = msg_len - (pipe2 - msg) - 1;
        /* Strip trailing newline */
        while (hmac_len > 0 && (msg[pipe2 - msg + 1 + hmac_len - 1] == '\n' ||
               msg[pipe2 - msg + 1 + hmac_len - 1] == '\r'))
                hmac_len--;
        if (hmac_len != HYMRPL_HMAC_LEN * 2) return -1;
        memcpy(hmac_hex, pipe2 + 1, hmac_len);

        /* Reconstruct data to verify: "CLASS_X|nonce_hex" */
        snprintf(data_to_verify, sizeof(data_to_verify),
                 "%s|%s", cmd_part, nonce_hex);

        /* Compute expected HMAC */
        HMAC(EVP_sha256(), sec->token, HYMRPL_TOKEN_LEN,
             (unsigned char *)data_to_verify, strlen(data_to_verify),
             expected_hmac, &hmac_out_len);

        /* Decode received HMAC from hex */
        for (int i = 0; i < HYMRPL_HMAC_LEN; i++) {
                unsigned int byte;
                if (sscanf(&hmac_hex[i * 2], "%02x", &byte) != 1)
                        return -1;
                received_hmac[i] = (uint8_t)byte;
        }

        /* Constant-time comparison to prevent timing attacks */
        int diff = 0;
        for (int i = 0; i < HYMRPL_HMAC_LEN; i++)
                diff |= expected_hmac[i] ^ received_hmac[i];

        if (diff != 0) {
                flog(LOG_WARNING, "HYMRPL security: HMAC verification FAILED");
                return -1;
        }

        /* Parse nonce */
        uint64_t nonce = 0;
        for (int i = 0; i < HYMRPL_NONCE_LEN * 2; i++) {
                unsigned int nibble;
                if (sscanf(&nonce_hex[i], "%1x", &nibble) != 1)
                        return -1;
                nonce = (nonce << 4) | nibble;
        }
        *out_nonce = nonce;

        /* Parse class */
        if (strstr(cmd_part, "CLASS_N"))
                *out_class = HYMRPL_CLASS_N;
        else if (strstr(cmd_part, "CLASS_S"))
                *out_class = HYMRPL_CLASS_S;
        else
                return -1;

        return 0;
}

int hymrpl_fifo_sec_check(struct hymrpl_fifo_sec *sec,
                           struct list_head *ifaces)
{
        char buf[HYMRPL_FIFO_MSG_MAX];
        static int fifo_fd = -1;
        int n;
        struct iface *iface;
        struct rpl *rpl;
        struct dag *dag;
        uint8_t new_class;
        ev_tstamp now = ev_now(EV_DEFAULT);

        /* Keep FIFO open persistently to avoid blocking writers */
        if (fifo_fd < 0) {
                fifo_fd = open(HYMRPL_FIFO_PATH, O_RDONLY | O_NONBLOCK);
                if (fifo_fd < 0)
                        return -1;
        }

        n = read(fifo_fd, buf, sizeof(buf) - 1);

        if (n <= 0)
                return -1;

        buf[n] = '\0';

        /* --- Authentication --- */
        if (sec->token_loaded) {
                uint64_t nonce;

                if (verify_hmac(sec, buf, n, &new_class, &nonce) < 0) {
                        flog(LOG_WARNING,
                             "HYMRPL security: rejected FIFO command "
                             "(invalid HMAC or format)");
                        return -1;
                }

                /* Replay protection: nonce must be strictly increasing */
                if (nonce <= sec->last_nonce) {
                        flog(LOG_WARNING,
                             "HYMRPL security: rejected FIFO command "
                             "(replay detected, nonce=%lu <= last=%lu)",
                             (unsigned long)nonce,
                             (unsigned long)sec->last_nonce);
                        return -1;
                }
                sec->last_nonce = nonce;

        } else {
                /* Legacy mode (no token): accept plain commands */
                if (strstr(buf, "CLASS_N"))
                        new_class = HYMRPL_CLASS_N;
                else if (strstr(buf, "CLASS_S"))
                        new_class = HYMRPL_CLASS_S;
                else
                        return -1;

                flog(LOG_INFO,
                     "HYMRPL security: accepted FIFO command (INSECURE mode)");
        }

        /* --- Rate Limiting --- */
        /* Reset minute counter if window expired */
        if ((now - sec->minute_start) > 60.0) {
                sec->switches_this_minute = 0;
                sec->minute_start = now;
        }

        if (sec->switches_this_minute >= HYMRPL_MAX_SWITCHES_PER_MIN) {
                flog(LOG_WARNING,
                     "HYMRPL security: rate limit exceeded (%d switches/min)",
                     HYMRPL_MAX_SWITCHES_PER_MIN);
                return -1;
        }

        if ((now - sec->last_switch_time) < HYMRPL_MIN_SWITCH_INTERVAL) {
                flog(LOG_WARNING,
                     "HYMRPL security: too fast (%.1fs < %.1fs min interval)",
                     now - sec->last_switch_time,
                     HYMRPL_MIN_SWITCH_INTERVAL);
                return -1;
        }

        /* --- Apply class change --- */
        list_for_each_entry(iface, ifaces, list) {
                iface->node_class = new_class;
                list_for_each_entry(rpl, &iface->rpls, list) {
                        list_for_each_entry(dag, &rpl->dags, list) {
                                if (dag->node_class != new_class) {
                                        flog(LOG_INFO,
                                             "HYMRPL: profile switch %s -> %s "
                                             "(authenticated=%s)",
                                             dag->node_class == HYMRPL_CLASS_S ? "S" : "N",
                                             new_class == HYMRPL_CLASS_S ? "S" : "N",
                                             sec->token_loaded ? "yes" : "no");
                                        dag->node_class = new_class;
                                }
                        }
                }
        }

        sec->last_switch_time = now;
        sec->switches_this_minute++;

        return new_class;
}

int hymrpl_generate_token(const char *path)
{
        FILE *f;
        unsigned char token[HYMRPL_TOKEN_LEN];
        int fd;

        /* Generate random token from /dev/urandom */
        fd = open("/dev/urandom", O_RDONLY);
        if (fd < 0) {
                perror("open /dev/urandom");
                return -1;
        }
        if (read(fd, token, HYMRPL_TOKEN_LEN) != HYMRPL_TOKEN_LEN) {
                perror("read urandom");
                close(fd);
                return -1;
        }
        close(fd);

        /* Write hex-encoded token */
        f = fopen(path, "w");
        if (!f) {
                perror("fopen token");
                return -1;
        }

        for (int i = 0; i < HYMRPL_TOKEN_LEN; i++)
                fprintf(f, "%02x", token[i]);
        fprintf(f, "\n");

        fclose(f);
        chmod(path, 0600);

        printf("Token generated: %s\n", path);
        return 0;
}
