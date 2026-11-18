"""turns.py — memorybuild.md Phase 1 (W1/W2): the merged timeline for one
person. ONE definition of "whose turn is it" shared by the wake packet's
turn-state block and the people-book clause (last_touch).

Verified fact (the 09-28 20-Questions incident): a delivered HUMAN send
from a wake exists ONLY in the outbox (outbound/<instance>/<date>.jsonl) —
the chronicle gets no row for it. Persona letters DO get send.py §6c
chronicle capture, so the same words can exist in BOTH files; dedup by
equal (role, text) keeps them one event. Wake-class dirs are never people.

Fail-open everywhere: any unreadable file means fewer events, never a
crash. Pure read surface — nothing here writes.
"""
import json
import os
import re
from datetime import datetime, timedelta

# threads that are never people (verified wake.py / core.py 2026-09-29)
WAKE_CLASS = {"system-wake", "continua:ritual", "system-sitting"}

_SENT_SPLIT = re.compile(r"(?<=[.!?…。！？])\s+")


def _outbox_dir(root, instance):
    """The outbound dir for a chronicle root: the chronicle root's PARENT
    is the project base (mirror.root = <base>/chronicle)."""
    return os.path.join(os.path.dirname(os.path.abspath(str(root))),
                        "outbound", instance)


def last_events(root, instance, person_id, days=2):
    """[{ts, role, text, source}] sorted by ts — the person's merged
    timeline: chronicle rows (chronicle/<instance>/<person>/<day>.jsonl for
    the last `days` local days) PLUS her delivered outbox rows for that
    person (outbound/<instance>/<day>.jsonl). Persona letters captured in
    both are de-duplicated by equal (role, text) — the chronicle row wins
    as the source label. Muted arrivals are skipped. [] on any failure."""
    instance = str(instance)
    person_id = str(person_id)
    out = []
    base = os.path.join(str(root), instance, person_id)
    ob_dir = _outbox_dir(root, instance)
    now = datetime.now()
    for i in range(max(1, int(days))):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            with open(os.path.join(base, f"{day}.jsonl"),
                      encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("arrived_muted"):
                        continue
                    out.append({"ts": str(r.get("ts") or ""),
                                "role": r.get("role"),
                                "text": str(r.get("content") or ""),
                                "source": "chronicle"})
        except OSError:
            pass
        try:
            with open(os.path.join(ob_dir, f"{day}.jsonl"),
                      encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if str(r.get("person_id") or "") != person_id:
                        continue
                    if not (r.get("delivered") or r.get("queued")):
                        continue  # failed wire attempts are not events
                    if str(r.get("kind") or "human") == "letter":
                        continue  # letters ride send.py §6c capture instead
                    out.append({"ts": str(r.get("ts") or ""),
                                "role": "assistant",
                                "text": str(r.get("text") or ""),
                                "source": "outbox"})
        except OSError:
            pass
    # dedup equal (role, text) — a persona letter captured twice (send.py
    # §6c + the outbox) is ONE event; prefer the chronicle row's ts.
    seen = {}
    for e in out:
        k = (e.get("role"), e.get("text"))
        if k not in seen or (e.get("source") == "chronicle"
                             and seen[k].get("source") != "chronicle"):
            seen[k] = e
    return sorted(seen.values(), key=lambda e: e.get("ts") or "")


def last_sentence(text, cap=200):
    """Her last sentence, for the turn-state and people-book lines. Never a
    mid-word clip: an over-long sentence is cut back to a word boundary
    with a leading '…'. Empty-safe."""
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    parts = [p.strip() for p in _SENT_SPLIT.split(t) if p.strip()]
    tail = parts[-1] if parts else t
    if len(tail) <= cap:
        return tail
    cut = t[-cap:]
    sp = cut.find(" ")
    if sp > 0:
        cut = cut[sp + 1:]
    return "…" + cut


def waiting_people(root, instance, person_ids, muted=None, busy=None):
    """People whose latest event (merged timeline) is HER OWN last word —
    nothing new has come from them. Facts for the wake packet's turn-state
    block (memorybuild W1a): the packet says whose turn it is; it never
    says what to do (the R18 ruling — no send guard, understanding not
    limits). Muted channels never appear (they are paused, not waiting).
    A person with a chat turn RUNNING right now is flagged in_flight —
    the packet says so instead of calling them quiet.

    Returns [{person_id, in_flight, at, sentence}] (sentence only when
    not in_flight)."""
    out = []
    muted = {str(m) for m in (muted or set())}
    busy = {str(b) for b in (busy or set())}
    for pid in person_ids:
        pid = str(pid)
        if pid in muted or pid in WAKE_CLASS:
            continue
        try:
            events = last_events(root, instance, pid)
        except Exception:
            continue
        if not events:
            continue
        last = events[-1]
        if last.get("role") != "assistant":
            continue  # their turn → the UNREAD block's job (unchanged)
        if pid in busy:
            out.append({"person_id": pid, "in_flight": True,
                        "at": str(last.get("ts") or "")[11:16],
                        "sentence": ""})
            continue
        out.append({"person_id": pid, "in_flight": False,
                    "at": str(last.get("ts") or "")[11:16],
                    "sentence": last_sentence(last.get("text") or "")})
    return out