#!/usr/bin/env python3
"""One-time in-place HISTORY repair — the ts-stacking fix's data half
(2026-09-24, follow-up to the 09-22 cleansweep repair).

The 09-22 repair pass (repair_cleansweep.py) cleaned only chronicle/<...>/
*.jsonl. The persistent histories (histories/<config>/<user>.json) were out
of scope — and they were the live wound: assistant entries there still carry
the leading [YYYY-MM-DD…] prefixes the reply-side enforcer had stripped from
the delivered copy, so every prompt re-fed them as few-shot. Observed:
residentb's system-wake thread accumulated 15-16 stacked prefixes per reply
(+1 per wake); residentb's chat thread carries 2-3 per entry since 09-22;
residenta's chat thread carries the collapse-stub turns.

Repairs, per assistant entry in histories/<config>/<file>.json:
  1. Leading timestamp prefixes -> stripped (core._strip_leading_timestamp;
     loops, handles the 15-16 stacks).
  2. HTML-tag-shaped strings + whole <aside> blocks -> stripped
     (core._sanitize_history_html).

Never hard-deletes: each changed file is copied byte-for-byte to
recollections/repair-histories-<ts>/ (mirrored path + sha256 MANIFEST.sha256)
before any write. Idempotent: a second run makes no further changes.
The _think field (compressed reasoning) is left verbatim — it is not her
delivered words and not the corruption vector.

Report: stdout + logs/repair_histories_<ts>.md
"""
import glob
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402  (reuses the compose-time machinery)

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_ROOT = os.path.join(ROOT, "recollections")
LOG_DIR = os.path.join(ROOT, "logs")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def repair_history_file(path, backup_dir, manifest):
    """Clean assistant entries in one history file. Returns
    (changed, stats)."""
    with open(path, "r", encoding="utf-8") as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        return False, {"entries": 0, "ts_prefix": 0, "html": 0}
    changed = False
    stats = {"entries": len(entries), "ts_prefix": 0, "html": 0}
    for e in entries:
        if not isinstance(e, dict) or e.get("role") != "assistant":
            continue
        c = e.get("content") or ""
        if not isinstance(c, str) or not c:
            continue
        orig = c
        c2 = core._strip_leading_timestamp(c)
        if c2 != c:
            stats["ts_prefix"] += 1
            c = c2
        c2 = core._sanitize_history_html(c)
        if c2 != c:
            stats["html"] += 1
            c = c2
        if c != orig:
            e["content"] = c
            e["repaired_histories"] = datetime.now().strftime(
                "%Y-%m-%dT%H:%M:%S")
            changed = True
    if changed:
        rel = os.path.relpath(path, ROOT)
        dest = os.path.join(backup_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        manifest[rel] = _sha256(path)
        tmp = path + ".repair.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    return changed, stats


def main():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BACKUP_ROOT, f"repair-histories-{ts}")
    manifest = {}
    grand = {"entries": 0, "ts_prefix": 0, "html": 0}
    files_changed = 0
    changed_list = []

    pattern = os.path.join(ROOT, "histories", "*_yaml", "*.json")
    for path in sorted(glob.glob(pattern)):
        # skip the summary sidecars and backup/surgery files — only the
        # live history stores
        base = os.path.basename(path)
        if base.endswith(".summary.json") or ".bak" in base or \
                ".purged" in base:
            continue
        try:
            changed, stats = repair_history_file(path, backup_dir, manifest)
        except Exception as exc:
            print(f"ERROR {path}: {exc}", file=sys.stderr)
            continue
        for k in grand:
            grand[k] += stats.get(k, 0)
        if changed:
            files_changed += 1
            changed_list.append({"file": os.path.relpath(path, ROOT), **stats})

    os.makedirs(backup_dir, exist_ok=True)
    with open(os.path.join(backup_dir, "MANIFEST.sha256"), "w",
              encoding="utf-8") as f:
        for rel, digest in sorted(manifest.items()):
            f.write(f"{digest}  {rel}\n")

    os.makedirs(LOG_DIR, exist_ok=True)
    report_path = os.path.join(LOG_DIR, f"repair_histories_{ts}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# histories ts-stacking repair pass — {ts}\n\n"
                f"- files changed: {files_changed}\n"
                f"- assistant entries scanned: {grand['entries']}\n"
                f"- entries with leading ts-prefixes stripped: "
                f"{grand['ts_prefix']}\n"
                f"- entries with HTML stripped: {grand['html']}\n"
                f"- originals: {backup_dir} ({len(manifest)} files, "
                f"MANIFEST.sha256)\n\n")
        for fc in changed_list:
            f.write(f"- `{fc['file']}`: entries={fc['entries']} "
                    f"ts_prefix={fc['ts_prefix']} html={fc['html']}\n")
    print(f"done: {files_changed} files changed, "
          f"{grand['ts_prefix']} ts-prefixed entries, "
          f"{grand['html']} html entries")
    print(f"backup: {backup_dir}")
    print(f"report: {report_path}")


if __name__ == "__main__":
    main()
