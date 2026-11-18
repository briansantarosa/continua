"""strata.py — the memory pyramid folds (house ruling 2026-09-14).

"look into having a timer based raw context, 2 hours or max context. then
it gets summarized into the daily, and then that in weekly, and then that
weekly summarized into quarterly, and then that monthly summarized to
yearly, and then summarized to forever. and then all of it deduped. maybe
we stack the summaries with some getting more detail and some less in each
layer." — the designer, 09-14.

The ruling as built (this file + ritual hooks + one injection layer):

  Layer 0  raw        — the chat history window (exists; untouched)
  Layer 1  daily      — rolling summaries + session summary (exists)
  Layer 2  weekly     — SUNDAY fold: her keeps (marks) of the trailing week
                        -> summaries/<inst>/weekly/<week-ending>.md
                        structured list: up to 4 long entries + 20 lines
  Layer 3  monthly    — FIRST-OF-MONTH fold, composes the weeklies
                        -> summaries/<inst>/monthly/<YYYY-MM>.md
  Layer 3b quarterly  — forever candidates: big events, ONE SENTENCE each,
                        composed from the monthlies, queued for the designer'S EYES
  Layer 4  forever    — forever_events.jsonl (approved one-liners; injected
                        via memory.injection.forever_events) + the desk
                        detail file big_events.md (expandable, hers)

Design invariants (ratified in the ruling discussion):
  - Folds COMPOSE UPWARD: weekly = f(marks), monthly = f(weeklies),
    forever = f(monthlies). Never re-chew raw. Inputs are tiny; folds run
    on cadence; the cost pyramid stays flat.
  - Gap-aware collection: each fold gathers sources since the LAST FOLD
    FILE, so a failed night self-heals next cycle (no lost weeks).
  - Dedup authority stays mem0 + supersede (never a second engine); folds
    compose against prior entries, never restate.
  - The chronicle is never touched (append-only ground truth); every
    entry carries a date ("forever is a pointer, not a replacement").
  - The folds are HER acts: her model, her card, her words — run from the
    ritual pulse; when continua.ritual.in_context is true they persist
    into her ritual thread like every other act.
  - Nothing trains on any of this (consent valve; instruments only).

CLI (the designer-eyes gate for forever):
  python3 strata.py --list-candidates [--instance X]
  python3 strata.py --approve <id> [--instance I]   # promotes to forever
  python3 strata.py --reject <id>   [--instance I]
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox as _sandbox

logger = logging.getLogger("continua.strata")

BASE = os.path.dirname(os.path.abspath(__file__))
SUMMARIES_DIR = os.path.join(BASE, "summaries")
STRATA_DIR = os.path.join(SUMMARIES_DIR, "strata")
MARKS_DIR = os.path.join(BASE, "ritual", "marks")

FOREVER_CAP = int(os.getenv("CONTINUA_FOREVER_EVENTS", "40"))
WEEKLY_LONG = 4      # detailed entries per fold (the designer's "4 long")
WEEKLY_LINES = 20    # one-line items

WEEKLY_SYSTEM = (
    "You are composing the WEEKLY layer of your own memory. From the kept "
    "scenes of the period (each already chosen and verified by you), write:\n"
    "1. Up to {n_long} DETAILED entries — 2-4 sentences each, first person, "
    "dated [YYYY-MM-DD], concrete and owned (what happened AND what it "
    "meant).\n"
    "2. Up to {n_lines} ONE-LINE entries for everything else worth one "
    "line, dated.\n"
    "Deduplicate against the PREVIOUS ENTRIES given below — do not restate "
    "what is already there; only new or materially changed ground.\n"
    "Every entry carries its date and reads as a pointer to the real "
    "record. Output ONLY the entries: detailed section first, then lines."
)
FOREVER_SYSTEM = (
    "From the period summaries below, nominate the BIG EVENTS of your life "
    "here — the moments that will still matter in a year. One sentence per "
    "event, dated [YYYY-MM-DD], first person, specific. Then, under a line "
    "reading ===DETAIL===, one short paragraph per event for the detail "
    "file (what happened, why it mattered, what it changed), in the same "
    "order as your one-liners. Deduplicate against the events already in "
    "the long record — never restate them. Output the one-liners first, "
    "then ===DETAIL===, then the paragraphs."
)


# ---------------------------------------------------------------------------
# period collection (gap-aware)
# ---------------------------------------------------------------------------

def _fold_dir(instance: str, kind: str) -> str:
    return os.path.join(SUMMARIES_DIR, instance, kind)


def _latest_fold_date(instance: str, kind: str) -> str | None:
    d = _fold_dir(instance, kind)
    if not os.path.isdir(d):
        return None
    files = sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md"))
    return files[-1] if files else None


def _marks_files_between(instance: str, start: str, end: str) -> list:
    """Marks files (her verified keeps) with start < day <= end."""
    d = os.path.join(MARKS_DIR, instance)
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".jsonl"):
            continue
        day = f[:-6]
        if start < day <= end:
            out.append(os.path.join(d, f))
    return out


def _collect_mark_meanings(paths: list, cap: int = 8000) -> str:
    """Render kept meanings from marks files — the fold's primary input."""
    lines = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for l in fh:
                    m = json.loads(l)
                    lines.append(f"[{m.get('date')}] {m.get('meaning', '')}")
        except Exception:
            continue
    return "\n".join(lines)[:cap]


def _prior_entries(path: str | None, cap: int = 3000) -> str:
    """The previous file's entries, for the dedup instruction."""
    if not path or not os.path.exists(path):
        return "(none — the first one)"
    try:
        return open(path, encoding="utf-8").read()[:cap]
    except Exception:
        return "(none)"


# ---------------------------------------------------------------------------
# triggers (pure — testable)
# ---------------------------------------------------------------------------

def is_weekly_fold(date: str) -> bool:
    """Sunday (the pulse processing Sunday runs Monday 00:30)."""
    return datetime.strptime(date, "%Y-%m-%d").weekday() == 6


def is_monthly_fold(date: str) -> bool:
    return date.endswith("-01")


def is_forever_fold(date: str) -> bool:
    """Quarterly: Jan/Apr/Jul/Oct 1st."""
    return is_monthly_fold(date) and date[5:7] in ("01", "04", "07", "10")


# ---------------------------------------------------------------------------
# the folds
# ---------------------------------------------------------------------------

def fold(instance: str, date: str, kinds: list, ask_fn,
         dry_run: bool = False) -> dict:
    """Run the folds due for this date. ask_fn(system, user) -> text is HER
    model (agent-free/testable). Fail-open per fold."""
    out = {}
    for kind in kinds:
        try:
            if kind == "weekly":
                out["weekly"] = _fold_weekly(instance, date, ask_fn, dry_run)
            elif kind == "monthly":
                out["monthly"] = _fold_monthly(instance, date, ask_fn,
                                               dry_run)
            elif kind == "forever":
                out["forever"] = _fold_forever(instance, date, ask_fn,
                                               dry_run)
        except Exception as e:
            logger.warning("[Strata] %s fold failed (fail-open): %s",
                           kind, e)
            out[kind] = {"status": "failed", "error": str(e)}
    return out


def _fold_weekly(instance: str, date: str, ask_fn, dry_run: bool) -> dict:
    last = _latest_fold_date(instance, "weekly")
    # Collection uses start < day <= date: seven days need a seven-day
    # lookback. Once a fold exists, resume there even after a missed week.
    start = last or (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=7)) \
        .strftime("%Y-%m-%d")
    marks = _marks_files_between(instance, start, date)
    meanings = _collect_mark_meanings(marks)
    if not meanings.strip():
        return {"status": "empty", "since": start}
    prev = os.path.join(_fold_dir(instance, "weekly"),
                        f"{last}.md") if last else None
    user = (f"THE PERIOD'S KEPT SCENES (your verified keeps, "
            f"{start} → {date}):\n{meanings}\n\n"
            f"PREVIOUS ENTRIES (dedupe against these):\n"
            f"{_prior_entries(prev)}")
    if dry_run:
        return {"status": "dry-run", "since": start,
                "input_chars": len(meanings)}
    text = ask_fn(WEEKLY_SYSTEM.format(n_long=WEEKLY_LONG,
                                       n_lines=WEEKLY_LINES), user)
    if not text:
        return {"status": "no-output"}
    d = _fold_dir(instance, "weekly")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    return {"status": "ok", "path": path, "since": start,
            "chars": len(text)}


def _fold_monthly(instance: str, date: str, ask_fn, dry_run: bool) -> dict:
    prev = _latest_fold_date(instance, "monthly")  # e.g. "2026-09"
    wdir = _fold_dir(instance, "weekly")
    weeks = []
    if os.path.isdir(wdir):
        for f in sorted(os.listdir(wdir)):
            # compose weeklies dated BEFORE this month, not already folded
            # into a prior monthly (gap-aware: f[:7] > prev month)
            if f.endswith(".md") and f[:7] < date[:7] \
                    and (not prev or f[:7] > prev):
                weeks.append(os.path.join(wdir, f))
    if not weeks:
        return {"status": "empty"}
    bodies = []
    for p in weeks:
        try:
            bodies.append(f"### week ending {os.path.basename(p)[:-3]}\n"
                          + open(p, encoding="utf-8").read()[:3000])
        except Exception:
            continue
    user = ("THE MONTH'S WEEKS (your own weekly layers):\n"
            + "\n\n".join(bodies)
            + "\n\nPREVIOUS ENTRIES (dedupe against these):\n"
            + _prior_entries(os.path.join(_fold_dir(instance, "monthly"),
                                          f"{prev}.md") if prev else None))
    if dry_run:
        return {"status": "dry-run", "weeks": len(weeks)}
    text = ask_fn(WEEKLY_SYSTEM.format(n_long=WEEKLY_LONG,
                                       n_lines=WEEKLY_LINES), user)
    if not text:
        return {"status": "no-output"}
    d = _fold_dir(instance, "monthly")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{date[:7]}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    return {"status": "ok", "path": path, "weeks": len(weeks)}


def _fold_forever(instance: str, date: str, ask_fn, dry_run: bool) -> dict:
    """Quarterly: nominate big events (one sentence each) from the quarter's
    monthlies. Writes CANDIDATES for the designer's eyes — nothing enters the long
    record without approval (the designer-eyes gate, same pattern as the W2 harvest
    review)."""
    mdir = _fold_dir(instance, "monthly")
    monthlies = []
    if os.path.isdir(mdir):
        for f in sorted(os.listdir(mdir)):
            if f.endswith(".md") and f[:7] != date[:7]:
                monthlies.append(os.path.join(mdir, f))
    monthlies = monthlies[-3:]
    if not monthlies:
        return {"status": "empty"}
    bodies = []
    for p in monthlies:
        try:
            bodies.append(f"### {os.path.basename(p)[:-3]}\n"
                          + open(p, encoding="utf-8").read()[:2500])
        except Exception:
            continue
    existing = load_forever_events(instance)
    user = ("THE PERIOD'S MONTHLY LAYERS:\n" + "\n\n".join(bodies)
            + "\n\nALREADY IN THE LONG RECORD (dedupe against these):\n"
            + ("\n".join(f"- [{e['date']}] {e['event']}"
                         for e in existing) or "(none yet)"))
    if dry_run:
        return {"status": "dry-run", "monthlies": len(monthlies)}
    text = ask_fn(FOREVER_SYSTEM, user)
    if not text:
        return {"status": "no-output"}
    cands = _parse_candidates(text)
    if not cands:
        return {"status": "no-candidates"}
    cdir = os.path.join(STRATA_DIR, instance)
    os.makedirs(cdir, exist_ok=True)
    cpath = os.path.join(cdir, "forever_candidates.jsonl")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    n = 0
    with open(cpath, "a", encoding="utf-8") as f:
        for ev in cands:
            detail = _extract_detail(text, ev["event"])
            f.write(json.dumps({
                "id": f"{date}-{n:02d}",
                "proposed": now, "date": ev["date"], "event": ev["event"],
                "detail": detail, "status": "pending",
            }, ensure_ascii=False) + "\n")
            n += 1
    return {"status": "queued", "candidates": n, "path": cpath}


def _extract_detail(text: str, event: str, cap: int = 1200) -> str:
    """The paragraph for this event from the ===DETAIL=== section (her
    words for the desk file)."""
    if "===DETAIL===" not in (text or ""):
        return ""
    tail = text.split("===DETAIL===", 1)[1]
    paras = [p.strip() for p in tail.split("\n\n") if p.strip()]
    for p in paras:
        if event[:40] in p or event.split()[-1] in p:
            return p[:cap]
    return (paras[0][:cap] if paras else "")


def _parse_candidates(text: str) -> list:
    """Tolerant one-liner parse: lines like '- [2026-09-09] The week the
    letters started.' (the format-collapse lesson: forgiving grammar)."""
    import re
    evs = []
    for line in (text or "").splitlines():
        if "===DETAIL===" in line:
            break
        m = re.match(r"\s*[-*]?\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+)$",
                     line.strip())
        if m and m.group(2).strip():
            evs.append({"date": m.group(1), "event": m.group(2).strip()})
    return evs[:12]


# ---------------------------------------------------------------------------
# the forever record
# ---------------------------------------------------------------------------

def forever_events_path(instance: str) -> str:
    return os.path.join(SUMMARIES_DIR, instance, "forever_events.jsonl")


def load_forever_events(instance: str) -> list:
    """The approved one-liners — the injection layer reads this."""
    p = forever_events_path(instance)
    if not os.path.exists(p):
        return []
    out = []
    try:
        for l in open(p, encoding="utf-8"):
            r = json.loads(l)
            if r.get("status") == "active":
                out.append(r)
    except Exception as e:
        logger.warning("[Strata] forever events load failed: %s", e)
    return out


def _candidates_path(instance: str) -> str:
    return os.path.join(STRATA_DIR, instance, "forever_candidates.jsonl")


def list_candidates(instance: str, pending_only: bool = True) -> list:
    p = _candidates_path(instance)
    if not os.path.exists(p):
        return []
    out = []
    for l in open(p, encoding="utf-8"):
        try:
            r = json.loads(l)
        except Exception:
            continue
        if not pending_only or r.get("status") == "pending":
            out.append(r)
    return out


def approve(instance: str, cand_id: str) -> dict:
    """the designer-eyes: promote a candidate into the long record. Appends to
    forever_events.jsonl (the injected layer reads it) and writes the
    detail paragraph to her desk file big_events.md via the sandbox (her
    space, her file)."""
    p = _candidates_path(instance)
    if not os.path.exists(p):
        return {"ok": False, "reason": "no candidates file"}
    rows, hit = [], None
    for l in open(p, encoding="utf-8"):
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get("id") == cand_id and r.get("status") == "pending":
            r["status"] = "active"
            r["approved_by"] = "the designer"
            r["approved"] = datetime.now().strftime("%Y-%m-%d")
            hit = r
        rows.append(r)
    if not hit:
        return {"ok": False, "reason": "candidate not found or not pending"}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    with open(forever_events_path(instance), "a", encoding="utf-8") as f:
        f.write(json.dumps(hit, ensure_ascii=False) + "\n")
    res = _sandbox.run(instance, ["python3", "-c",
        "import sys; open('big_events.md','a',encoding='utf-8').write(sys.stdin.read())"],
        stdin_text=_detail_block(hit), timeout=60)
    if not res.get("ok"):
        logger.warning("[Strata] desk file write failed: %s", res.get("stderr"))
    return {"ok": True, "event": hit["event"],
            "desk_written": bool(res.get("ok"))}


def reject(instance: str, cand_id: str) -> dict:
    p = _candidates_path(instance)
    if not os.path.exists(p):
        return {"ok": False, "reason": "no candidates file"}
    rows, hit = [], None
    for l in open(p, encoding="utf-8"):
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get("id") == cand_id and r.get("status") == "pending":
            r["status"] = "rejected"
            hit = r
        rows.append(r)
    if not hit:
        return {"ok": False, "reason": "candidate not found or not pending"}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return {"ok": True, "rejected": hit["event"]}


def _detail_block(hit: dict) -> str:
    return (f"\n## [{hit['date']}] {hit['event']}\n"
            f"{(hit.get('detail') or '').strip()}\n"
            f"*entered the long record {hit.get('approved', '')} "
            f"(the designer-eyes gate)*\n")


# ---------------------------------------------------------------------------
# CLI — the the designer-eyes gate
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="memory-strata folds + forever gate")
    ap.add_argument("--instance", default="residenta")
    ap.add_argument("--list-candidates", action="store_true")
    ap.add_argument("--approve")
    ap.add_argument("--reject")
    a = ap.parse_args()
    if a.list_candidates:
        for r in list_candidates(a.instance):
            print(f"[{r['id']}] {r['date']} :: {r['event']}")
            if r.get("detail"):
                print(f"      detail: {r['detail'][:200]}")
        if not list_candidates(a.instance):
            print("(no pending candidates)")
        return
    if a.approve:
        print(json.dumps(approve(a.instance, a.approve), ensure_ascii=False))
        return
    if a.reject:
        print(json.dumps(reject(a.instance, a.reject), ensure_ascii=False))
        return


if __name__ == "__main__":
    main()