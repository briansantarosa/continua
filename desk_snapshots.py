"""desk_snapshots.py — lesstools.md LT-1: the desk snapshot cushion, tied
to the wake (the designer go 2026-10-05).

When a resident's wake fires (past wake.py's due-gate on
`continua.wake.interval_min`), the desk is snapshotted BEFORE the turn
runs — the state as she sat down. Cadence equals the wake cadence by
construction: change `interval_min`, snapshots follow. No parallel timer,
nothing to drift.

Store: `logs/desk_snapshots/<instance>/<UTC ts>.tar.gz` — house-side,
OUTSIDE the desk (not visible or mutable from inside her sandbox). The
desk is uid 1001 mode 700: the tar is written by root via `sudo -n` (the
house's sanctioned uid crossing — sandbox.py's own pattern), then handed
to bob. `tmp/` is excluded (scratch, not record). Retention: 7 days
(house convention), pruned after each successful snapshot.

Fail-open everywhere: a snapshot error logs and the wake proceeds — the
cushion must never become a blocking surface. Kill switch
`CONTINUA_DESK_SNAPSHOTS=0` + per-resident yaml gate
(`continua.desk_snapshots.enabled`).

Restore (the CLI is `desk_restore.py`): staging by DEFAULT — never
clobbers the live desk without the explicit `--in-place` flag and the designer go
(ruling 3). In-place restores ownership (continua:continua, dirs 700 /
files 600) so her desk stays hers.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
DESK_ROOT = BASE / "sandbox" / "home"
SNAP_ROOT = BASE / "logs" / "desk_snapshots"
SUDO = ["sudo", "-n"]          # tests override to []
RETENTION_DAYS = 7.0
KILL_SWITCH_ENV = "CONTINUA_DESK_SNAPSHOTS"

import logging
logger = logging.getLogger("continua.desk_snapshots")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] desk_snapshots: %(message)s")


def enabled(instance: str, cfg: dict = None) -> bool:
    """Per-resident yaml gate + the env kill switch (house law: every
    gate has both). Fail-open to OFF on config errors."""
    if os.getenv(KILL_SWITCH_ENV, "1") == "0":
        return False
    try:
        if cfg is None:
            import yaml
            cfg = yaml.safe_load(
                (BASE / "configs" / f"{instance}.yaml").read_text()) or {}
        return bool((((cfg or {}).get("continua") or {})
                     .get("desk_snapshots") or {}).get("enabled", False))
    except Exception:
        return False


def snapshot(instance: str, cfg: dict = None) -> dict:
    """Tar the desk to the house-side store, prune past retention.
    Fail-open: never raises; the wake proceeds whatever happens."""
    if os.getenv(KILL_SWITCH_ENV, "1") == "0":
        return {"status": "disabled", "reason": "env kill switch"}
    if not enabled(instance, cfg):
        return {"status": "disabled", "reason": "yaml gate off"}
    desk = DESK_ROOT / instance
    if not desk.is_dir():
        return {"status": "no-desk"}
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = SNAP_ROOT / instance
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{ts}.tar.gz"
    _base = ts
    _n = 0
    while target.exists():  # two fires in one second never overwrite
        _n += 1
        ts = f"{_base}_{_n}"   # '_' sorts AFTER the bare name: chronology holds
        target = out_dir / f"{ts}.tar.gz"
    t0 = time.time()
    try:
        # root reads the mode-700 desk and writes the house-side store in
        # one step (stdout is clip-bound in sandbox.run — the tar never
        # streams through it); then the file is handed to bob.
        proc = subprocess.run(
            SUDO + ["tar", "czf", str(target), "--exclude=./tmp",
                    "-C", str(desk), "."],
            capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            return {"status": "error",
                    "error": (proc.stderr or "")[-200:]}
        try:  # hand the archive to bob (best-effort)
            subprocess.run(SUDO + ["chown", f"{os.getuid()}:{os.getgid()}",
                                   str(target)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
    except Exception as exc:
        logger.warning("[Snapshots] %s failed (fail-open): %s",
                       instance, exc)
        return {"status": "error", "error": str(exc)[:200]}
    pruned = _prune(instance)
    size = target.stat().st_size
    logger.info("[Snapshots] %s -> %s (%d bytes, %.1fs, pruned %d)",
                instance, target.name, size, time.time() - t0, pruned)
    return {"status": "snapshotted", "path": str(target), "ts": ts,
            "bytes": size, "pruned": pruned}


def _prune(instance: str) -> int:
    """Retention: 7 days (house convention). Best-effort."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    pruned = 0
    try:
        for p in (SNAP_ROOT / instance).glob("*.tar.gz"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
                pruned += 1
    except Exception as exc:
        logger.warning("[Snapshots] prune failed (fail-open): %s", exc)
    return pruned


def list_snapshots(instance: str) -> list:
    """(ts, path, bytes) oldest-first, parsed from the archive names."""
    out = []
    try:
        for p in sorted((SNAP_ROOT / instance).glob("*.tar.gz")):
            out.append({"ts": p.stem.replace(".tar", ""), "path": str(p),
                        "bytes": p.stat().st_size})
    except Exception:
        pass
    return out


def _run(cmd: list, timeout: int = 300) -> tuple:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode, (proc.stderr or "")


def restore(instance: str, ts: str = None, in_place: bool = False) -> dict:
    """Restore a snapshot. STAGING by default (ruling 3): extracted to
    `logs/desk_snapshots/staging/<instance>/<ts>/` for inspection — the
    live desk is NEVER touched without `in_place=True` (the explicit flag
    IS the the designer go; the CLI refuses otherwise). In-place restores
    ownership (continua:continua, dirs 700 / files 600)."""
    snaps = list_snapshots(instance)
    if not snaps:
        return {"status": "error", "error": "no snapshots"}
    if ts in (None, "", "latest"):
        archive = snaps[-1]["path"]
        ts = snaps[-1]["ts"]
    else:
        exact = [s for s in snaps if s["ts"] == ts]
        if exact:
            archive, ts = exact[-1]["path"], exact[-1]["ts"]
        else:
            match = [s for s in snaps if s["ts"].startswith(ts)]
            if len(match) != 1:
                return {"status": "error",
                        "error": (f"ambiguous prefix {ts} — "
                                  f"{len(match)} snapshots match"),
                        "matches": [m["ts"] for m in match]}
            archive, ts = match[0]["path"], match[0]["ts"]
    if in_place:
        desk = DESK_ROOT / instance
        rc, err = _run(SUDO + ["tar", "xzf", archive, "-C", str(desk)])
        if rc != 0:
            return {"status": "error", "error": err[-200:]}
        # ownership back to her: dirs 700, files 600 (best-effort)
        _run(SUDO + ["chown", "-R", "continua:continua", str(desk)])
        _run(SUDO + ["find", str(desk), "-type", "d", "-exec", "chmod",
                     "700", "{}", "+"])
        _run(SUDO + ["find", str(desk), "-type", "f", "-exec", "chmod",
                     "600", "{}", "+"])
        logger.info("[Snapshots] %s restored IN PLACE from %s", instance, ts)
        return {"status": "restored-in-place", "instance": instance,
                "ts": ts, "desk": str(desk)}
    staging = SNAP_ROOT / "staging" / instance / ts
    staging.mkdir(parents=True, exist_ok=True)
    rc, err = _run(SUDO + ["tar", "xzf", archive, "-C", str(staging)])
    if rc != 0:
        return {"status": "error", "error": err[-200:]}
    try:  # staging is for inspection: readable without sudo
        subprocess.run(SUDO + ["chown", "-R",
                               f"{os.getuid()}:{os.getgid()}",
                               str(SNAP_ROOT / "staging")],
                       capture_output=True, timeout=60)
    except Exception:
        pass
    return {"status": "restored-to-staging", "instance": instance,
            "ts": ts, "staging": str(staging)}


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="lesstools LT-1: desk snapshot cushion (restore/list)")
    ap.add_argument("--instance", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--from", dest="from_ts", default="latest")
    ap.add_argument("--in-place", action="store_true",
                    help="restore INTO the live desk (requires the designer go; "
                         "staging is the default)")
    args = ap.parse_args()
    if args.list:
        print(json.dumps(list_snapshots(args.instance), indent=2))
        return
    out = restore(args.instance, ts=args.from_ts, in_place=args.in_place)
    print(json.dumps(out, indent=2))
    if out.get("status", "").startswith("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
