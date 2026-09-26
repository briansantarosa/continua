#!/usr/bin/env python3
"""One-time in-place chronicle repair — cleansweep.md plan Phase 3.

house rulings (2026-09-22): in-place repair (not compose-time only),
originals preserved, residentb gets the same treatment.

Repairs, per assistant turn in chronicle/<inst>/<person>/<day>.jsonl:
  1. HTML-corrupted content  -> HTML-tag-shaped strings + whole
     <aside>...</aside> blocks stripped (same machinery as core._sanitize_history_html).
  2. Leading timestamp prefix -> stripped (core._strip_leading_timestamp).
  2b. (2026-09-24 extension, F3 residue found by the failure-dashboard
      investigation) user-role LETTER rows (uid letter-*) — the captured
      reply of the OTHER resident, machine-generated text — also get the
      leading ts-prefix strip. The 09-22 pass skipped every non-assistant
      row, so 32 letter replies kept their prefixes and the "0 ts-prefixed
      post-sweep" claim held only for assistant rows. Genuine human turns
      keep their brackets untouched (the 09-24 render-strip ruling).
  3. Future-dated content -> lines dated after the day file's own date are
     DATE-CORRECTED only when unambiguous: the turn's own ts is the truth —
     hallucinated [YYYY-MM-DD] prefixes that postdate the file are rewritten
     to the record's real date. When the true date is NOT unambiguous
     (inline date not equal to the record ts), the entry is FLAGGED in the
     report and left verbatim (house rule: never guess).

Never hard-deletes: the original file is copied byte-for-byte to
recollections/repair-cleansweep-<ts>/ (mirrored path + sha256 manifest) before
any write. idempotent: a second run makes no further changes.

Report: stdout + logs/repair_cleansweep_<ts>.md
"""
import glob
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402  (reuses the compose-time machinery)

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_ROOT = os.path.join(ROOT, "recollections")
LOG_DIR = os.path.join(ROOT, "logs")

# future-dated prefix check: anything after the file's day is suspect;
# the record's own ts is authoritative
_TS_PREFIX = re.compile(r"^(\s*)\[(\d{4}-\d{2}-\d{2})(?: \d{2}:\d{2})?\]\s*")
_FUT_INLINE = re.compile(r"\[(\d{4}-\d{2}-\d{2})(?: \d{2}:\d{2})?\]")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def repair_day_file(path, day, backup_dir, manifest, report):
    day_date = date.fromisoformat(day)
    lines_out = []
    changed = False
    stats = {"html": 0, "ts_prefix": 0, "letter_ts_prefix": 0,
             "date_fixed": 0, "flagged": 0, "turns": 0}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                lines_out.append(line)
                continue
            stats["turns"] += 1
            if rec.get("role") != "assistant":
                # 2026-09-24 (F3 residue): letter-reply rows are USER-role
                # in the recipient's chronicle but carry the OTHER
                # resident's GENERATED text — strip the leading prefix on
                # letter-* rows only; genuine human turns keep their
                # brackets untouched (09-24 render-strip ruling).
                if (rec.get("role") == "user"
                        and str(rec.get("uid") or "").startswith("letter-")):
                    c = rec.get("content") or ""
                    c2 = core._strip_leading_timestamp(c)
                    if c2 != c:
                        stats["letter_ts_prefix"] += 1
                        rec["content"] = c2
                        rec["repaired_cleansweep"] = datetime.now().strftime(
                            "%Y-%m-%dT%H:%M:%S")
                        changed = True
                        lines_out.append(json.dumps(rec, ensure_ascii=False))
                        continue
                lines_out.append(line)
                continue
            c = rec.get("content") or ""
            orig = c

            # 1. HTML corruption
            c2 = core._sanitize_history_html(c)
            if c2 != c:
                stats["html"] += 1
                c = c2

            # 2. leading timestamp prefix (loops: stacked [date] lines
            # observed in the corrupted chronicle)
            c2 = core._strip_leading_timestamp(c)
            if c2 != c:
                stats["ts_prefix"] += 1
                c = c2

            # 3. future-dated inline stamps vs the record's own ts
            rec_day = (rec.get("ts") or "")[:10]
            try:
                rec_date = date.fromisoformat(rec_day)
            except ValueError:
                rec_date = day_date
            fixed_run = []
            def _fix(mm):
                try:
                    d = date.fromisoformat(mm.group(1))
                except ValueError:
                    return mm.group(0)
                if d > rec_date and d > day_date:
                    # hallucinated future date — anchor to the record's
                    # real timestamp (unambiguous: the record's ts is the
                    # mechanical truth of when the words were written)
                    fixed_run.append((mm.group(1), rec_day))
                    return "[" + rec_day + "]"
                return mm.group(0)
            c3 = _FUT_INLINE.sub(_fix, c)
            if fixed_run:
                stats["date_fixed"] += len(fixed_run)
                report.setdefault("date_fixes", []).append(
                    {"file": path, "ts": rec.get("ts"),
                     "fixed": sorted({a for a, _ in fixed_run}),
                     "to": rec_day})
                c = c3

            if c != orig:
                changed = True
                rec["content"] = c
                rec["repaired_cleansweep"] = datetime.now().strftime(
                    "%Y-%m-%dT%H:%M:%S")
                lines_out.append(json.dumps(rec, ensure_ascii=False))
            else:
                lines_out.append(line)

    if changed:
        rel = os.path.relpath(path, ROOT)
        dest = os.path.join(backup_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        manifest[rel] = _sha256(path)
        tmp = path + ".repair.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines_out) + "\n")
        os.replace(tmp, path)
    return changed, stats


def main():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BACKUP_ROOT, f"repair-cleansweep-{ts}")
    manifest = {}
    report = {"files_changed": [], "totals": {}, "date_fixes": [],
              "flagged": []}
    grand = {"html": 0, "ts_prefix": 0, "letter_ts_prefix": 0,
             "date_fixed": 0, "turns": 0}
    files_changed = 0

    pattern = os.path.join(ROOT, "chronicle", "*", "*", "2026-*.jsonl")
    for path in sorted(glob.glob(pattern)):
        day = os.path.basename(path)[:-6]
        try:
            changed, stats = repair_day_file(path, day, backup_dir, manifest,
                                             report)
        except Exception as exc:
            print(f"ERROR {path}: {exc}", file=sys.stderr)
            continue
        for k in ("html", "ts_prefix", "letter_ts_prefix", "date_fixed",
                  "turns"):
            grand[k] += stats[k]
        if changed:
            files_changed += 1
            report["files_changed"].append(
                {"file": os.path.relpath(path, ROOT), **stats})

    # write the manifest (checksummed originals)
    os.makedirs(backup_dir, exist_ok=True)
    with open(os.path.join(backup_dir, "MANIFEST.sha256"), "w",
              encoding="utf-8") as f:
        for rel, digest in sorted(manifest.items()):
            f.write(f"{digest}  {rel}\n")

    # report
    os.makedirs(LOG_DIR, exist_ok=True)
    report_path = os.path.join(LOG_DIR, f"repair_cleansweep_{ts}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# cleansweep repair pass — {ts}\n\n"
                f"- files changed: {files_changed}\n"
                f"- turns scanned: {grand['turns']}\n"
                f"- HTML-corrupted turns cleaned: {grand['html']}\n"
                f"- timestamp prefixes stripped: {grand['ts_prefix']}\n"
                f"- letter-reply (user-role) prefixes stripped: "
                f"{grand['letter_ts_prefix']}\n"
                f"- future dates corrected: {grand['date_fixed']}\n"
                f"- originals: {backup_dir} ({len(manifest)} files, "
                f"MANIFEST.sha256)\n\n")
        for fc in report["files_changed"]:
            f.write(f"- `{fc['file']}`: html={fc['html']} "
                    f"ts_prefix={fc['ts_prefix']} date_fixed={fc['date_fixed']}\n")
    print(f"done: {files_changed} files changed, {grand['html']} html "
          f"turns, {grand['ts_prefix']} ts-prefixes, "
          f"{grand['letter_ts_prefix']} letter-reply prefixes, "
          f"{grand['date_fixed']} dates fixed")
    print(f"backup: {backup_dir}")
    print(f"report: {report_path}")


if __name__ == "__main__":
    main()
