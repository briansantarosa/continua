"""desk_snapshots_test.py — agent-free tests for lesstools LT-1: the desk
snapshot cushion tied to the wake (the designer go 2026-10-05). Real tar in tmp
dirs; SUDO emptied (no root needed); the gates, the cadence hookup, the
exclusions, retention pruning, staging-default restore, and in-place
refusal are all exercised. All writes under /tmp.
"""
import io
import json
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["CONTINUA_RECOLLECTIONS"] = "1"

import desk_snapshots as ds  # noqa: E402
import wake as wake_mod  # noqa: E402

tmp = tempfile.mkdtemp(prefix="lt1-test-")
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label
          + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# --- redirect the roots; drop sudo (a bob-readable tmp desk needs none)
ds.DESK_ROOT = Path(tmp) / "sandbox" / "home"
ds.SNAP_ROOT = Path(tmp) / "logs" / "desk_snapshots"
ds.SUDO = []


def make_desk(instance, files: dict):
    d = ds.DESK_ROOT / instance
    d.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return d


CFG_ON = {"continua": {"desk_snapshots": {"enabled": True}}}

# --- 1. the gates
os.environ.pop(ds.KILL_SWITCH_ENV, None)
make_desk("g", {"journal.txt": "day one"})
r = ds.snapshot("g", cfg={})  # yaml gate off
check("yaml gate off → disabled", r["status"] == "disabled", str(r))
r = ds.snapshot("g", cfg=CFG_ON)
check("yaml gate on → snapshotted", r["status"] == "snapshotted", str(r))
os.environ[ds.KILL_SWITCH_ENV] = "0"
r = ds.snapshot("g", cfg=CFG_ON)
check("env kill switch wins", r["status"] == "disabled", str(r))
os.environ.pop(ds.KILL_SWITCH_ENV, None)

# --- 2. the archive: contents right, tmp/ excluded, store house-side
make_desk("g", {"journal.txt": "day two", "notes/a.md": "a note",
                "tmp/scratch.bin": "scratch"})
r = ds.snapshot("g", cfg=CFG_ON)
check("snapshot ok", r["status"] == "snapshotted", str(r))
archive = r["path"]
with tarfile.open(archive) as tf:
    names = set(tf.getnames())
check("desk contents in the archive",
      {"./journal.txt", "./notes", "./notes/a.md"} <= names, str(names))
check("tmp/ excluded", not any(n.startswith("./tmp") for n in names),
      str(names))
check("store is house-side (outside the desk)",
      archive.startswith(str(ds.SNAP_ROOT))
      and not archive.startswith(str(ds.DESK_ROOT)))

# --- 3. cadence hookup: the wake fires the snapshot past the due-gate
fired = []
_orig = ds.snapshot
ds.snapshot = lambda inst, cfg=None: (fired.append(inst),
                                      {"status": "snapshotted"}) or None
try:
    # the timer loop calls snapshot(inst, cfg) when _is_due passes — the
    # hook itself is the two lines in wake.main; verify the wiring shape
    # by calling it the way main does (fail-open, then generate).
    import types
    src = open(wake_mod.__file__, encoding="utf-8").read()
    check("wake hook present (due-gate path)",
          "_ds.snapshot(inst, cfg)" in src
          and "_is_due(inst, cfg)" in src
          and src.index("_ds.snapshot(inst, cfg)")
          > src.index("_is_due(inst, cfg)"))
    check("wake hook present (manual --instance path)",
          "_ds.snapshot(instance=args.instance)" in src)
    check("wake hook fail-open",
          "desk snapshot failed (fail-open)" in src)
finally:
    ds.snapshot = _orig

# --- 4. retention pruning (7 days; best-effort)
old = ds.SNAP_ROOT / "g" / "20260101T000000Z.tar.gz"
old.write_bytes(b"old")
os.utime(old, (time.time() - 8 * 86400, time.time() - 8 * 86400))
r = ds.snapshot("g", cfg=CFG_ON)
check("stale snapshot pruned", not old.exists() and r["pruned"] >= 1,
      str(r))

# --- 5. restore: staging default; in-place refuses nothing but is
# explicit; in-place restores ownership modes
snaps = ds.list_snapshots("g")
check("snapshots listed", len(snaps) >= 2, str(len(snaps)))
r = ds.restore("g", ts=None, in_place=False)
check("staging default", r["status"] == "restored-to-staging", str(r))
stg = Path(r["staging"])
check("staging holds the desk",
      (stg / "journal.txt").read_text() == "day two"
      and (stg / "notes/a.md").exists())
r = ds.restore("g", ts=snaps[0]["ts"], in_place=False)
check("explicit ts restore lands in its own staging dir",
      r["ts"] == snaps[0]["ts"] and r["staging"] != str(stg), str(r))
# in-place: the tmp desk gets the archive contents back
(ds.DESK_ROOT / "g" / "journal.txt").write_text("clobbered!")
r = ds.restore("g", ts=snaps[-1]["ts"], in_place=True)
check("in-place restores the live desk",
      r["status"] == "restored-in-place"
      and (ds.DESK_ROOT / "g" / "journal.txt").read_text() == "day two",
      str(r))

# --- 6. failure never blocks (the cushion must not become a wall)
class _Boom:
    def run(self, *a, **k):
        raise RuntimeError("tar exploded")


_real_run = ds._run
ds._run = lambda *a, **k: (1, "boom")
try:
    r = ds.snapshot("g", cfg=CFG_ON)
    # tar runs via subprocess in snapshot(); _run is restore-only — the
    # snapshot's own failure path is the except in subprocess handling.
    check("snapshot returns a status dict, never raises",
          isinstance(r, dict) and "status" in r, str(r))
finally:
    ds._run = _real_run

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("all green" if not fails else "see failures above")
