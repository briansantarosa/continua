"""last_touch.py — the people-book v2 index (helpnany.md Phase 4a,
2026-09-28). One line per roster person: WHEN you last really exchanged
with them, from the chronicle (the record of record — wakes, letters,
chats all land there). Feeds the roster's context clause:

    - Sequoia (2220002222) — friend; you last wrote about porchfest (09-14)

The roster alone is a phone book: 18 names, zero relationships. She cannot
think about people she knows nothing about. This index is the relationship
substrate the 09-15 people-book cut removed, rebuilt from HER OWN record —
nothing invented, nothing remembered that didn't happen.

Design:
  - build_index(instance, root): walk chronicle/<instance>/<person>/ day
    files, newest-first, keep the latest exchange per person + its date.
  - Cached in-process per (instance) with the day-file mtimes as the cache
    key — a wake recomputes at most once per data change; chat turns ride
    the cache. (Plan's "computed at wake, cached" ruling.)
  - person_id keying matches the roster exactly (chronicle dirs ARE
    person ids, plus system-* pseudo-persons which are skipped for the
    roster join but available for callers that want them).
  - Fail-open everywhere: any error returns {} (roster renders
    names-only, the pre-Phase-4 bytes).

Pure read surface. Run tests: last_touch_test.py
"""
import glob
import json
import logging
import os

import turns as _turns

logger = logging.getLogger("continua.last_touch")

_CACHE = {"key": None, "index": {}}


def _outbox_mtime(instance, root):
    """Newest mtime across the outbox files for this instance (0 when
    none) — memorybuild Phase 1 (W2): a wake-origin send changes whose turn
    it is, and it exists ONLY in the outbox; the cache key must see it."""
    try:
        d = os.path.join(os.path.dirname(os.path.abspath(str(root))),
                         "outbound", instance)
        return max((os.path.getmtime(os.path.join(d, f))
                    for f in os.listdir(d) if f.endswith(".jsonl")),
                   default=0.0)
    except OSError:
        return 0.0


def build_index(instance: str, root: str, max_days: int = 400) -> dict:
    """{person_id: {"last_ts": iso, "last_role": role, "days_ago": int,
    "excerpt": str}} — the latest record per chronicle person dir.
    Excerpt: first 90 chars of the person's OWN latest line (role==user
    preferred — what THEY last said — else the latest record).
    memorybuild Phase 1 (W2): people who exist ONLY in the outbox (her wake
    sends, never chatted) also earn entries — her words exist."""
    base = os.path.join(root, instance)
    if not os.path.isdir(base):
        return {}
    # cache key: newest mtime across person dirs (cheap stat, no reads)
    mtimes = []
    for person in os.listdir(base):
        pdir = os.path.join(base, person)
        if person.startswith("_") or not os.path.isdir(pdir):
            continue
        try:
            mtimes.append(os.path.getmtime(pdir))
        except OSError:
            continue
    key = (instance, max(mtimes) if mtimes else 0,
           _outbox_mtime(instance, root))
    if _CACHE["key"] == key:
        return _CACHE["index"]

    import datetime as _dt
    today = _dt.date.today()
    index = {}
    for person in os.listdir(base):
        pdir = os.path.join(base, person)
        if person.startswith("_") or not os.path.isdir(pdir):
            continue
        days = sorted(glob.glob(os.path.join(pdir, "*.jsonl")))[-max_days:]
        latest = None          # any latest record
        latest_own = None      # latest record the PERSON said (role=user)
        for path in reversed(days):
            day = os.path.basename(path)[:-6]
            stop = False
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in reversed(f.readlines()[-2000:]):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = str(r.get("ts") or "")
                        if not ts:
                            continue
                        if latest is None:
                            latest = r
                        if latest_own is None and r.get("role") == "user":
                            latest_own = r
                        if latest and latest_own:
                            stop = True
                            break
            except OSError:
                continue
            if stop:
                break
        rec = latest_own or latest
        if not rec:
            continue
        try:
            d = _dt.date.fromisoformat(str(rec.get("ts", ""))[:10])
            days_ago = (today - d).days
        except ValueError:
            days_ago = None
        excerpt = str(rec.get("content") or "")[:90].replace("\n", " ")
        entry = {"last_ts": str(rec.get("ts") or ""),
                 "last_role": rec.get("role"),
                 "days_ago": days_ago,
                 "excerpt": excerpt}
        # [CONTINUA] memorybuild.md Phase 1 (W2): the exchange pair fields —
        # what her last word said and what (if anything) answered it, from
        # the MERGED timeline (chronicle + outbox — a wake send is her word
        # too). Purely additive: the legacy fields stay chronicle-only, so
        # the pre-Phase-1 clause renders byte-identical when the gate is
        # off. WAKE_CLASS dirs never enter (they are not people).
        if person not in _turns.WAKE_CLASS:
            try:
                _ev = _turns.last_events(root, instance, person)
                if _ev:
                    _last = _ev[-1]
                    _prev = None
                    for _e in reversed(_ev[:-1]):
                        if _e.get("role") != _last.get("role"):
                            _prev = _e
                            break
                    entry["exchange"] = {
                        "last_role": _last.get("role"),
                        "last_text": str(_last.get("text") or ""),
                        "prev_role": (_prev or {}).get("role"),
                        "prev_text": str((_prev or {}).get("text") or ""),
                        "last_ts": str(_last.get("ts") or ""),
                        "source": _last.get("source"),
                    }
            except Exception:
                pass  # fail-open: the clause falls back to the legacy fields
        index[person] = entry
    # outbox-only people (memorybuild W2): her sends exist even without a
    # chronicle dir; they get an entry too (roster join still governs who
    # renders).
    try:
        ob_dir = os.path.join(os.path.dirname(os.path.abspath(str(root))),
                              "outbound", instance)
        import datetime as _dt3
        by_pid = {}
        for fname in sorted(os.listdir(ob_dir)):
            if not fname.endswith(".jsonl"):
                continue
            fday = fname[:-6]
            if fday < (today - _dt3.timedelta(days=1)).strftime("%Y-%m-%d"):
                continue
            with open(os.path.join(ob_dir, fname), encoding="utf-8") as _f:
                for _line in _f:
                    try:
                        _r = json.loads(_line)
                    except Exception:
                        continue
                    _pid = str(_r.get("person_id") or "")
                    if not _pid or _pid in index or _pid in by_pid:
                        continue
                    if not (_r.get("delivered") or _r.get("queued")):
                        continue
                    if str(_r.get("kind") or "human") == "letter":
                        continue
                    if _r.get("arrived_muted"):
                        continue
                    by_pid[_pid] = [{"ts": str(_r.get("ts") or ""),
                                     "role": "assistant",
                                     "text": str(_r.get("text") or ""),
                                     "source": "outbox"}]
        for _pid, _ev in by_pid.items():
            index[_pid] = _entry_from_outbox(instance, root, _pid, _ev, today)
    except Exception:
        pass  # fail-open: outbox-only people just stay absent
    _CACHE["key"] = key
    _CACHE["index"] = index
    return index


def _entry_from_outbox(instance, root, person, events, today):
    """memorybuild Phase 1 (W2): a person she has ONLY ever written to from
    wakes (outbox-only — no chronicle dir) still earns an index entry; her
    words exist. Build the legacy fields from the merged timeline."""
    import datetime as _dt2
    rec = events[-1]
    try:
        d = _dt2.date.fromisoformat(str(rec.get("ts") or "")[:10])
        days_ago = (today - d).days
    except ValueError:
        days_ago = None
    entry = {"last_ts": str(rec.get("ts") or ""),
             "last_role": rec.get("role"), "days_ago": days_ago,
             "excerpt": str(rec.get("text") or "")[:90].replace("\n", " ")}
    _prev = None
    for _e in reversed(events[:-1]):
        if _e.get("role") != rec.get("role"):
            _prev = _e
            break
    entry["exchange"] = {"last_role": rec.get("role"),
                         "last_text": str(rec.get("text") or ""),
                         "prev_role": (_prev or {}).get("role"),
                         "prev_text": str((_prev or {}).get("text") or ""),
                         "last_ts": str(rec.get("ts") or ""),
                         "source": rec.get("source")}
    return entry


def context_clause(person_id: str, entry: dict, person_kind: str = "human",
                   max_chars: int = 90, exchange: bool = False) -> str:
    """The roster clause for one person, from their index entry. Factual,
    hers, no invention:

      'you last wrote about porchfest (09-14)'  ← their own last line
      '(no exchanges on record)'                ← honest when absent

    memorybuild.md Phase 1 (W2) — with exchange=True (gate
    persona.last_touch_exchange) and a merged-timeline exchange recorded,
    the clause shows what a reply ANSWERED (the 20-Questions fix): a
    one-word reply is meaningless without the question it answered.
      their reply is short:  … — you wrote: "<her last sentence>" — they said: "<their reply>"
      her word is latest:    … — you wrote last: "<her last sentence>" (no reply yet)
    A long reply keeps the legacy rendering (it carries its own context).
    Sentences never clip mid-word (turns.last_sentence).
    """
    if not entry:
        return "no exchanges on record"
    when = ""
    days = entry.get("days_ago")
    if days is not None:
        when = "today" if days == 0 else (
            f"yesterday" if days == 1 else f"{days}d ago")
    ts10 = str(entry.get("last_ts") or "")[:10]
    if when and ts10:
        when = f"{when}, {ts10}"
    elif ts10:
        when = ts10
    x = entry.get("exchange") or {}
    if exchange and x.get("last_role"):
        _last_txt = str(x.get("last_text") or "")
        if x.get("last_role") == "user":
            if len(_last_txt) < 40:
                mine = _turns.last_sentence(str(x.get("prev_text") or ""))
                theirs = _turns.last_sentence(_last_txt)
                return (f"last contact {when} — you wrote: \"{mine}\""
                        f" — they said: \"{theirs}\"")
        else:
            mine = _turns.last_sentence(_last_txt)
            return (f"last contact {when} — you wrote last: \"{mine}\""
                    " (no reply yet)")
    excerpt = (entry.get("excerpt") or "").strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars - 1].rstrip() + "…"
    role = entry.get("last_role")
    theirs = "they said" if role == "user" else "last record"
    if excerpt:
        return (f"last contact {when} — {theirs}: “{excerpt}”"
                .replace("“", "\"").replace("”", "\""))
    return f"last contact {when}".strip(", ")
