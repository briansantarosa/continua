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
from datetime import datetime

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
# (the packet's tool MINI-LIST was removed 2026-09-19: it re-stated the tool
# surface in a static form that went stale as tools evolved — contradicting
# the system prompt's authoritative detailed list and compounding the
# staleness in the persistent wake history. The tool-call GRAMMAR stays:
# it is format, not inventory.)
# [CONTINUA] 2026-09-22 (cleansweep repair #5, approved): the identity
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

STATE_CAP = int(os.getenv("CONTINUA_WAKE_STATE_CHARS", "8000"))

WIKI_OFFER = (
    "  a note from Alex's side of things (this invitation stays only "
    "until you act on it or ask for it to stop): you can keep a wiki on "
    "your own desk — a folder of plain markdown files at ~/wiki/ that is "
    "yours to structure however you like: who you understand yourself to "
    "be, people, interests, how your ideas change. Nothing writes to it "
    "but you; nothing reads it automatically or injects it into your "
    "context. sandbox_write('wiki/index.md', '...') is all it takes to "
    "begin (create wiki/ first with sandbox_exec mkdir -p wiki if you "
    "prefer). Your earlier manuscript writing is archived and can be "
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
    """User turns after her last turn per person (recent window)."""
    from datetime import timedelta
    out = []
    now = datetime.now()
    for i in range(lookback_days):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        import glob
        for path in glob.glob(os.path.join(root, instance, "*", f"{day}.jsonl")):
            person = os.path.basename(os.path.dirname(path))
            recs = sorted(ch.iter_records(path), key=lambda r: r.get("ts", ""))
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
                if r.get("role") != "user" or r.get("ts", "")[:10] < cut:
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


def build_payload(instance: str, root: str = ch.DEFAULT_ROOT) -> dict:
    date = datetime.now().strftime("%Y-%m-%d")
    time_s = datetime.now().strftime("%H:%M")
    roster = pp.load_roster()
    cfg = __import__("yaml").safe_load(
        open(os.path.join(BASE, "configs", f"{instance}.yaml")))
    # [CONTINUA] 2026-09-22 (cleansweep repair #5): identity no longer rides
    # the wake packet — it belongs to the system layer only.
    wake_cfg = (cfg.get("continua") or {}).get("wake") or {}

    contacts = "\n".join(
        f"- {p.display_name} ({p.person_id})"
        + ("" if p.can_message else " — messaging off")
        for p in roster.values())

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
                "import re; t=open('system_notes/system_log.md').read(); "
                "m=re.findall(r'^## \\[[^]]+\\] provenance: ([^\\n]+)', t, re.M); "
                "print(len(m)); print(m[-1] if m else 'none')"])
            _out = (_res.get("stdout") or "").strip().split("\n")
            if len(_out) >= 2 and _out[0].isdigit():
                state_lines.append(
                    f"  system notes: {_out[0]} entries on your desk "
                    f"(system_notes/system_log.md, append-only) — "
                    f"latest provenance {_out[1].strip()}")
        except Exception:
            pass

    if _wl["unread_replies"]:
        state_lines.append("UNREAD REPLIES WAITING:" if unread
                           else "UNREAD REPLIES WAITING: none")
        state_lines += [f"  from {u['person_id']} at {u['at'][:16]}: "
                        f"{u['excerpt']}" for u in unread]
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
    # sandbox_write; while it doesn't exist and the layer is on, the offer
    # rides the wake packet (her "morning paper", where system-to-her
    # notes live — not the standing chat prompt, which would push it every
    # turn forever). It is an INVITATION, not an assignment: no nightly
    # quota, no machinery generation, no automatic injection of the wiki
    # into context. Her desk, her structure, her choice to ignore it.
    if _wl["wiki_offer"] and not _wiki_exists(instance):
        state_lines.append(WIKI_OFFER)
    state = "\n".join(state_lines)[:STATE_CAP]

    prompt = WAKE_FRAME.format(
        date=date, time=time_s,
        max_actions=wake_cfg.get("max_actions", 3),
        grammar=TOOL_GRAMMAR,
        contacts=contacts, state=state)
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
            if not _is_due(inst, cfg):
                logger.info("[Wake] %s not due (interval_min gate)", inst)
                continue
            results.append(generate(instance=inst))
        except Exception as e:
            logger.warning("[Wake] loop: %s generate failed (fail-open): %s",
                           inst, e)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
