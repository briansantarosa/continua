#!/bin/bash
# Restart script for the Continua bridge (systemd user unit).
# Continua-scoped (2026-09-15): manages ONLY what continua needs —
# no Searchie, Newsie, Tarot, or other fleet services. Modeled on
# Modeled on the Sagent restart script, stripped to continua's own teardown,
# cleanup, and start. The bridge spawns its own Searchie child, which
# dies and returns with the bridge — it is never managed separately here.
#
# Baked-in lessons:
#   - 2026-09-14 22:46 incident: an external SIGTERM killed both bridges;
#     continua (which has no Restart= directive) stayed dark ~8h and the designer's
#     message sat undelivered. Restarting must be one verified command.
#   - 2026-09-01 forensic incident: log TRUNCATION destroys the evidence
#     window — rotate (keep newest 5), never truncate.

PROJ="$(cd "$(dirname "$0")" && pwd)"
UNIT="continua.service"

# [CONTINUA] 2026-10-07 incident: a restart with a turn mid-flight SIGTERMs
# the LLM call — the turn dies AND the message vanishes (nothing is
# persisted: no history entry, no chronicle row, no reply). Lost this way:
# Alex's 21:17 "Do you see anything new?" (turn was 2 min into generation)
# and the 20:45 wake. Guard: refuse while an LLM call is newer than the
# last completion. Override with --force (fail-safe: a false ABORT costs
# a moment; a missed in-flight turn costs her words).
LOG="$PROJ/logs/bridge.log"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
if [ "$FORCE" = 0 ] && [ -f "$LOG" ]; then
    _last_call=$(grep -n "LLM call #" "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)
    _last_done=$(grep -n -E "LLM reply|LLM finished|empty LLM response|falling through to the turn-fallback|consume failed" "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)
    if [ -n "$_last_call" ] && { [ -z "$_last_done" ] || [ "$_last_call" -gt "$_last_done" ]; }; then
        echo "ABORT: a resident turn is IN FLIGHT (an LLM call is newer than the last completion):"
        grep -n "LLM call #" "$LOG" | tail -1
        echo "Wait for the turn to finish, then restart. Override: $0 --force"
        exit 1
    fi
fi

echo "=== Stopping $UNIT ==="
systemctl --user stop "$UNIT" 2>/dev/null || true

echo "=== Cleaning up ==="
# Qdrant lock files (per-agent mem0 DBs live under agents/<instance>/)
find "$PROJ/agents" -path '*/mem0_db/qdrant/.lock' -type f -delete 2>/dev/null || true
find "$PROJ/agents" -path '*/.mem0/*/migrations_qdrant/.lock' -type f -delete 2>/dev/null || true

# Rotate logs (keep newest 5 rotations per target)
_ts=$(date +%Y%m%d-%H%M%S)
for _lg in "$PROJ/logs/bridge.log"; do
    if [ -s "$_lg" ]; then
        mv "$_lg" "${_lg}.${_ts}"
        ls -1t "${_lg}."* 2>/dev/null | tail -n +6 | xargs -r rm -f
    fi
done

echo "=== Starting $UNIT ==="
rm -f "$PROJ/logs/mem0_warm.done"
systemctl --user start "$UNIT"

# mem0 warmup — the bridge constructs each agent's memory engine in a
# background thread; a message landing in the cold window stalls silently
# (2026-09-01 /searchmem incident).
echo "Waiting for mem0 warmup..."
_TICK=0
while [ ! -f "$PROJ/logs/mem0_warm.done" ] && [ "$_TICK" -lt 60 ]; do
    sleep 5
    _TICK=$((_TICK + 1))
done
if [ -f "$PROJ/logs/mem0_warm.done" ]; then
    echo "OK: mem0 warm ($(cat "$PROJ/logs/mem0_warm.done"))"
else
    echo "WARN: mem0 warmup incomplete after $((_TICK * 5))s — first commands may stall briefly"
fi

sleep 3
if systemctl --user is-active --quiet "$UNIT"; then
    _PID=$(pgrep -f "$PROJ/bridge.py" | head -1)
    echo "OK: $UNIT running (PID: $_PID)"
else
    echo "FAILED: $UNIT did not reach active state."
    journalctl --user-unit=continua -n 15 --no-pager
    exit 1
fi

# Queued wake packets pending consumption (context only; the bridge drains
# them on its own 60s consumer loop — one full LLM turn per file, oldest
# first, both residents concurrently).
for _inst in residenta residentb residentc; do
    _n=$(ls -1 "$PROJ/wakes/$_inst"/wake_*.json 2>/dev/null | wc -l)
    echo "INFO: $_inst queued wake packets: $_n"
done
