#!/bin/bash
# provision_desk.sh — create the sandbox desk for a Continua resident
# (T1 multi-agent, plan: agentwiki/projects/Continua-Multi-Agent-Upgrade-Plan.md).
#
# Sandbox/jobs FAIL CLOSED if the desk is missing (sandbox.build_argv probes
# desk + tmp via sudo test -d before running anything), so a new resident
# needs its desk created ONCE, as root, before their continua.sandbox block
# is enabled:
#
#     sudo ./provision_desk.sh <instance> [<instance> ...]
#
# What it creates (per resident):
#   $DESK_ROOT/<instance>/            mode 700, owned continua:continua
#   $DESK_ROOT/<instance>/tmp/        TMPDIR target (build_argv requirement)
#   $DESK_ROOT/<instance>/job_logs/   jobs.py creates per-job logs via sudo
#                                     (chown to the continua uid at job start)
#
# Overridable for tests: CONTINUA_SANDBOX_HOME, CONTINUA_SANDBOX_USER
# (same env names sandbox.py reads — one source of truth).

set -euo pipefail

DESK_ROOT="${CONTINUA_SANDBOX_HOME:-/tmp/continua/sandbox/home}"
SBX_USER="${CONTINUA_SANDBOX_USER:-continua}"

if [ "$(id -u)" -ne 0 ]; then
    echo "provision_desk.sh: must run as root (sudo ./provision_desk.sh <instance> ...)" >&2
    exit 1
fi

if ! id "$SBX_USER" >/dev/null 2>&1; then
    echo "provision_desk.sh: sandbox user '$SBX_USER' does not exist" >&2
    exit 1
fi

if [ "$#" -lt 1 ]; then
    echo "usage: sudo ./provision_desk.sh <instance> [<instance> ...]" >&2
    exit 1
fi

for inst in "$@"; do
    # instance ids are filesystem path components AND chronicle path
    # components (chronicle filename regex: [a-z0-9_]+) — allow nothing else
    if [[ ! "$inst" =~ ^[a-z0-9_]+$ ]]; then
        echo "provision_desk.sh: bad instance name '$inst' (use [a-z0-9_]+)" >&2
        exit 1
    fi
    d="$DESK_ROOT/$inst"
    mkdir -p "$d/tmp" "$d/job_logs"
    chown -R "$SBX_USER:$(id -gn "$SBX_USER")" "$d"
    chmod 700 "$d"
    echo "provisioned desk: $d (owner $SBX_USER, mode 700)"
done