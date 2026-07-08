/*
 * hymrpl_cmd — Authenticated command sender for HyMRPL FIFO
 *
 * Sends CLASS_S or CLASS_N commands to the rpld FIFO with
 * HMAC-SHA256 authentication and nonce-based replay protection.
 *
 * Usage:
 *   hymrpl_cmd CLASS_S          # Switch to storing-like
 *   hymrpl_cmd CLASS_N          # Switch to non-storing-like
 *   hymrpl_cmd --gen-token      # Generate a new shared token
 *
 * The command is formatted as:
 *   CLASS_X|<nonce_hex>|<hmac_hex>\n
 *
 * Where:
 *   - nonce is a monotonically increasing 64-bit counter (unix timestamp in µs)
 *   - HMAC is SHA-256 over "CLASS_X|<nonce_hex>" using the shared token
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
#include <sys/time.h>
#include <errno.h>

#include <openssl/hmac.h>
#include <openssl/sha.h>

#define HYMRPL_FIFO_PATH    "/tmp/hymrpl_cmd"
#define HYMRPL_TOKEN_PATH   "/etc/hymrpl/fifo.token"
#define HYMRPL_TOKEN_LEN    32
#define HYMRPL_HMAC_LEN     32

static int load_token(const char *path, unsigned char *token)
{
        FILE *f = fopen(path, "r");
        if (!f) {
                fprintf(stderr, "Error: cannot open token file %s: %s\n",
                        path, strerror(errno));
                fprintf(stderr, "Run 'hymrpl_cmd --gen-token' to create one.\n");
                return -1;
        }

        char hex[65] = {0};
        if (fread(hex, 1, 64, f) != 64) {
                fprintf(stderr, "Error: token file too short\n");
                fclose(f);
                return -1;
        }
        fclose(f);

        for (int i = 0; i < HYMRPL_TOKEN_LEN; i++) {
                unsigned int byte;
                if (sscanf(&hex[i * 2], "%02x", &byte) != 1) {
                        fprintf(stderr, "Error: invalid hex in token\n");
                        return -1;
                }
                token[i] = (unsigned char)byte;
        }

        return 0;
}

static uint64_t get_nonce(void)
{
        struct timeval tv;
        gettimeofday(&tv, NULL);
        return (uint64_t)tv.tv_sec * 1000000ULL + (uint64_t)tv.tv_usec;
}

static int generate_token(const char *path)
{
        unsigned char token[HYMRPL_TOKEN_LEN];
        int fd;
        FILE *f;

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

        /* Ensure directory exists */
        system("mkdir -p /etc/hymrpl");

        f = fopen(path, "w");
        if (!f) {
                perror("fopen token");
                return -1;
        }

        for (int i = 0; i < HYMRPL_TOKEN_LEN; i++)
                fprintf(f, "%02x", token[i]);
        fprintf(f, "\n");

        fclose(f);

        /* Restrict permissions */
        if (chmod(path, 0600) < 0)
                perror("chmod");

        printf("Token generated: %s (mode 0600)\n", path);
        printf("Copy this file to all nodes that need to send FIFO commands.\n");
        return 0;
}

static void usage(void)
{
        fprintf(stderr,
                "Usage: hymrpl_cmd <command>\n"
                "\n"
                "Commands:\n"
                "  CLASS_S        Switch node to storing-like mode\n"
                "  CLASS_N        Switch node to non-storing-like mode\n"
                "  --gen-token    Generate a new authentication token\n"
                "  --help         Show this help\n"
                "\n"
                "The command is authenticated with HMAC-SHA256 using\n"
                "the shared token at %s\n",
                HYMRPL_TOKEN_PATH);
}

int main(int argc, char *argv[])
{
        unsigned char token[HYMRPL_TOKEN_LEN];
        unsigned char hmac_out[HYMRPL_HMAC_LEN];
        unsigned int hmac_len;
        char data[64];
        char message[128];
        uint64_t nonce;
        int fd;

        if (argc < 2) {
                usage();
                return 1;
        }

        if (strcmp(argv[1], "--gen-token") == 0) {
                return generate_token(HYMRPL_TOKEN_PATH);
        }

        if (strcmp(argv[1], "--help") == 0 || strcmp(argv[1], "-h") == 0) {
                usage();
                return 0;
        }

        /* Validate command */
        if (strcmp(argv[1], "CLASS_S") != 0 &&
            strcmp(argv[1], "CLASS_N") != 0) {
                fprintf(stderr, "Error: invalid command '%s'\n", argv[1]);
                fprintf(stderr, "Valid commands: CLASS_S, CLASS_N\n");
                return 1;
        }

        /* Check if token exists — if not, send in legacy mode */
        if (access(HYMRPL_TOKEN_PATH, R_OK) != 0) {
                fprintf(stderr, "Warning: no token file, sending in INSECURE mode\n");
                fprintf(stderr, "Run 'hymrpl_cmd --gen-token' to enable authentication\n\n");

                /* Legacy mode: plain command */
                fd = open(HYMRPL_FIFO_PATH, O_WRONLY | O_NONBLOCK);
                if (fd < 0) {
                        fprintf(stderr, "Error: cannot open FIFO %s: %s\n",
                                HYMRPL_FIFO_PATH, strerror(errno));
                        fprintf(stderr, "Is rpld running?\n");
                        return 1;
                }

                snprintf(message, sizeof(message), "%s\n", argv[1]);
                write(fd, message, strlen(message));
                close(fd);

                printf("Sent (insecure): %s\n", argv[1]);
                return 0;
        }

        /* Load token */
        if (load_token(HYMRPL_TOKEN_PATH, token) < 0)
                return 1;

        /* Generate nonce (microsecond timestamp — monotonically increasing) */
        nonce = get_nonce();

        /* Build data to sign: "CLASS_X|nonce_hex" */
        snprintf(data, sizeof(data), "%s|%016llx",
                 argv[1], (unsigned long long)nonce);

        /* Compute HMAC-SHA256 */
        HMAC(EVP_sha256(), token, HYMRPL_TOKEN_LEN,
             (unsigned char *)data, strlen(data),
             hmac_out, &hmac_len);

        /* Build full message: "CLASS_X|nonce_hex|hmac_hex\n" */
        char hmac_hex[65] = {0};
        for (int i = 0; i < HYMRPL_HMAC_LEN; i++)
                snprintf(&hmac_hex[i * 2], 3, "%02x", hmac_out[i]);

        snprintf(message, sizeof(message), "%s|%016llx|%s\n",
                 argv[1], (unsigned long long)nonce, hmac_hex);

        /* Send to FIFO */
        fd = open(HYMRPL_FIFO_PATH, O_WRONLY | O_NONBLOCK);
        if (fd < 0) {
                fprintf(stderr, "Error: cannot open FIFO %s: %s\n",
                        HYMRPL_FIFO_PATH, strerror(errno));
                fprintf(stderr, "Is rpld running?\n");
                return 1;
        }

        ssize_t written = write(fd, message, strlen(message));
        close(fd);

        if (written < 0) {
                fprintf(stderr, "Error: write failed: %s\n", strerror(errno));
                return 1;
        }

        printf("Sent (authenticated): %s [nonce=%016llx]\n",
               argv[1], (unsigned long long)nonce);
        return 0;
}
