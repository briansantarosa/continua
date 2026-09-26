"""bookmark.py — the bookmark producer (phase 5).

The dumb capture-level stamp (data, not judgment — review round one #2):
when she or Alex says "remember this", the turn gets a bookmark that
tonight's Ritual honors (priority input, fuller text). Append-only log —
the chronicle itself is never rewritten (principle 1).

Pre-cutover: CLI for testing + manual stamping. At cutover: the bridge's
"remember this" handler and her memory-tool calls call bookmark() directly.

    python3 bookmark.py --uid <uid> [--by Alex] [--note "..."]
    python3 bookmark.py --list [--date YYYY-MM-DD]
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chronicle as ch

logger = logging.getLogger("continua.bookmark")

BASE = os.path.dirname(os.path.abspath(__file__))
BOOKMARKS_DIR = os.path.join(BASE, "ritual", "bookmarks")


def log_path(instance: str, date: str) -> str:
    return os.path.join(BOOKMARKS_DIR, instance, f"{date}.jsonl")


def find_record(root: str, instance: str, uid: str) -> dict | None:
    """Locate a record by uid across the mirror (small store — full scan)."""
    import glob
    for path in sorted(glob.glob(os.path.join(root, instance, "*", "*.jsonl"))):
        for r in ch.iter_records(path):
            if r.get("uid") == uid:
                return r
    return None


def bookmark(instance: str, uid: str, by: str = "Alex", note: str = "",
             root: str = ch.DEFAULT_ROOT, date: str = None) -> dict | None:
    """Stamp a turn. Append-only; the chronicle is never rewritten.
    The Ritual reads this log and boosts the scene containing the uid."""
    rec = find_record(root, instance, uid)
    if not rec:
        logger.warning("[Bookmark] uid not found in mirror: %s", uid)
        return None
    date = date or rec.get("ts", "")[:10] or datetime.now().strftime("%Y-%m-%d")
    entry = {
        "schema_version": 1,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "uid": uid,
        "record_date": rec.get("ts", "")[:10],
        "instance": instance,
        "person_id": rec.get("person_id", ""),
        "by": by,
        "note": note,
    }
    path = log_path(instance, date)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def bookmarks_for(instance: str, date: str) -> list:
    path = log_path(instance, date)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[Bookmark] log read failed (fail-open): %s", e)
    return out


def apply_to_records(records: list, instance: str, date: str) -> list:
    """Ritual-side wiring: flag records bookmarked for this date (in-memory
    only — the chronicle stays untouched; capture stays dumb)."""
    uids = {b["uid"] for b in bookmarks_for(instance, date)}
    for r in records:
        if r.get("uid") in uids:
            r["bookmark"] = True
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", help="record uid to bookmark")
    ap.add_argument("--by", default="Alex")
    ap.add_argument("--note", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--instance", default="residenta")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.list:
        for b in bookmarks_for(args.instance, args.date):
            print(json.dumps(b, ensure_ascii=False))
        return
    if not args.uid:
        ap.error("--uid required (or --list)")
    res = bookmark(args.instance, args.uid, by=args.by, note=args.note)
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
