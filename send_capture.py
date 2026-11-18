"""send_capture.py — memorybuild.md Phase 1 (W4): words she says to a
person join the conversation they belong to.

Verified fact (09-28): a delivered HUMAN send from a wake exists ONLY in
the outbox — the chronicle has no row and the person's chat-thread history
never sees it. Her own memory system couldn't remember what she told
Sequoia from a wake, and when she next opened a chat with that person the
thread didn't know her own words.

Design:
  - OUTBOX-DRIVEN: the outbox is the durable structured record (person_id,
    text, ts, delivered) — no tool-format parsing. The bridge calls
    reconcile() after every turn (wake consumer AND the chat path); the
    reconcile processes not-yet-captured delivered non-letter rows.
  - TWO captures per row, both append-only:
      1. chronicle row (role assistant, source send_message) — her words
         enter HER record; the recollection scan sees them.
      2. the person's chat-thread history (in-memory dict + disk file) —
         wake-origin sends carry the prefix "[sent from a wake at HH:MM] "
         (honest about the surface); chat-origin sends join bare.
  - IDEMPOTENT: a cursor (wakes/<instance>/send_capture.json, bookkeeping
    only — never her words) plus a defense-in-depth check (existing
    chronicle uid / existing identical history entry) means a crash or
    restart mid-reconcile cannot duplicate anything.
  - CONCURRENCY: a per-person threading.Lock guards the history append;
    the in-memory list is the SAME object the chat path saves, so the
    next chat save carries the row. Residual race (a chat turn replacing
    the list object mid-append) is accepted and documented — the
    chronicle capture, which feeds her memory, is immune to it.
  - GATE: continua.wake.send_capture (default false); env
    CONTINUA_SEND_CAPTURE=0 wins. Fail-open everywhere.
"""
import datetime
import hashlib
import json
import logging
import os
import threading

logger = logging.getLogger("continua.send_capture")

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def enabled(instance, config_dir=None):
    """Gate: continua.wake.send_capture; CONTINUA_SEND_CAPTURE=0 wins."""
    if os.getenv("CONTINUA_SEND_CAPTURE", "1") == "0":
        return False
    try:
        import yaml
        d = config_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "configs")
        with open(os.path.join(d, f"{instance}.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return bool(((cfg.get("continua") or {}).get("wake") or {})
                    .get("send_capture", False))
    except Exception:
        return False


def _lock(pid):
    with _LOCKS_GUARD:
        if pid not in _LOCKS:
            _LOCKS[pid] = threading.Lock()
        return _LOCKS[pid]


def _cursor_path(base, instance):
    return os.path.join(base, "wakes", instance, "send_capture.json")


def _load_cursor(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("captured"), dict):
            return data
    except Exception:
        pass
    return {"schema_version": 1, "captured": {}}


def _save_cursor(path, cursor):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cap = cursor.get("captured") or {}
        if len(cap) > 200:
            keep = sorted(cap.items(), key=lambda kv: str(kv[1]))[-200:]
            cursor["captured"] = dict(keep)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cursor, f)
        os.replace(tmp, path)
    except Exception:
        logger.warning("[SendCapture] cursor save failed (fail-open)",
                       exc_info=True)


def _chronicle_has(base_root, instance, pid, ts, text):
    """Defense-in-depth idempotency: the deterministic uid already in the
    person's chronicle day files?"""
    try:
        import chronicle as _ch
        uid = ("send-capture-" + str(ts)[:19] + "-"
               + hashlib.sha1(str(text).encode()).hexdigest()[:8])
        pdir = os.path.join(str(base_root), instance, str(pid))
        if not os.path.isdir(pdir):
            return False
        today = str(ts)[:10]
        yesterday = (datetime.datetime.now()
                     - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        for day in (today, yesterday):
            p = os.path.join(pdir, f"{day}.jsonl")
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if uid in line:
                        return True
    except Exception:
        return False
    return False


def _append_history(config_name, pid, entry, base, user_histories,
                    load_fn, save_fn):
    """Append to the person's chat-thread history: the in-memory list (the
    SAME object the chat path saves) + the disk file."""
    hist = None
    if user_histories is not None:
        key = f"{config_name}::{pid}"
        if key in user_histories:
            hist = user_histories[key]
        elif load_fn is not None:
            try:
                hist = load_fn(config_name, pid) or []
            except Exception:
                hist = []
            user_histories[key] = hist
    if hist is None:
        # disk-only mode (no bridge structures — tests, or bare callers)
        safe = config_name.replace(".", "_").replace(":", "_")
        hdir = os.path.join(base, "histories", safe)
        hpath = os.path.join(hdir, f"{pid}.json")
        try:
            with open(hpath, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            hist = []
        if any(e.get("role") == entry["role"]
               and e.get("content") == entry["content"]
               and str(e.get("ts") or "") == str(entry.get("ts") or "")
               for e in hist if isinstance(e, dict)):
            return False
        hist.append(entry)
        try:
            os.makedirs(hdir, exist_ok=True)
            tmp = hpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False, indent=2)
            os.replace(tmp, hpath)
        except Exception:
            logger.warning("[SendCapture] history disk append failed "
                           "(fail-open)", exc_info=True)
            return False
        return True
    # in-memory path: dedup then append + save through the bridge's fn
    if any(e.get("role") == entry["role"]
           and e.get("content") == entry["content"]
           and str(e.get("ts") or "") == str(entry.get("ts") or "")
           for e in hist if isinstance(e, dict)):
        return False
    hist.append(entry)
    if save_fn is not None:
        try:
            save_fn(config_name, pid, hist)
        except Exception:
            logger.warning("[SendCapture] history save failed (fail-open)",
                           exc_info=True)
            return False
    return True


def reconcile(instance, config_name, from_wake, base=None,
              user_histories=None, load_fn=None, save_fn=None,
              chronicle_root=None, days=2):
    """Process uncaptured delivered non-letter outbox rows for the last
    `days` days. Returns the number captured. Never raises."""
    base = base or os.path.dirname(os.path.abspath(__file__))
    if os.getenv("CONTINUA_SEND_CAPTURE", "1") == "0":
        return 0  # the kill switch is honored at every layer
    instance = str(instance)
    ob_dir = os.path.join(base, "outbound", instance)
    if not os.path.isdir(ob_dir):
        return 0
    cpath = _cursor_path(base, instance)
    cursor = _load_cursor(cpath)
    count = 0
    now = datetime.datetime.now()
    for i in range(max(1, int(days))):
        day = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            with open(os.path.join(ob_dir, f"{day}.jsonl"),
                      encoding="utf-8") as f:
                raw = f.readlines()
        except OSError:
            continue
        for line in raw:
            try:
                row = json.loads(line)
            except Exception:
                continue
            pid = str(row.get("person_id") or "")
            text = str(row.get("text") or "")
            ts = str(row.get("ts") or "")
            if not pid or not text or not ts:
                continue
            if not row.get("delivered"):
                continue  # queued letters aren't "words said" yet
            if str(row.get("kind") or "human") == "letter":
                continue  # letters ride send.py §6c capture (already)
            key = ts + "|" + hashlib.sha1(text.encode()).hexdigest()[:12]
            if key in cursor["captured"]:
                continue
            with _lock(pid):
                if _chronicle_has(chronicle_root or
                                  os.path.join(base, "chronicle"),
                                  instance, pid, ts, text):
                    cursor["captured"][key] = ts
                    continue
                # 1. chronicle: her words enter her record
                try:
                    import chronicle as _ch
                    _ch.append({"ts": ts, "instance": instance,
                                "person_id": pid, "role": "assistant",
                                "content": text,
                                "uid": ("send-capture-" + ts[:19] + "-"
                                        + hashlib.sha1(
                                            text.encode()).hexdigest()[:8]),
                                "source": "send_message"},
                               root=(chronicle_root
                                     or os.path.join(base, "chronicle")))
                except Exception:
                    logger.warning("[SendCapture] chronicle capture failed "
                                   "(fail-open)", exc_info=True)
                # 2. the person's chat thread learns her words
                prefix = (f"[sent from a wake at {ts[11:16]}] "
                          if from_wake else "")
                try:
                    _append_history(config_name, pid,
                                    {"role": "assistant",
                                     "content": prefix + text, "ts": ts},
                                    base, user_histories, load_fn, save_fn)
                except Exception:
                    logger.warning("[SendCapture] history append failed "
                                   "(fail-open)", exc_info=True)
            cursor["captured"][key] = ts
            count += 1
    if count:
        _save_cursor(cpath, cursor)
    return count