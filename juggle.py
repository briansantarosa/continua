"""Work package B (memory plan §4a "Juggling several conversations at once",
§6e-B, §6d.5): the juggle — all recent conversations present VERBATIM in the
prompt, the active one last, nearest the generation point.

Rules from the plan (verbatim obligations):
  * Non-active blocks first, ordered by activity, OLDEST first, under
    "other conversations in my head", each with participant name and
    timestamp range. The active thread renders last and complete (it is the
    chat history itself — this module never touches it).
  * Hard thread boundaries: never blend two conversations into one transcript.
  * Keep earlier blocks stable between turns (deterministic rendering).
  * Include the persistent wake thread as its own labeled block.
  * Juggle window 24h, matching the recent band (§6d.5). Threads idle beyond
    it leave the juggle and live in standing memory.
  * Cap juggled threads at ~5; on overflow drop WHOLE OLDEST threads — never
    truncate every thread into fragments.
  * Per-thread window 10,000 chars; prefer dropping whole old turns over
    truncating mid-turn; never split a tool call from its result.
  * Deduplicate against standing memory: the caller passes the rendered
    windows to select_view, which suppresses recollections whose sources sit
    entirely inside a window (the verbatim words beat the summary).
  * Omit an unavailable thread silently and log it.

Known risk (plan §4a): with everyone's words present she may answer the wrong
person. Mitigation here: clear per-block framing plus an explicit closing
instruction to respond only in the active conversation; the behavioural check
is live, not a fixture (the plan says flag it, do not fake it).

Fail-open: any error yields no blocks; the rest of the assembly proceeds.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from recollections import now

WAKE_LABEL = "My wake reflections"
RITUAL_LABEL = "My ritual"
_RESERVE_LABEL = "my reserved reflections"

# threads that are not conversations: purge archives, the retired ritual
_SKIP_RE = re.compile(r"\.purged-|\.summary\.json$|\.tmp$")
_SKIP_UIDS = {"continua:ritual"}


def _as_dt(at):
    """recollections.now() returns an ISO string; arithmetic needs a datetime.
    Tolerant parser: history ts are NAIVE local wall-clock (the bridge writes
    them without offset), so no strict tz requirement here."""
    if isinstance(at, str):
        return datetime.fromisoformat(at)
    return at


def _parse(ts, at):
    """Parse a history ts; naive values take `at`'s offset (the bridge writes
    local wall-clock; `at` is offset-aware local)."""
    dt = datetime.fromisoformat(ts)
    at = _as_dt(at)
    if dt.tzinfo is None and at is not None:
        dt = dt.replace(tzinfo=at.tzinfo)
    return dt


def _fmt_hm(ts):
    return ts[11:16] if isinstance(ts, str) and len(ts) >= 16 else "?"


def _fmt_day(ts):
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else "?"


def eligible_threads(history_dir, active_user, at=None, window_hours=24.0):
    """Conversation threads with activity inside the juggle window, oldest
    activity FIRST (so the most recently active other thread sits nearest the
    active block). Returns [{'uid', 'path', 'last_ts', 'msgs'}]."""
    at = _as_dt(at or now())
    cutoff = at - timedelta(hours=float(window_hours))
    root = Path(history_dir)
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.json")):
        name = path.name
        if _SKIP_RE.search(name):
            continue
        uid = name[:-len(".json")] if name.endswith(".json") else name
        if uid == str(active_user) or uid in _SKIP_UIDS:
            continue
        try:
            msgs = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue  # omit an unavailable thread silently; caller logs
        if not isinstance(msgs, list) or not msgs:
            continue
        last = None
        for m in reversed(msgs):
            ts = m.get("ts")
            if isinstance(ts, str) and ts:
                last = ts
                break
        if not last:
            continue
        try:
            last_dt = _parse(last, at)
        except Exception:
            continue
        if last_dt < cutoff:
            continue  # idle beyond the window: standing memory holds it
        found.append({"uid": uid, "path": path, "last_ts": last,
                      "last_dt": last_dt, "msgs": msgs})
    found.sort(key=lambda t: t["last_dt"])  # oldest activity first
    return found


def tail_window(msgs, max_bytes):
    """Whole-message tail within max_bytes; never splits a tool result from
    its call context: if the first included row is a tool result, extend one
    row earlier. A single row larger than the cap renders alone (the
    single-row oversize exception, mirroring the episode planner)."""
    out, used = [], 0
    i = len(msgs) - 1
    start = len(msgs)
    while i >= 0:
        row = msgs[i]
        size = len(str(row.get("content") or "").encode("utf-8")) + 64
        if used + size > max_bytes and out:
            break
        start = i
        used += size
        out.insert(0, row)
        i -= 1
    # pair-safety: a tail that begins on a tool result drags its call in
    while start > 0 and msgs[start].get("role") == "tool":
        start -= 1
    return msgs[start:]


def _speaker(row, names):
    role = row.get("role")
    if role == "user":
        return "?"  # replaced by the caller with the roster name
    if role == "assistant":
        return "Me"
    if role == "tool":
        return f"[{row.get('name') or 'tool'} result]"
    return role or "?"


def render_thread(uid, msgs, names=None):
    """Labeled block for one thread: header with participant + timestamp
    range, then verbatim rows. Returns (text, t0, t1, bytes).

    The wake thread's user rows are the MACHINERY's wake packet (injected
    layers, identity priming) — not anyone's words; they render as one-line
    markers so the layers are not duplicated into the juggle. Her replies
    render verbatim. Rows without ts are static priming and are skipped.
    Prose is never clipped."""
    names = names or {}
    is_wake = uid == "system-wake"
    header_name = WAKE_LABEL if is_wake else (names.get(uid) or f"person-{uid}")
    lines = []
    t0 = t1 = None
    for row in msgs:
        ts = row.get("ts") or ""
        role = row.get("role")
        content = str(row.get("content") or "").strip()
        if not content or not ts:
            continue  # static priming rows carry no time and no voice
        if is_wake and role == "user":
            lines.append(f"[{_fmt_hm(ts)}] [wake packet — machinery voice]")
            t0 = t0 or ts
            t1 = ts
            continue
        speaker = ("Me" if role == "assistant"
                   else f"[{row.get('name') or 'tool'} result]" if role == "tool"
                   else names.get(uid) or f"person-{uid}")
        # rows already carrying a rendered [YYYY-MM-DD HH:MM] prefix (the
        # history stores stamped content) are not double-stamped
        _body = content if content.startswith("[2") else f"[{_fmt_hm(ts)}] {speaker}: {content}"
        lines.append(_body)
        if t0 is None:
            t0 = ts
        t1 = ts
    if not lines:
        return "", None, None, 0
    day0, day1 = _fmt_day(t0), _fmt_day(t1)
    span = (f"{day0} { _fmt_hm(t0)}" if day0 == day1
            else f"{day0} {_fmt_hm(t0)} – {day1} {_fmt_hm(t1)}")
    text = (f"With {header_name} — {span}\n" + "\n".join(lines))
    return text, t0, t1, len(text.encode("utf-8"))


def assemble(history_dir, active_user, at=None, window_hours=24.0,
             max_threads=5, thread_bytes=10000, names=None, budget_bytes=None):
    """Build the juggle. Returns:
    {'text', 'bytes', 'blocks': [{'uid','t0','t1','bytes'}],
     'dropped': [uid...], 'windows': [(uid, t0, t1), ...]}
    `windows` feeds select_view's dedup: recollections whose sources sit
    entirely inside a rendered window are suppressed (verbatim beats summary).
    """
    at = _as_dt(at or now())
    names = names or {}
    result = {"text": "", "bytes": 0, "blocks": [], "dropped": [],
              "windows": []}
    try:
        threads = eligible_threads(history_dir, active_user, at, window_hours)
    except Exception:
        return result
    # §6d.5: cap at ~5 threads; beyond that the OLDEST move to memory whole
    if len(threads) > max(0, int(max_threads)):
        result["dropped"] = [t["uid"] for t in threads[:len(threads) - int(max_threads)]]
        threads = threads[-int(max_threads):] if int(max_threads) else []
    blocks = []
    for t in threads:
        window = tail_window(t["msgs"], int(thread_bytes))
        text, t0, t1, nbytes = render_thread(t["uid"], window, names)
        if not text:
            continue
        blocks.append({"uid": t["uid"], "text": text, "t0": t0, "t1": t1,
                       "bytes": nbytes})
    # room-aware overflow: drop whole oldest threads until the total fits
    if budget_bytes is not None:
        while blocks and sum(b["bytes"] for b in blocks) > int(budget_bytes):
            dropped = blocks.pop(0)  # blocks are oldest-activity first
            result["dropped"].append(dropped["uid"])
    if not blocks:
        return result
    parts = ["[Other conversations in my head]", ""]
    for b in blocks:
        parts.append(b["text"])
        parts.append("")
        if b["t0"] and b["t1"]:
            result["windows"].append((b["uid"], b["t0"], b["t1"]))
        result["blocks"].append({"uid": b["uid"], "t0": b["t0"],
                                 "t1": b["t1"], "bytes": b["bytes"]})
    parts.append("(These are memories of other conversations, kept verbatim so "
                 "you remain one continuous person across them. They are not "
                 "the conversation to answer — respond only in the active "
                 "conversation below, and attribute remembered words to the "
                 "person and time shown.)")
    text = "\n".join(parts).rstrip() + "\n"
    result["text"] = text
    result["bytes"] = len(text.encode("utf-8"))
    return result
