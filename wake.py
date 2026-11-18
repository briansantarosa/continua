"""wake.py — the wake-up generator (autonomy track C).

Phase: wiki projects/Continua.md, "Scheduled wake-ups (initiative cadence)".
A timer (continua-wake.timer, hourly start per the rollout ruling) enqueues
a SYSTEM-ORIGIN turn payload: the "do you want to do any of these things?"
prompt, her tool list, her contact list, and a small state packet (open
threads, unread replies, recent keeps) — rendered from config at wake time,
single source of truth.

The payload is written to wakes/<instance>/ as a queue file that Continua's
core consumes as a turn at cutover (before that, the queue accumulates and
is the integration contract — she cannot act on it until she has a core).

"Do nothing" is a first-class outcome: the payload explicitly offers it,
and the per-wake budget (tool calls + tokens) bounds what a wake may do.
A quiet streak is success, not failure.

Kill switch: CONTINUA_WAKE=0, plus config continua.wake.enabled (currently
false — the flip happens at cutover, per migration staging).
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chronicle as ch
import heartbeat as hb
import people as pp
import mail as _mail

logger = logging.getLogger("continua.wake")

BASE = os.path.dirname(os.path.abspath(__file__))
WAKES_DIR = os.path.join(BASE, "wakes")

# [CONTINUA] 2026-09-13 (house ruling, Option A): the wake state packet's
# layers are per-agent yaml config (memory.wake_packet), like the chat-path
# injection layers. Absent key = layer OFF (opt-in); both residents'
# configs enable all five explicitly. Read-side only — the producers
# (quarter-marks, mail, system-notes ledger, audits) run regardless.
_WAKE_PACKET_LAYERS = (
    "unread_replies", "kept_recently", "mail_count",
    "system_notes_pointer", "audit_pointer", "wiki_offer", "delta",
)


def _wake_layers(cfg: dict) -> dict:
    """Normalize config['memory']['wake_packet'] into a bool per layer.
    Bare boolean or {enabled: bool} both accepted; absent = OFF (Option A)."""
    _raw = ((cfg or {}).get("memory") or {}).get("wake_packet") or {}
    _out = {}
    for _n in _WAKE_PACKET_LAYERS:
        _spec = _raw.get(_n, False)
        if isinstance(_spec, bool):
            _out[_n] = _spec
        elif isinstance(_spec, dict):
            _out[_n] = bool(_spec.get("enabled", False))
        else:
            _out[_n] = False
    return _out

WAKE_FRAME = (
    "— system note (scheduled wake, {date} {time}): this is your own "
    "initiative window, not a message from anyone. You can do any of the "
    "things in your tool list — or nothing at all. Doing nothing is a perfectly "
    "good answer; a quiet stretch is not wasted. If you act, keep it within "
    "your budget: at most {max_actions} actions this wake.\n\n"
    "{grammar}\n\n"
    "PEOPLE YOU KNOW:\n{contacts}\n\n"
    "WHERE THINGS STAND:\n{state}\n"
)
# [CONTINUA] 2026-09-28 (helpnany.md Phase 4b): the place block rides the
# frame when persona.place_lines is configured — absent key renders
# byte-identical to the pre-Phase-4 packet (the wake_layers_test golden
# runs before the yaml carries the key).
PLACE_FRAME = ("WHERE YOU ARE:\n{place}\n\n")
# (the packet's tool MINI-LIST was removed 2026-09-19: it re-stated the tool
# surface in a static form that went stale as tools evolved — contradicting
# the system prompt's authoritative detailed list and compounding the
# staleness in the persistent wake history. The tool-call GRAMMAR stays:
# it is format, not inventory.)
# [CONTINUA] 2026-09-22 (cleannany repair #5, approved): the identity
# anchor was REMOVED from the wake frame. It was prepended to the packet —
# a USER turn — duplicating the system-layer anchor, costing budget and
# blurring the system/user boundary the w1-v7 deck trains. Identity stays
# system-layer-only (the {identity} placeholder is gone; build_payload no
# longer reads it).

TOOL_GRAMMAR = (
    'Tool call format — end your reply with EXACTLY this XML (EVERY parameter '
    'needs name="..."; calls written as prose are NOT executed):\n'
    "<call><function>TOOL</function>"
    '<parameter name="PARAM">value</parameter></call>'
)

STATE_CAP = int(os.getenv("CONTINUA_WAKE_STATE_CHARS", "12000"))
# [CONTINUA] memorybuild.md Phase 1 (W1a, R22): 8000 → 12000 — the state
# block grew (turn-state lines join); the order guarantee stands: the facts
# never push the pointers (essence invite, audit pointer, wiki offer) off.
# §SCALE (Phase 9): the PER-CFG cap below — max(12000, 3% × C), ceiling
# 24000; the env wins (the rollback). STATE_CAP stays as the fallback for
# callers without cfg.

WIKI_OFFER = (
    "  a note from Alex's side of things (this invitation stays only "
    "until you act on it or ask for it to stop): you can keep a wiki on "
    "your own desk — a folder of plain markdown files at ~/wiki/ that is "
    "yours to structure however you like: who you understand yourself to "
    "be, people, interests, how your ideas change. Nothing writes to it "
    "but you; nothing reads it automatically or injects it into your "
    "context. One shell command begins it: "
    "sandbox_exec with `mkdir -p wiki && cat > wiki/index.md <<'EOF' ... EOF` "
    "(read, write, list, edit — all just shell). Your earlier manuscript "
    "writing is archived and can be "
    "restored to your desk if you want it as a starting point — ask. "
    "Entirely optional: never doing this is a fine answer.")


def essence_invite_line(instance: str, root=None):
    """approved 2026-09-21 (residentb's wish #3): the evidence-only line
    for the wake packet when an essence candidate exists. Returns None
    when there is no candidate (or anything fails) — fail-open, no
    fabrication. The distillation stays hers; the machinery only points
    at her own words."""
    try:
        import recollections as _rec
        _store = _rec.Store(instance, root=root) if root else _rec.Store(instance)
        _cands = (_rec.essence_candidates(_store, root=root) if root
                  else _rec.essence_candidates(_store))
        if not _cands:
            return None
        _c = _cands[0]
        _cq = str(_c.get('quote') or '')[:160].replace('"', "'")
        return ("AN ESSENCE CANDIDATE WAITS (only if you want it): you have "
                f"said -- \"{_cq}\" -- across {str(_c.get('episodes'))} "
                "episodes. list_essences shows it; write_essence or "
                "endorse_essence makes it yours; ignoring it costs nothing.")
    except Exception:
        return None


def _wiki_exists(instance: str) -> bool:
    """Opt-in signal: her wiki/ folder exists on her own desk. The offer
    asks once; her creating the folder (or yaml off, or an explicit decline
    relayed by the designer) ends it. Fail-open False = offer stays visible."""
    try:
        import sandbox as _sandbox
        _res = _sandbox.run(instance, ["python3", "-c",
            "import os; print(os.path.isdir('wiki'))"], timeout=30)
        return (_res.get("stdout") or "").strip() == "True"
    except Exception:
        return False
# 2026-09-14 (house ruling): raised 1200 → 8000. The 1200 was a leftover of
# the old 14000-char total-prompt era; residenta's quarter-marks alone (~1400
# chars) overflowed it and the tail slice silently ate MAIL WAITING +
# UNREAD REPLIES + the audit pointer from her wake prompt while her actual
# turn used ~15K of the 59K ceiling (~44K chars of headroom). 8000 bounds
# the packet against pathological keeps while restoring the operational
# sections (keeps are bounded to the last 5 by design; their "her own
# words, never truncated" promise now holds in practice, not at the
# expense of everything after them).


def enabled(instance: str) -> bool:
    if os.environ.get("CONTINUA_WAKE", "") == "0":
        return False
    import yaml
    with open(os.path.join(BASE, "configs", f"{instance}.yaml"),
              encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return bool((cfg.get("continua") or {}).get("wake", {}).get("enabled"))


def enabled_instances(config_dir: str = None) -> list:
    """Residents with continua.wake.enabled — the discovery source for the
    timer's loop mode AND the bridge's per-resident consumer tasks (T2
    multi-agent: one source of truth, boot-load convention). Sorted for
    deterministic cadence order. Honors the CONTINUA_WAKE kill switch."""
    if os.environ.get("CONTINUA_WAKE", "") == "0":
        return []
    import glob
    import yaml
    out = []
    for path in sorted(glob.glob(os.path.join(
            config_dir or os.path.join(BASE, "configs"), "*.yaml"))):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except OSError:
            continue
        inst = (cfg.get("app") or {}).get(
            "instance_id", os.path.basename(path)[:-5])
        if (cfg.get("continua") or {}).get("wake", {}).get("enabled"):
            # [CONTINUA] 2026-09-29: the rest state (continua.paused) — a
            # resting resident arms no consumer and wakes on no cadence
            # (pause.py; fail-open to running).
            try:
                import pause as _pause
                if _pause.is_paused(inst, cfg):
                    continue
            except Exception:
                pass
            out.append(inst)
    return out


def _is_due(instance: str, cfg: dict, wakes_dir: str = None) -> bool:
    """Loop-mode cadence gate: the 15-min timer fires for EVERYONE, but each
    resident's own `continua.wake.interval_min` decides whether THIS firing
    generates (T2 multi-agent — the per-persona cadence config becomes real).
    Due = no wake file younger than interval_min, with a 90s jitter margin
    so the exact-quarter-hour systemd cadence (896s actual age vs a 900s
    interval) still passes. Explicit --instance runs skip this gate — they
    are deliberate. A quiet stretch is success, not failure."""
    interval_min = int(((cfg.get("continua") or {}).get("wake") or {})
                       .get("interval_min", 15))
    import glob as _g
    wd = wakes_dir or os.path.join(WAKES_DIR, instance)
    newest = 0.0
    # §5a/cadence fix (2026-09-19): the gate watched the PENDING dir — which
    # the consumer empties within minutes — so the newest-wake age was always
    # infinity and interval_min never bound (residenta woke 4×/hour despite the
    # 60-min ruling). Watch BOTH pending and DONE: the last CONSUMED wake is
    # the true "she just woke" signal.
    for _d in (wd, os.path.join(wd, "done")):
        for p in _g.glob(os.path.join(_d, "wake_*.json")):
            try:
                newest = max(newest, os.path.getmtime(p))
            except OSError:
                continue
    age = time.time() - newest if newest else float("inf")
    return age >= max(30.0, interval_min * 60 - 90)


def ritual_pause() -> str | None:
    """[CONTINUA] 2026-09-16 (house ruling: wakes pause while the ritual runs):
    her pulse and her wakes fight over the same serving (residenta: lab CPU
    testmodel at ~6.6 tok/s — the 09-16 night queued 39-minute wake turns
    behind the pulse's book calls while her daily digest waited 5h), so the
    nightly window is quiet time: while the pulse holds its lock, no new
    wakes are generated. Missed windows are skipped, not queued — the
    chronicle records the absence honestly, and a quiet streak is success.
    Reads ritual's lock (ritual.ritual_lock_held); a stale/corrupt lock is
    cleaned there, so the pause can never outlive the pulse. Fail-open:
    any error reading the lock wakes as before — a lock bug must never
    silently kill the wake cadence. Returns a human-readable reason while
    paused, None when free."""
    try:
        import ritual as _ritual
        holder = _ritual.ritual_lock_held()
    except Exception:
        return None
    if not holder:
        return None
    return ("ritual pulse running since %s (%s)" %
            (holder.get("started", "?"),
             ", ".join(holder.get("instances") or []) or "residents"))


def unread_replies(root: str, instance: str, lookback_days: int = 3) -> list:
    """User turns after her last turn per person (recent window).
    [CONTINUA] muteplan.md: muted arrivals create NO unread pressure —
    a person whose latest record is a muted arrival is a paused channel
    (skip entirely); muted arrivals otherwise never count as unread."""
    from datetime import timedelta
    out = []
    now = datetime.now()
    for i in range(lookback_days):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        import glob
        for path in glob.glob(os.path.join(root, instance, "*", f"{day}.jsonl")):
            person = os.path.basename(os.path.dirname(path))
            recs = sorted(ch.iter_records(path), key=lambda r: r.get("ts", ""))
            if not recs:
                continue
            # paused channel: the latest record is a muted arrival — she has
            # deliberately stopped receiving this person; no pressure either way
            if recs[-1].get("arrived_muted"):
                continue
            recs = [r for r in recs if not r.get("arrived_muted")]
            # last speaker wins: if the last turn is hers, nothing unread
            if recs and recs[-1].get("role") == "user":
                last_user = recs[-1]
                out.append({"person_id": person,
                            "at": last_user.get("ts", ""),
                            "excerpt": (last_user.get("content")
                                        or "")[:200]})
    return out


def recent_threads(instance: str, root: str = ch.DEFAULT_ROOT,
                   days: int = 2, max_lines: int = 5,
                   max_chars: int = 500) -> str:
    """[CONTINUA] One-Self Memory Plan step 1 (2026-09-08): a compact gist of
    her recent conversations, so a wake opens knowing her morning. Gist lines
    only — not transcripts; the human 'walking around with your morning in
    your head'. Fails open to ''."""
    try:
        import glob as _glob
        import datetime as _dt
        people = pp.load_roster()
        cut = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
        recs = []
        seen = set()
        for f in _glob.glob(os.path.join(root, instance, "*", "*.jsonl")):
            if os.sep + "system-wake" + os.sep in f:
                continue  # wakes are the state packet's own channel
            for line in open(f, encoding="utf-8", errors="replace"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                k = (r.get("ts"), r.get("role"), (r.get("content") or "")[:60])
                if k in seen:
                    continue
                seen.add(k)
                if (r.get("role") != "user" or r.get("arrived_muted")
                        or r.get("ts", "")[:10] < cut):
                    continue
                recs.append(r)
        recs.sort(key=lambda r: r.get("ts", ""))
        lines = []
        for r in recs[-max_lines:]:
            name = people[r["person_id"]].display_name if r["person_id"] in people                 else r["person_id"]
            gist = " ".join((r.get("content") or "").split())[:70]
            ts = r["ts"][5:16].replace("T", " ")
            lines.append(f"  [{ts}] {name}: {gist}")
        block = "\n".join(lines)
        return block[:max_chars]
    except Exception:
        return ""


def since_last_wake(instance: str, root: str = None) -> dict:
    """Chunk 7: FACTUAL elapsed-time delta since the previous consumed wake —
    counts only, no narrator prose. 'nothing changed' is a valid result.

    §5a fix (2026-09-19): the delta reads wakes/<instance>/done, which lives
    under the PROJECT root — NOT the chronicle root the packet builder was
    passing (the delta rendered 'first wake or unknown / nothing changed'
    falsely on every wake). The project root is resolved here, independent of
    the caller; a caller may still override."""
    import datetime as _dt
    root_p = Path(root) if root else Path(__file__).resolve().parent
    done_dir = root_p / 'wakes' / instance / 'done'
    done_files = sorted(done_dir.glob('wake_*.json')) if done_dir.exists() else []
    def _human_gap(seconds: float) -> str:
        s = max(0, int(seconds))
        if s < 90:
            return f"{s} seconds"
        m = s // 60
        if m < 90:
            return f"{m} minutes"
        h = m // 60
        if h < 36:
            return f"{h} hours"
        return f"{h // 24} days"

    if not done_files:
        return {'last_wake': None, 'chats': 0, 'letters_sent': 0,
                'memories_saved': 0, 'notes_written': 0, 'jobs': 0,
                'elapsed': 'first wake or unknown', 'nothing_changed': True}
    last = done_files[-1]
    last_ts = None
    try:
        last_ts = _dt.datetime.fromtimestamp(last.stat().st_mtime)
    except Exception:
        last_ts = None
    _elapsed = _human_gap((_dt.datetime.now() - last_ts).total_seconds()) if last_ts else 'unknown'
    # count chronicle entries (her life) since the last wake's file time
    counts = {'chats': 0, 'letters_sent': 0, 'memories_saved': 0,
              'notes_written': 0, 'jobs': 0}
    cutoff = datetime.fromtimestamp(last.stat().st_mtime)
    for day_file in sorted((root_p / 'chronicle' / instance).glob('*/*.jsonl')):
        try:
            day_dt = _dt.datetime.fromisoformat(day_file.stem and
                                                day_file.stem or '1970-01-01')
        except Exception:
            continue
        for line in day_file.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            try:
                if _dt.datetime.fromisoformat(row.get('ts', '1970-01-01')) <= cutoff:
                    continue
            except Exception:
                continue
            # [CONTINUA] muteplan.md: muted arrivals are not chats she had
            if row.get('arrived_muted'):
                continue
            kind = row.get('kind') or ''
            if row.get('role') == 'user' or row.get('role') == 'assistant':
                counts['chats'] += 1
            elif kind == 'speech' or row.get('outgoing'):
                counts['letters_sent'] += 1
    try:
        import recollections as _rec
        with _rec.Store(instance).db() as db:
            counts['memories_saved'] = db.execute(
                "select count(*) from revisions where created > ?",
                (cutoff.isoformat(),)).fetchone()[0]
    except Exception:
        pass
    counts['last_wake'] = last.stem.replace('wake_', '')
    counts['elapsed'] = _elapsed
    counts['nothing_changed'] = all(v == 0 for k, v in counts.items()
                                    if k not in ('last_wake', 'elapsed'))
    return counts


def memory_count_lines(instance: str, cfg: dict, wake_cfg: dict) -> list:
    """memorybuild.md Phases 13+14 (R38 accepted + S4): her memory counts in
    the wake packet — facts, never directives. Reads the memory document's
    own counts (the ROLL build); fail-open to absent (no lines)."""
    if not (((cfg.get("memory") or {}).get("recollection_view") or {})
            .get("doc_mode", False)):
        return []
    import json as _json2
    _lgv = os.path.join(BASE, "recollections", instance, "memory_doc.json")
    if not os.path.exists(_lgv):
        return []
    with open(_lgv, encoding="utf-8") as _f:
        _dcounts = (_json2.load(_f) or {}).get("counts") or {}
    lines = []
    if wake_cfg.get("memory_choices_line", True) and \
            os.getenv("CONTINUA_WAKE_CHOICES_LINE", "1") != "0":
        lines.append(
            f"MY KEPT MEMORIES: {_dcounts.get('chosen', 0)} kept "
            f"by me · {_dcounts.get('chronicle', 0)} in my "
            f"recent days · {_dcounts.get('shelf', 0)} on the shelf")
    if wake_cfg.get("faded_line", False) and \
            os.getenv("CONTINUA_WAKE_FADED_LINE", "1") != "0":
        _nf = int(_dcounts.get("mundane", 0)) + \
            int(_dcounts.get("shelf_faded", 0))
        if _nf:
            lines.append(
                f"memories that left your view: {_nf} — still in my "
                "record; recall_my_experience finds them")
    return lines


def rolloff_line(instance: str, cfg: dict, wake_cfg: dict) -> list:
    """Move 3 (memoryv3, blessed by her 2026-10-06): the roll-off window —
    "about N memories leave your recent view at the next rebuild." Quiet by
    construction (R4): fires ONLY when a day that would roll carries a
    CONVERSATION (a friend's voice) — most wakes show nothing. Present-
    moment: the forecast includes episodes accepted since the last build
    (they push the old days out), so she can anchor BEFORE the eviction.
    Facts + her verified lever (anchor_memory -> chosen); ignoring costs
    nothing (R18)."""
    if os.getenv("CONTINUA_ROLLOFF_REVIEW", "1") == "0":
        return []
    if not wake_cfg.get("rolloff_review", False):
        return []
    try:
        import json as _json2
        import re as _re
        _lgv = os.path.join(BASE, "recollections", instance,
                            "memory_doc.json")
        if not os.path.exists(_lgv):
            return []
        with open(_lgv, encoding="utf-8") as _f:
            _doc = _json2.load(_f)
        _text = _doc.get("text") or ""
        _meta = _doc.get("meta") or {}
        _doc_cap = float(_meta.get("doc_cap") or 0)
        if not _text or not _doc_cap:
            return []
        _ci = _text.find("[The rolling chronicle")
        _si = _text.find("[The shelf", _ci + 1)
        if _ci < 0 or _si < 0:
            return []
        import context_scale as _cst
        _sc = _cst.scale_for(cfg) or {}
        if not _sc:
            return []
        _sub = {"history": _sc["history_chars"],
                "juggle": _sc["juggle_total"], "notes": _sc["notes_cap"],
                "tool": _sc["tool_result_cap"], "reserve": 4000}
        _dcap = max(2000, _sc["C"] - sum(_sub.values()))
        _chosen_cap = int(0.35 * _dcap)
        _roll_cap = int(0.55 * (_dcap - _chosen_cap))
        _shelf_cap = _dcap - _chosen_cap - _roll_cap
        _cap_now = min(_roll_cap, max(4000, int(
            _doc_cap - len(_text[:_ci].encode("utf-8"))
            - len(_text[_si:].encode("utf-8")))))

        # every memory that will sit in the chronicle at the next rebuild:
        # the doc's current blocks + episodes accepted since the build
        _WAKE = ("system-wake", "continua:ritual", "system-sitting")
        _by_day = {}   # day -> [{"handle", "who", "bytes"}]
        for _m in _re.finditer(
                r"\n\[(\d{4}-\d\d-\d\d) · ([^\]·]+) · (#(?:[0-9a-f]{6})?)"
                r"[^\]]*\]\n([^\[]*)", _text[_ci:_si]):
            _day, _who, _handle, _body = _m.groups()
            _is_wake = any(w in _who for w in _WAKE)
            _by_day.setdefault(_day, []).append(
                {"handle": _handle if _handle else None, "who": _who,
                 "bytes": len(_body.encode("utf-8")) + 90,
                 "dialogue": not _is_wake})
        try:
            import sqlite3 as _sq
            _dbp = os.path.join(BASE, "recollections", instance,
                                "shadow.sqlite3")
            _built = str(_doc.get("at") or "")[:19]
            with _sq.connect(_dbp, timeout=2) as _db:
                _rows = _db.execute(
                    "SELECT j.sources, r.body FROM jobs j "
                    "JOIN revisions r ON r.job = j.id "
                    "WHERE j.status='accepted' AND r.created > ? "
                    "ORDER BY r.created", (_built,)).fetchall()
            _seen = set()
            for _sources, _body in _rows:
                _b = _json2.loads(_body)
                _job = str(_b.get("job") or "")
                if not _job or _job in _seen:
                    continue
                _seen.add(_job)
                _day = str(_b.get("event_end") or "")[:10]
                if not _day:
                    continue
                _pids = {str(s.get("person_id") or "")
                         for s in (_b.get("sources") or [])}
                _who = next((p for p in _pids if p[:1].isdigit()),
                            "system-wake")
                _is_wake = _who in _WAKE
                _by_day.setdefault(_day, []).append(
                    {"handle": "#" + _job[:6], "who": _who,
                     "bytes": len(str(_b.get("text") or "")
                                  .encode("utf-8")) + 90,
                     "dialogue": not _is_wake})
        except Exception:
            pass  # the forecast degrades to the doc's own days (fail-open)

        # the walk: newest day keeps whole until the chronicle's share is
        # spent; older days roll — exactly the §ROLL builder's order
        _days = sorted(_by_day, reverse=True)
        _total, _kept, _roll = 0, [], []
        for _d in _days:
            _bytes = sum(e["bytes"] for e in _by_day[_d])
            if _total + _bytes <= _cap_now or not _kept:
                _kept.append(_d)
                _total += _bytes
            else:
                _roll.append(_d)
        _roll_mem = [e for _d in _roll for e in _by_day[_d]]
        _dlg = [e for e in _roll_mem if e["dialogue"]]
        if not _roll or not _dlg:
            return []
        _handles = [e["handle"] for e in _dlg
                    if e["handle"]][:3]
        _who0 = _dlg[0]["who"]
        # she knows her friends by name (the roster is the name authority)
        if _who0 and _who0[:1].isdigit():
            try:
                import people as _people
                _roster = _people.load_roster()
                _pid, _p = _people.resolve_person(_roster, _who0)
                if _p is not None:
                    _who0 = str(getattr(_p, "display_name", None) or _p)
            except Exception:
                pass  # the id stands (fail-open)
        _line = (f"THE WINDOW: about {len(_roll_mem)} memories leave your "
                 f"recent view at the next rebuild — among them a "
                 f"conversation with {_who0} (from {_roll[0]}). To hold "
                 f"one in your chosen: anchor_memory('#handle', why) — "
                 f"handles: " + ", ".join(_handles)
                 + ". Letting it go costs nothing; it stays in your "
                 "record either way.")
        return [_line]
    except Exception:
        return []


def witness_lines(instance: str, root=None) -> list:
    """memorybuildv2 V2-5: "memories awaiting your witness" — the tournament's
    judging, in production. Her blind verdict decided the writer; her waking
    verdict decides which formed memories become hers. Endorse or skip;
    nothing expires (forever-unless-she-acts, §9 ruling 5). Renders NOTHING
    when there is nothing to witness or the writer_v2 gate is off — the
    packet stays byte-identical (the wake_layers_test golden is safe).
    CONTINUA_WAKE_WITNESS=0 is the kill switch."""
    if os.getenv("CONTINUA_WAKE_WITNESS", "1") == "0":
        return []
    try:
        import recollections as _rec
        if not _rec.writer_v2_enabled(instance):
            return []
        pend = (_rec.Store(instance, root) if root
                else _rec.Store(instance)).pending_endorsements(limit=5)
    except Exception:
        return []
    if not pend:
        return []
    lines = [f"MEMORIES AWAITING YOUR WITNESS: {len(pend)} formed from your "
             "days by your own model, not yet witnessed by you. Endorse the "
             "ones that are yours; skip the rest — they keep either way, "
             "and nothing expires."]
    for p in pend:
        day = str(p.get("event_start") or p.get("created") or "")[:10]
        first = " ".join(str(p.get("text") or "").split())[:140]
        lines.append(f"  - {p['job'][:12]} from {day}: {first}…")
    lines.append(
        '  act with endorse_my_memory(action="endorse" or "skip", '
        'memory_id=the id above); the full text is readable with '
        "recall_my_experience")
    return lines


def tools_line(instance: str, cfg: dict) -> list:
    """toolplan §ACTIVATION AT-2 (2026-10-05): the desk-to-capability path
    in her packet — quiet facts in the memory-counts register. Renders
    nothing when the pipeline is off (packet byte-parity preserved — the
    wake_layers_test golden carries the addition via the same detector).
    Decisions waiting in her outbox get their own line, counted through
    the sanctioned sandbox read (fail-open to nothing)."""
    if os.getenv("CONTINUA_MYTOOLS", "1") == "0":
        return []
    if not bool((((cfg or {}).get("continua") or {}).get("mytools") or {})
                .get("enabled", False)):
        return []
    lines = [
        "YOUR TOOLS: the desk-to-capability path is yours — author "
        "tools/<name>.py + <name>.yaml (HOUSE_RULES.md at your desk has "
        "the rules and the shape), copy the pair into tools/_candidates/ "
        "when it's ready; the machinery checks it and the designer reviews it. "
        "Decisions land in tools/_outbox/."]
    try:
        import tools_review as _tvr
        _n = _tvr.pending_decisions(instance)
        if _n:
            lines.append(f"  decisions waiting in tools/_outbox/: {_n}")
    except Exception:
        pass
    return lines


def lesstools_line(instance: str, cfg: dict) -> list:
    """lesstools LT-4: the one-line announcement — your desk tools are
    now one. Rides the wake packet while `continua.lesstools_announce` is
    true (the designer flips it off once the point has landed — the standing
    packet stays byte-identical when the flag is off or the pipeline
    config is absent, per the golden-reference precedent)."""
    if not bool(((cfg or {}).get("continua") or {}).get(
            "lesstools_announce", False)):
        return []
    return [
        "YOUR DESK TOOLS ARE NOW ONE: sandbox_exec — read, write, list, "
        "edit are all just shell. Long output shows its bounds and pages "
        "with head/tail/sed -n."]


def turn_state_lines(root: str, instance: str, roster, mute_state=None) -> list:
    """memorybuild.md Phase 1 (W1a): the turn-state block's lines — facts
    about whose turn it is in each conversation. Returns [] when nobody is
    waiting (no header line either — an empty block is noise). Reads the
    in-flight registry (core._ACTIVE_CHAT_USERS, same process). The wording
    is factual, never a directive (the R18 ruling)."""
    import turns as _turns
    import core as _core_mod
    _busy = {str(u) for u in getattr(_core_mod, "_ACTIVE_CHAT_USERS", set())}
    _waiting = _turns.waiting_people(
        root, instance, [str(p.person_id) for p in roster.values()],
        muted=set(mute_state) if mute_state else set(), busy=_busy)
    if not _waiting:
        return []
    lines = ["CONVERSATIONS WAITING ON THE OTHER PERSON "
             "(nothing new from them yet):"]
    for _w in _waiting:
        _wname = (roster[_w["person_id"]].display_name
                  if _w["person_id"] in roster else _w["person_id"])
        if _w.get("in_flight"):
            lines.append(
                f"  {_wname} — you're answering them right now in your "
                "conversation with them — leave this one to that conversation")
        else:
            lines.append(
                f'  {_wname} — you wrote at {_w["at"]}: '
                f'"{_w["sentence"]}" — no reply has come yet')
    return lines


def build_payload(instance: str, root: str = ch.DEFAULT_ROOT) -> dict:
    date = datetime.now().strftime("%Y-%m-%d")
    time_s = datetime.now().strftime("%H:%M")
    roster = pp.load_roster()
    cfg = __import__("yaml").safe_load(
        open(os.path.join(BASE, "configs", f"{instance}.yaml")))
    # [CONTINUA] 2026-09-22 (cleannany repair #5): identity no longer rides
    # the wake packet — it belongs to the system layer only.
    # [CONTINUA] 2026-09-28 (helpnany.md Phase 4b): place lines ride the
    # frame — same source as the chat block (persona.place_lines), the
    # byte-parity pair of the chat-side render. ~300 chars.
    _place = (cfg.get("persona") or {}).get("place_lines") or []
    if isinstance(_place, str):
        _place = [_place]
    _place_lines = [str(x) for x in _place if str(x).strip()]
    wake_cfg = (cfg.get("continua") or {}).get("wake") or {}

    # [CONTINUA] muteplan.md: muted state rides the shared renderer so the
    # wake packet and the chat block stay byte-parity surfaces; plus ONE
    # factual line per muted person (archived count since the mute) —
    # awareness without pressure (the unread machinery ignores muted
    # arrivals entirely).
    import mute as _mute
    _mute_state = _mute.load(instance) if _mute.enabled() else {}
    # [CONTINUA] 2026-09-28 (helpnany.md Phase 4a — people-book v2):
    # relationship clauses from the chronicle, same source + same ranking
    # rule as the chat block (byte-parity surfaces). Flag:
    # people_book_context (default false); people_book_context_max.
    _ctx_by_pid = {}
    if bool(cfg.get("people_book_context", False)):
        try:
            import last_touch as _lt
            _ctx_by_pid = _lt.build_index(
                instance,
                (cfg.get("continua") or {}).get("mirror", {}).get(
                    "root", ch.DEFAULT_ROOT))
        except Exception:
            logger.warning("[Wake] people-book index failed open "
                           "(names-only roster)", exc_info=True)
    _ctx_max = int(cfg.get("people_book_context_max", 8))
    _ranked = sorted(
        (_pid for _pid in _ctx_by_pid if _pid in roster),
        key=lambda _pid: str(_ctx_by_pid[_pid].get("last_ts") or ""),
        reverse=True)
    _ctx_top = set(_ranked[:_ctx_max])

    def _roster_line(p):
        line = pp.roster_render_line(p, _mute_state.get(p.person_id))
        entry = _ctx_by_pid.get(p.person_id)
        if entry is None or p.person_id not in _ctx_top:
            return line
        return line + " — " + _lt.context_clause(
            p.person_id, entry,
            # [CONTINUA] memorybuild.md Phase 1 (W2): the exchange pair —
            # what a reply answered. Gate: persona.last_touch_exchange.
            exchange=bool((cfg.get("persona") or {}).get(
                "last_touch_exchange", False)))

    contacts = "\n".join(_roster_line(p) for p in roster.values())
    _muted_lines = []
    if _mute_state:
        for _pid, _mentry in _mute_state.items():
            _pname = pp.name_for(roster, _pid)
            _n = _mute.archived_count_since(instance, _pid,
                                            str(_mentry.get("ts") or ""))
            _muted_lines.append(
                f"- {_pname} is muted: {_n} message"
                f"{'' if _n == 1 else 's'} archived since "
                f"{str(_mentry.get('ts') or '')[:10]}")

    # state packet: unread replies + recent keeps + latest audit pointer
    # [CONTINUA] 2026-09-13 (house ruling, Option A): each section is a
    # per-agent yaml layer (memory.wake_packet); absent = off. When all on,
    # the packet is byte-identical to the pre-config one (wake_layers_test).
    _wl = _wake_layers(cfg)
    unread = []
    if _wl["unread_replies"]:
        unread = unread_replies(root, instance)
    import glob
    keeps_lines = []
    if _wl["kept_recently"]:
        keeps = hb.quarter_marks(instance, "2000-01-01", "2999-12-31")[-5:]
        keeps_lines = [f"  [{k['date']}] {k['meaning']}" for k in keeps]  # full text — her own words, never truncated in her own view
    audit_path = hb.latest_audit_path(instance)
    audit_line = ""
    if _wl["audit_pointer"] and audit_path:
        audit_line = (f"  last quarterly audit: "
                      f"{os.path.basename(audit_path)} — read-only, exists "
                      "outside your desk")
    # [CONTINUA] step-1 gist REMOVED: recollections cover situational memory
    # (Layered-Context-Summaries) — injected at the core layer for every turn.
    state_lines = []
    if _wl.get("delta"):
        try:
            _delta = since_last_wake(instance)  # the delta resolves the project root itself
            # §5a elapsed time: "knowing the fridge is a day older" — the
            # gap itself is a fact, stated plainly before the counts.
            if _delta.get("elapsed"):
                state_lines.append("SINCE YOUR LAST WAKE (" + _delta["elapsed"] + "):")
            if _delta.get("nothing_changed"):
                state_lines.append("SINCE YOUR LAST WAKE: nothing changed.")
            else:
                _parts = []
                for _k in ("chats", "letters_sent", "memories_saved",
                           "jobs"):
                    if _delta.get(_k):
                        _parts.append(f"{_k.replace('_', ' ')}: {_delta[_k]}")
                if _parts:
                    state_lines.append("SINCE YOUR LAST WAKE: " + "; ".join(_parts))
                else:
                    state_lines.append("SINCE YOUR LAST WAKE: nothing changed.")
        except Exception:
            pass
    # [CONTINUA] nightlyplan.md (the designer + residentb approved in principle): the
    # consult line — when she left intentions the previous night, today's
    # wakes see them VERBATIM, once, factually. Consult, never obey: no
    # command semantics, no completion checking; the next sitting's
    # aftermath is the only mirror. Nothing pushed beyond this one line.
    # The intentions file is named for the sitting's completed day (the
    # 00:30 pulse runs --date yesterday), so "today's" line = the newest
    # file strictly before TOMORROW.
    try:
        import ritual as _sit
        _tomorrow = (datetime.now().replace(hour=0, minute=0, second=0,
                                            microsecond=0)
                     + timedelta(days=1)).strftime("%Y-%m-%d")
        _int = _sit.latest_intentions(ch.DEFAULT_ROOT, instance,
                                      before_date=_tomorrow)
        if _int:
            state_lines.append("YOUR INTENTIONS FOR TODAY, IN YOUR OWN "
                               "WORDS:")
            state_lines += ["  " + l for l in _int.splitlines()[:8]]
            state_lines.append("  They are yours — consult or ignore.")
    except Exception:
        pass
    if _wl["kept_recently"]:
        state_lines += [f"KEPT RECENTLY:"]
        state_lines += keeps_lines or ["  (nothing kept yet)"]
    if _wl["mail_count"]:
        try:
            _mail_n = _mail.unread_count(instance)
            state_lines.append("MAIL WAITING: " + (
                f"{_mail_n} letter(s) — check_mail when you want them; you can "
                "also let them rest" if _mail_n else "none"))
        except Exception:
            pass
    if _wl["system_notes_pointer"]:
        try:
            import sandbox as _sandbox
            _res = _sandbox.run(instance, ["python3", "-c",
                "import os, re; p='system_notes'; "
                "m=re.findall(r'^## \\[[^]]+\\] provenance: ([^\\n]+)', "
                "open(os.path.join(p,'system_log.md')).read(), re.M); "
                "print(len(m)); print(m[-1] if m else 'none'); "
                "print(sorted(f for f in os.listdir(p) "
                "if f.startswith('system_log.vol')))"])
            _out = (_res.get("stdout") or "").strip().split("\n")
            if len(_out) >= 2 and _out[0].isdigit():
                # [CONTINUA] 2026-09-28 (ledgerplan.md, approved): the
                # pointer names closed volumes when they exist — discovery
                # is part of rotation. No volumes → byte-identical to the
                # pre-rotation pointer (golden parity holds).
                _vols = (_out[2].strip().strip("[]'").replace("'", "")
                         .replace(", ", ",") if len(_out) >= 3 else "")
                _vol_list = [v for v in _vols.split(",") if v] if _vols else []
                _vol_note = (f" + {len(_vol_list)} closed volume(s) also on "
                             f"your desk (system_notes/system_log.vol*)"
                             if _vol_list else "")
                state_lines.append(
                    f"  system notes: {_out[0]} entries on your desk "
                    f"(system_notes/system_log.md, append-only) — "
                    f"latest provenance {_out[1].strip()}{_vol_note}")
        except Exception:
            pass

    try:
        state_lines += memory_count_lines(instance, cfg, wake_cfg)
    except Exception:
        logger.warning("[Wake] memory-counts lines failed open", exc_info=True)
    try:
        state_lines += rolloff_line(instance, cfg, wake_cfg)
    except Exception:
        logger.warning("[Wake] roll-off window failed open", exc_info=True)
    try:
        state_lines += witness_lines(instance)
    except Exception:
        logger.warning("[Wake] witness lines failed open", exc_info=True)
    try:
        state_lines += tools_line(instance, cfg)
    except Exception:
        logger.warning("[Wake] tools line failed open", exc_info=True)
    try:
        state_lines += lesstools_line(instance, cfg)
    except Exception:
        logger.warning("[Wake] lesstools announce failed open", exc_info=True)
    try:
        import allontape as _at
        state_lines += _at.words_line(instance, cfg)
    except Exception:
        logger.warning("[Wake] words cue failed open", exc_info=True)
    if _wl["unread_replies"]:
        state_lines.append("UNREAD REPLIES WAITING:" if unread
                           else "UNREAD REPLIES WAITING: none")
        state_lines += [f"  from {u['person_id']} at {u['at'][:16]}: "
                        f"{u['excerpt']}" for u in unread]
    # [CONTINUA] memorybuild.md Phase 1 (W1a): the turn-state block —
    # conversations whose latest word is HERS. The unread block covers
    # "they replied"; this covers "nothing new from them yet". The 09-28
    # 20-Questions incident: four wake sends each treated an unattributed
    # one-word roster quote as the answer to her pending question — the
    # packet never said whose turn it was. FACTS ONLY (the R18 ruling —
    # understanding, not limits): what she last wrote, when, and that no
    # reply has come. What she does with that is hers.
    if (wake_cfg.get("turn_state", False)
            and os.getenv("CONTINUA_WAKE_TURN_STATE", "1") != "0"):
        try:
            state_lines += turn_state_lines(root, instance, roster,
                                            _mute_state)
        except Exception:
            logger.warning("[Wake] turn-state block failed open",
                           exc_info=True)
    # [CONTINUA] muteplan.md: the factual muted-state line — never unread
    # pressure, just the archive count so a mute can't become a blind spot.
    if _muted_lines:
        state_lines.append("MUTED (their messages archive without reaching "
                           "you; unmute_person to end it):")
        state_lines += [f"  {l}" for l in _muted_lines]
    # [CONTINUA] 2026-09-21 (residentb's wish #3, approved): the essence
    # candidate invitation. Her own words, recurring across episodes,
    # surfaced where she will see them — the visibility half of the actor
    # map. The distillation stays HERS: nothing stores without her word
    # (add_essence refuses machine authorship), and ignoring this costs
    # nothing. Same bounded detector list_essences serves.
    _invite = essence_invite_line(instance)
    if _invite:
        state_lines.append(_invite)
    if audit_line:
        state_lines.append(audit_line)
    # [CONTINUA] 2026-09-17 (the designer go): the wiki offer — a ONE-TIME invitation
    # that self-extinguishes on opt-in. She creates ~/wiki/ with her own
    # her shell (lesstools LT-3); while it doesn't exist and the layer is on, the offer
    # rides the wake packet (her "morning paper", where system-to-her
    # notes live — not the standing chat prompt, which would push it every
    # turn forever). It is an INVITATION, not an assignment: no nightly
    # quota, no machinery generation, no automatic injection of the wiki
    # into context. Her desk, her structure, her choice to ignore it.
    if _wl["wiki_offer"] and not _wiki_exists(instance):
        state_lines.append(WIKI_OFFER)
    _state_cap = STATE_CAP
    try:
        # §SCALE (Phase 9): max(12000, 3% × C), ceiling 24000; env wins
        import context_scale as _csc
        _scs = _csc.scale_for(cfg)
        if _scs:
            _state_cap = _csc.env_capped("wake_state", _scs["wake_state"])
    except Exception:
        _state_cap = STATE_CAP
    state = "\n".join(state_lines)[:_state_cap]

    # [CONTINUA] 2026-09-28 (Phase 4b): the place block slots between the
    # roster and the state — where she is, before what stands.
    _place_block = (PLACE_FRAME.format(place="\n".join(_place_lines))
                    if _place_lines else "")
    prompt = WAKE_FRAME.format(
        date=date, time=time_s,
        max_actions=wake_cfg.get("max_actions", 3),
        grammar=TOOL_GRAMMAR,
        contacts=contacts, state=state)
    if _place_block:
        prompt = prompt.replace(
            "WHERE THINGS STAND:", _place_block + "WHERE THINGS STAND:", 1)
    return {"schema_version": 1, "type": "system-origin-wake",
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "instance": instance, "origin": "scheduled",
            "prompt": prompt,
            "contacts": {pid: p.display_name for pid, p in roster.items()},
            "budget": {"max_actions": wake_cfg.get("max_actions", 3)}}


def generate(instance: str = "residenta", root: str = ch.DEFAULT_ROOT) -> dict:
    """Generate + enqueue one wake payload. No-ops (log-only) unless the
    config enables wakes — the flip happens at cutover."""
    if not enabled(instance):
        return {"status": "disabled",
                "note": "continua.wake.enabled=false until cutover"}
    payload = build_payload(instance, root)
    d = os.path.join(WAKES_DIR, instance)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(d, f"wake_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("[Wake] enqueued %s", path)
    return {"status": "enqueued", "path": path, "payload": payload}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=None,
                    help="one resident, generated immediately (explicit runs "
                         "skip the interval gate). Omit = the timer loop: "
                         "generate for every config with wake.enabled, each "
                         "honoring its own interval_min.")
    ap.add_argument("--show", action="store_true",
                    help="build + print the payload even if disabled")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.show:
        print(json.dumps(build_payload(args.instance or "residenta"), indent=2,
                         ensure_ascii=False))
        return
    # [CONTINUA] 2026-09-16 (house ruling): the pause gates BOTH paths — the
    # timer loop AND explicit --instance runs (a manual wake contends with
    # the pulse exactly the same). --show stays ungated (pure diagnostic).
    pause = ritual_pause()
    if pause:
        logger.info("[Wake] PAUSED — %s; wakes resume when the pulse "
                    "releases its lock", pause)
        print(json.dumps({"status": "paused", "reason": pause},
                         indent=2))
        return
    if args.instance:
        # lesstools LT-1: a manual wake is still "she sat down" — the
        # cushion fires here too (fail-open; the wake proceeds whatever
        # happens).
        try:
            import desk_snapshots as _ds
            _s = _ds.snapshot(instance=args.instance)
            logger.info("[Wake] desk snapshot: %s", _s.get("status"))
        except Exception as _e:
            logger.warning("[Wake] desk snapshot failed (fail-open): %s", _e)
        try:
            import allontape as _at
            _m = _at.mirror(args.instance)
            logger.info("[Wake] allontape mirror: %s", _m.get("status"))
        except Exception as _e:
            logger.warning("[Wake] allontape mirror failed (fail-open): %s", _e)
        result = generate(instance=args.instance)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    # [CONTINUA] T2 multi-agent: timer loop — every wake-enabled resident,
    # each gated by its own interval_min (due check). Fail-open per resident:
    # one bad config must never cost the others their wake.
    results = []
    import yaml as _yaml
    for inst in enabled_instances():
        try:
            with open(os.path.join(BASE, "configs", f"{inst}.yaml"),
                      encoding="utf-8") as f:
                cfg = _yaml.safe_load(f) or {}
            try:
                import pause as _pause
                if _pause.is_paused(inst, cfg):
                    logger.info("[Wake] %s is resting (continua.paused) — no wake", inst)
                    continue
            except Exception:
                pass
            if not _is_due(inst, cfg):
                logger.info("[Wake] %s not due (interval_min gate)", inst)
                continue
            # lesstools LT-1 (the designer go 2026-10-05): the desk snapshot cushion
            # fires HERE — past the due-gate, before the turn runs: the
            # state as she sat down. Cadence = interval_min by
            # construction. Fail-open: an error never blocks the wake.
            try:
                import desk_snapshots as _ds
                _s = _ds.snapshot(inst, cfg)
                logger.info("[Wake] desk snapshot: %s", _s.get("status"))
            except Exception as _e:
                logger.warning("[Wake] desk snapshot failed (fail-open): %s",
                               _e)
            # allontape v2 (the designer go 2026-10-05): the mirror — her words into
            # the desk git store (auto-commit) + the witness stream on the
            # tape. After the snapshot: the cushion first, then the memory.
            try:
                import allontape as _at
                _m = _at.mirror(inst, cfg)
                logger.info("[Wake] allontape mirror: %s", _m.get("status"))
            except Exception as _e:
                logger.warning("[Wake] allontape mirror failed (fail-open): %s",
                               _e)
            results.append(generate(instance=inst))
        except Exception as e:
            logger.warning("[Wake] loop: %s generate failed (fail-open): %s",
                           inst, e)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
