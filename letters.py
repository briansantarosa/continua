"""letters.py — continua↔continua persona letters (the in-process route).

T3 multi-agent (2026-09-11). house ruling, same day: resident #2 IS
letter-reachable from residenta day one — the in-process route is confirmed
in scope.

WHY IN-PROCESS: Sagent's control server (the sagent-resident letter route)
cannot load a Continua config — its CONFIG_DIR is Sagent's. A continua
target's mem0 store locks belong to THIS bridge process, so her turn runs
here — the same machinery the wake consumer already uses. The letter
DISCIPLINE is identical to the sagent route:

  - the target's turn is her NATIVE turn (her own mem0 collection, her own
    history under user_id 'continua:<sender>' — the sender arrives as a
    person like anyone else; her chronicle dual-write records it)
  - the reply is NEVER returned inline into the turn that sent the letter
    (the choice discipline): it is deposited into the SENDER's per-instance
    inbox, and reading it is the sender's next deliberate act
  - async: the send returns immediately; the reply arrives when ready
  - one letter turn per target at a time (parity with the control server's
    409 on a second concurrent turn for the same persona)

The deposit schema matches Sagent's control_server._run_letter_turn
byte-for-byte in the fields check_mail consumes (schema_version, ts,
from_person_id, from_name, text, status) — from_name carries the roster
display name here (the control server uses the bare bot id; both are
provenance, attribution resolves through the roster either way).

Kill switch: CONTINUA_LETTERS=0 refuses the in-process route.
"""

import json
import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger("continua.letters")

BASE = os.path.dirname(os.path.abspath(__file__))

# one letter turn per target at a time — the same guard the Sagent control
# server applies per config (two concurrent turns on one agent would contend
# for the same history file and engine).
_inflight = set()
_inflight_lock = threading.Lock()


def deliver(sender_instance: str, person, text: str,
            target_config: str) -> tuple[bool, str]:
    """Accept one continua↔continua letter. Spawns the target resident's
    native turn in a background thread; the reply lands in the SENDER's
    per-instance inbox when ready. Returns (accepted, reason).

    person is the roster Person (kind=persona) whose person_id IS the
    target's bot id (roster keys personas by bot id — attribution contract);
    target_config is the target's config FILENAME inside continua/configs.
    """
    if os.environ.get("CONTINUA_LETTERS", "") == "0":
        return False, "letters disabled (CONTINUA_LETTERS=0)"
    with _inflight_lock:
        if person.person_id in _inflight:
            return False, (f"a letter turn is already running for "
                           f"{person.display_name} — try again later")
        _inflight.add(person.person_id)
    t = threading.Thread(
        target=_run_turn,
        args=(sender_instance, person.person_id, str(person.display_name),
              target_config, text[:4000]),
        name=f"letter-{person.person_id}-{sender_instance}",
        daemon=True)
    t.start()
    logger.info("[Letters] accepted %s → %s (async turn)",
                sender_instance, person.display_name)
    return True, "queued"


def _other_instance(instance: str) -> str:
    """The OTHER resident's id (two-resident system: residentb↔residenta)."""
    return 'residenta' if instance == 'residentb' else 'residentb'


def _deposit_letter(inbox: str, from_person_id: str, from_name: str,
                    text: str, sender_instance: str = None,
                    target_person_id: str = None) -> None:
    """Append one reply letter to the sender's inbox. The schema matches
    Sagent's control_server._run_letter_turn in every field check_mail
    consumes; from_name carries the roster display name here (honest
    provenance at send time — attribution still resolves via the roster).

    §6c "no unreachable source" (2026-09-19): the reply ALSO lands in the
    sender's chronicle (her side of the exchange — the other resident's
    words arriving). Without this row the correspondence existed only in
    the recipient's memory; the sender kept no trace of her own letters or
    the replies to them."""
    try:
        import chronicle as _ch
        # house ruling 2026-09-20: attribute the reply to the letter's actual
        # sender (the roster id — renders as their name), matching the
        # backfill's per-person attribution. A continua:* id would break
        # roster-name resolution and fork the person dir.
        _ch.append({"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "instance": sender_instance,
                    "person_id": str(from_person_id),
                    "role": "user",
                    "content": str(text).strip(),
                    "uid": "letter-reply-live-"
                           + datetime.now().strftime("%Y%m%dT%H%M%S")})
    except Exception:
        logger.warning("[Letters] reply chronicle capture failed (fail-open)",
                       exc_info=True)
    letter = {
        "schema_version": 1,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "from_person_id": from_person_id,
        "from_name": from_name,
        "text": text.strip(),
        "status": "unread",
    }
    os.makedirs(os.path.dirname(inbox), exist_ok=True)
    with open(inbox, "a", encoding="utf-8") as f:
        f.write(json.dumps(letter, ensure_ascii=False) + "\n")


def _run_turn(sender_instance: str, target_person_id: str,
              target_name: str, target_config: str, text: str) -> None:
    """The target's native turn, in-process (same mem0 locks as telegram
    turns and wake turns). Never raises — a failed letter is a logged
    warning; the sender's outbox already records 'queued' (parity with the
    control-server route) and the no-reply cooldown bounds retries."""
    try:
        import bridge as _bridge
        import mail as _mail
        agent = _bridge.agent_manager.get_agent(target_config)
        # the sender arrives as a person like anyone else: user_id encodes
        # the sender identity (control-server convention, unmodified)
        user_id = f"continua:{sender_instance}"
        hist = _bridge._load_history_from_disk(target_config, user_id)
        hist.append({"role": "user", "content": text})
        reply, hist = agent.generate_response(user_id, hist)
        _bridge._save_history_to_disk(target_config, user_id, hist)
        if reply and reply.strip():
            # from = the REPLIER (her roster person_id resolves attribution
            # in the sender's check_mail — the same contract as the
            # control-server route)
            import mail as _mail
            _deposit_letter(_mail.inbox_path(sender_instance),
                            from_person_id=target_person_id,
                            from_name=target_name,
                            text=reply,
                            sender_instance=sender_instance)
        logger.info("[Letters] turn complete %s → %s (reply %s chars)",
                    sender_instance, target_name, len(reply or ""))
    except Exception as e:
        logger.error("[Letters] turn failed for %s (sender %s): %s",
                     target_person_id, sender_instance, e)
    finally:
        with _inflight_lock:
            _inflight.discard(target_person_id)

def backfill_letter_chronicle(instance: str, dry_run: bool = False) -> dict:
    """§6c 'no unreachable source' — the repair is recoverable, not just
    forward-looking: the letter route's outbox (her sends) and inbox (the
    replies) predate the chronicle capture points. Backfill BOTH into the
    sender's chronicle as letter rows (uid-stamped, idempotent — a rerun
    skips rows already present). Bounded by the files that exist."""
    from pathlib import Path as _P
    import glob as _g
    base = _P(__file__).resolve().parent
    rows, skipped = [], 0
    # her sends (outbound): role assistant — HER words to each person,
    # attributed to that person (the roster renders the name)
    for f in sorted(_g.glob(str(base / "outbound" / instance / "*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not (e.get("letter") and e.get("queued") and e.get("ts")):
                continue  # successful persona letters only
            uid = "letter-send-" + str(e.get("ts", ""))[:19]
            rows.append({"ts": str(e["ts"]), "uid": uid,
                         "role": "assistant",
                         "person_id": str(e.get("person_id") or ""),
                         "content": str(e.get("text") or "")})
    # the replies (inbox): role user — the other's words arriving, attributed
    inbox = base / "mail" / "inbox" / f"{instance}.jsonl"
    if inbox.exists():
        for line in open(inbox, encoding="utf-8"):
            try:
                l = json.loads(line)
            except Exception:
                continue
            if not l.get("text") or not l.get("ts") or not l.get("from_person_id"):
                continue
            uid = "letter-reply-" + str(l.get("ts", ""))[:19]
            rows.append({"ts": str(l["ts"]), "uid": uid,
                         "role": "user",
                         "person_id": str(l["from_person_id"]),
                         "content": str(l["text"])})
    # existing uids + content signatures (idempotence). The sig set also
    # matches live captures (letter-reply-live-*): a backfill rerun must
    # never duplicate a reply the live capture already wrote.
    have, sigs = set(), set()
    for f in _g.glob(str(base / "chronicle" / instance / "*" / "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            try:
                r = json.loads(line)
                if r.get("uid"):
                    have.add(r["uid"])
                    if str(r["uid"]).startswith("letter"):
                        sigs.add((str(r.get("person_id")), str(r.get("role")),
                                  str(r.get("content", ""))[:120]))
            except Exception:
                continue
    import chronicle as _ch
    written = 0
    for row in sorted(rows, key=lambda r: r["ts"]):
        if row["uid"] in have:
            skipped += 1
            continue
        if (row["person_id"], row["role"], row["content"][:120]) in sigs:
            skipped += 1  # already captured live (letter-reply-live-*)
            continue
        if dry_run:
            written += 1
            continue
        _ch.append({"ts": row["ts"], "instance": instance,
                    "person_id": row["person_id"],
                    "role": row["role"], "content": row["content"],
                    "uid": row["uid"]})
        written += 1
    return {"candidate_rows": len(rows), "written": written, "skipped": skipped}
