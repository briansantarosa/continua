#!/usr/bin/env bash
# Alternate residents; bounded passes, no outer resident worker lock.
# Usage: backfill_drain.sh [residentb|residenta|all] [jobs_per_pass=1] [wall_seconds=28800]
set -eu
cd /tmp/continua
umask 077
exec /home/you/Sagent/venv/bin/python -u backfill_drain.py "${1:-all}" "${2:-1}" "${3:-28800}"
