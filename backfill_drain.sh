#!/usr/bin/env bash
# Alternate residents; bounded passes, no outer resident worker lock.
# Usage: backfill_drain.sh [residentb|residenta|residentc|all] [jobs_per_pass=1] [wall_seconds=28800]
set -eu
cd "$(dirname "$0")"
umask 077
exec python3 -u backfill_drain.py "${1:-all}" "${2:-1}" "${3:-28800}"
