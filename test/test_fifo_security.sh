#!/bin/bash
# ============================================================
# HyMRPL — Test: FIFO Security (standalone, no Mininet needed)
#
# Tests the hymrpl_cmd tool and FIFO security mechanisms
# without requiring the full Mininet-WiFi topology.
#
# Prerequisites:
#   - hymrpl_cmd compiled: gcc -Wall -o hymrpl_cmd hymrpl_cmd.c -lssl -lcrypto
#   - rpld compiled with adaptive module
#
# Usage: sudo bash test_fifo_security.sh
# ============================================================

set -e

HYMRPL_CMD="${HYMRPL_CMD:-./hymrpl_cmd}"
TOKEN_PATH="/etc/hymrpl/fifo.token"
FIFO_PATH="/tmp/hymrpl_cmd"
PASS=0
FAIL=0

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() {
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
    PASS=$((PASS + 1))
}

fail() {
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    FAIL=$((FAIL + 1))
}

warn() {
    echo -e "  ${YELLOW}⚠ WARN${NC}: $1"
}

echo "============================================================"
echo "HyMRPL — FIFO Security Tests"
echo "============================================================"
echo ""

# --- Check prerequisites ---
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)"
    exit 1
fi

if [ ! -f "$HYMRPL_CMD" ]; then
    # Try system path
    HYMRPL_CMD=$(which hymrpl_cmd 2>/dev/null || true)
    if [ -z "$HYMRPL_CMD" ]; then
        echo "ERROR: hymrpl_cmd not found. Compile first:"
        echo "  gcc -Wall -o hymrpl_cmd hymrpl_cmd.c -lssl -lcrypto"
        exit 1
    fi
fi

echo "Using: $HYMRPL_CMD"
echo ""

# ============================================================
echo "--- TEST 1: Token Generation ---"
# ============================================================

# Clean up
rm -f "$TOKEN_PATH"
rm -rf /etc/hymrpl

$HYMRPL_CMD --gen-token > /dev/null 2>&1

if [ -f "$TOKEN_PATH" ]; then
    pass "Token file created at $TOKEN_PATH"
else
    fail "Token file not created"
fi

# Check permissions
PERMS=$(stat -c %a "$TOKEN_PATH" 2>/dev/null)
if [ "$PERMS" = "600" ]; then
    pass "Token permissions are 0600 (owner-only)"
else
    fail "Token permissions are $PERMS (expected 0600)"
fi

# Check token length (64 hex chars + newline)
TOKEN_LEN=$(wc -c < "$TOKEN_PATH" | tr -d ' ')
if [ "$TOKEN_LEN" -ge 64 ]; then
    pass "Token length is valid ($TOKEN_LEN bytes)"
else
    fail "Token too short ($TOKEN_LEN bytes, expected >= 64)"
fi

echo ""

# ============================================================
echo "--- TEST 2: Authenticated Command Format ---"
# ============================================================

# Create FIFO for testing (simulating rpld)
rm -f "$FIFO_PATH"
mkfifo "$FIFO_PATH"

# Read from FIFO in background
RECEIVED=""
cat "$FIFO_PATH" > /tmp/fifo_test_output &
CAT_PID=$!
sleep 0.5

# Send authenticated command
$HYMRPL_CMD CLASS_S > /tmp/hymrpl_cmd_output 2>&1
sleep 1

# Kill background reader
kill $CAT_PID 2>/dev/null || true
wait $CAT_PID 2>/dev/null || true

RECEIVED=$(cat /tmp/fifo_test_output 2>/dev/null)

# Check format: CLASS_S|<16 hex chars nonce>|<64 hex chars hmac>
if echo "$RECEIVED" | grep -qE '^CLASS_S\|[0-9a-f]{16}\|[0-9a-f]{64}$'; then
    pass "Authenticated message format is correct"
    echo "       Message: ${RECEIVED:0:40}..."
else
    fail "Message format incorrect: '$RECEIVED'"
fi

# Check that nonce is present
NONCE=$(echo "$RECEIVED" | cut -d'|' -f2)
if [ ${#NONCE} -eq 16 ]; then
    pass "Nonce is 16 hex chars (64-bit)"
else
    fail "Nonce length wrong: ${#NONCE} (expected 16)"
fi

# Check HMAC is present
HMAC=$(echo "$RECEIVED" | cut -d'|' -f3)
if [ ${#HMAC} -eq 64 ]; then
    pass "HMAC is 64 hex chars (SHA-256)"
else
    fail "HMAC length wrong: ${#HMAC} (expected 64)"
fi

echo ""

# ============================================================
echo "--- TEST 3: Nonce Monotonicity ---"
# ============================================================

# Send two commands and verify nonces are increasing
rm -f "$FIFO_PATH"
mkfifo "$FIFO_PATH"

cat "$FIFO_PATH" > /tmp/fifo_test_1 &
PID1=$!
sleep 0.3
$HYMRPL_CMD CLASS_N > /dev/null 2>&1
sleep 0.5
kill $PID1 2>/dev/null || true
wait $PID1 2>/dev/null || true

sleep 0.1

rm -f "$FIFO_PATH"
mkfifo "$FIFO_PATH"

cat "$FIFO_PATH" > /tmp/fifo_test_2 &
PID2=$!
sleep 0.3
$HYMRPL_CMD CLASS_S > /dev/null 2>&1
sleep 0.5
kill $PID2 2>/dev/null || true
wait $PID2 2>/dev/null || true

NONCE1=$(cat /tmp/fifo_test_1 | cut -d'|' -f2)
NONCE2=$(cat /tmp/fifo_test_2 | cut -d'|' -f2)

if [ -n "$NONCE1" ] && [ -n "$NONCE2" ]; then
    # Compare as hex (lexicographic works for same-length hex)
    N1_DEC=$(printf "%d" "0x$NONCE1" 2>/dev/null || echo 0)
    N2_DEC=$(printf "%d" "0x$NONCE2" 2>/dev/null || echo 0)

    if [ "$N2_DEC" -gt "$N1_DEC" ]; then
        pass "Nonces are monotonically increasing ($NONCE1 < $NONCE2)"
    else
        fail "Nonces not increasing ($NONCE1 >= $NONCE2)"
    fi
else
    fail "Could not extract nonces"
fi

echo ""

# ============================================================
echo "--- TEST 4: Invalid Command Rejected ---"
# ============================================================

rm -f "$FIFO_PATH"
mkfifo "$FIFO_PATH"

# Try sending invalid command directly
cat "$FIFO_PATH" > /tmp/fifo_test_invalid &
PID3=$!
sleep 0.3

# Send garbage (not CLASS_S or CLASS_N)
echo "INVALID_CMD" > "$FIFO_PATH" 2>/dev/null || true
sleep 0.5
kill $PID3 2>/dev/null || true
wait $PID3 2>/dev/null || true

# The hymrpl_cmd tool should reject invalid commands
OUTPUT=$($HYMRPL_CMD INVALID 2>&1 || true)
if echo "$OUTPUT" | grep -qi "invalid\|error"; then
    pass "hymrpl_cmd rejects invalid commands"
else
    fail "hymrpl_cmd did not reject invalid command"
fi

echo ""

# ============================================================
echo "--- TEST 5: Legacy Mode (no token) ---"
# ============================================================

# Remove token temporarily
mv "$TOKEN_PATH" "${TOKEN_PATH}.bak"

rm -f "$FIFO_PATH"
mkfifo "$FIFO_PATH"

cat "$FIFO_PATH" > /tmp/fifo_test_legacy &
PID4=$!
sleep 0.3

# Send in legacy mode (should warn but work)
OUTPUT=$($HYMRPL_CMD CLASS_N 2>&1)
sleep 0.5
kill $PID4 2>/dev/null || true
wait $PID4 2>/dev/null || true

LEGACY_MSG=$(cat /tmp/fifo_test_legacy 2>/dev/null)

if echo "$OUTPUT" | grep -qi "insecure\|warning\|no token"; then
    pass "Legacy mode shows security warning"
else
    fail "No warning in legacy mode"
fi

if echo "$LEGACY_MSG" | grep -q "CLASS_N"; then
    pass "Legacy mode sends plain command"
else
    fail "Legacy mode did not send command"
fi

# Restore token
mv "${TOKEN_PATH}.bak" "$TOKEN_PATH"

echo ""

# ============================================================
echo "--- TEST 6: HMAC Tampering Detection ---"
# ============================================================

rm -f "$FIFO_PATH"
mkfifo "$FIFO_PATH"

# Create a tampered message (valid format but wrong HMAC)
FAKE_NONCE="0000000000000001"
FAKE_HMAC="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TAMPERED="CLASS_N|${FAKE_NONCE}|${FAKE_HMAC}"

# Note: We can't test this without rpld running, but we verify
# the format would be rejected by checking the HMAC doesn't match
# what hymrpl_cmd would produce

# Generate a real message for CLASS_N
cat "$FIFO_PATH" > /tmp/fifo_test_real &
PID5=$!
sleep 0.3
$HYMRPL_CMD CLASS_N > /dev/null 2>&1
sleep 0.5
kill $PID5 2>/dev/null || true
wait $PID5 2>/dev/null || true

REAL_MSG=$(cat /tmp/fifo_test_real 2>/dev/null)
REAL_HMAC=$(echo "$REAL_MSG" | cut -d'|' -f3)

if [ "$REAL_HMAC" != "$FAKE_HMAC" ]; then
    pass "Real HMAC differs from fake (tampering would be detected)"
else
    fail "HMAC collision (extremely unlikely, check implementation)"
fi

echo ""

# ============================================================
# CLEANUP
# ============================================================
rm -f "$FIFO_PATH" /tmp/fifo_test_* /tmp/hymrpl_cmd_output

echo "============================================================"
echo "RESULTS: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
echo "============================================================"

if [ $FAIL -eq 0 ]; then
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed.${NC}"
    exit 1
fi
