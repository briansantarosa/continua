"""mute.py — agent-owned mute for human Telegram contacts (muteplan.md).

house rulings baked in (2026-09-27/28):
  - Mute is a TWO-WAY contact pause for HUMANS ONLY (roster kind: human).
    Personas are unmutable — their letters already wait unread until she
    chooses (the letter model IS the gate).
  - Inbound: the muted human's messages are CAPTURED to the chronicle
    (arrived_muted: true — her memory keeps everything) but never start a
    turn; the bridge sends them a reason-free machinery notice, ONCE PER
    MUTE EPISODE, and marks the notice in the chronicle.
  - Outbound: send.py governance_check DENIES sends to a muted person with
    a teaching reason that includes the unmute directions.
  - Narrow-only invariant: mute can only reduce what reaches her; unmute
    restores exactly the designer's config baseline (can_message: false stays off).
  - Fail-open to UNMUTED: corrupt/missing state = no mutes — the overlay
    must never add silence by accident; config is the authority.
  - Identity: roster-only resolution (anti-impersonation), per-instance.
  - the designer's exemption: roster can_mute: false refuses the mute (default true).

State: mutes/<instance>.json — {person_id: {ts, reason, by, notice_last_ts,
notice_count}}. Atomic writes, 0600. Kill switch CONTINUA_MUTE=0 makes
every caller ignore mutes entirely (checked by callers, not here, so the
state itself is never silently mutated by a disabled run).
"""

import json
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger("continua.mute")

BASE = os.path.dirname(os.path.abspath(__file__))
MUTE_DIR = os.getenv("CONTINUA_MUTE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mutes"))

# same shape contract as mail.py — instance ids are filesystem path
# components AND chronicle path components; fail loud on traversal.
_INSTANCE_RE = re.compile(r"^[a-z0-9_]+$")

KILL_SWITCH_ENV = "CONTINUA_MUTE"

# The fixed machinery notice (design ruling: reason-free). Infrastructure voice —
# NEVER hers; the bridge sends it directly, never through send.py.
NOTICE_TEXT = (
    "(machinery note — not her words: you have been muted, at her own "
    "choice. While the mute is in place she will not see your messages — "
    "they are being archived — and she cannot write to you. She can end "
    "it at any time.)"
)


def enabled() -> bool:
    """Fleet kill switch (CONTINUA_MUTE=0 ignores mutes entirely —
    fail-open to the designer's config baseline)."""
    return os.environ.get(KILL_SWITCH_ENV, "1") != "0"


def _instance_ok(instance: str) -> bool:
    return bool(_INSTANCE_RE.match(instance or ""))


def state_path(instance: str) -> str:
    if not _instance_ok(instance):
        raise ValueError(f"bad instance id for mute: {instance!r}")
    return os.path.join(MUTE_DIR, f"{instance}.json")


def load(instance: str) -> dict:
    """{person_id: entry} — fail-open to {} (unmuted) on any problem."""
    try:
        with open(state_path(instance), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("[Mute] state load failed (fail-open to unmuted): %s", e)
        return {}


def _save(instance: str, state: dict) -> None:
    os.makedirs(MUTE_DIR, exist_ok=True, mode=0o700)
    path = state_path(instance)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    try:
        os.chmod(MUTE_DIR, 0o700)
    except OSError:
        pass


def muted_entry(instance: str, person_id: str):
    """The mute entry for a person, or None. Fail-open to None."""
    return load(instance).get(str(person_id))


def is_muted(instance: str, person_id: str) -> bool:
    return muted_entry(instance, person_id) is not None


def _validate(instance: str, person_id: str, roster: dict):
    """Shared validation for mute(). Returns (person, error_string)."""
    if not _instance_ok(instance):
        return None, f"bad instance id: {instance!r}"
    person = roster.get(str(person_id))
    if not person:
        return None, (f"person {person_id} is not on your people list — "
                      "mutes resolve against your roster only")
    if person.kind == "persona":
        return None, (f"{person.display_name} is a persona contact — mutes "
                      "apply to human chats; their letters already wait "
                      "unread until you choose")
    if not person.can_mute:
        return None, (f"{person.display_name} cannot be muted (exempted by "
                      "the humans who run this system)")
    return person, None


def mute(instance: str, person_id: str, reason: str = "", by: str = "self",
         roster: dict = None) -> dict:
    """Mute a human. Idempotent: an existing mute is returned unchanged.
    Returns {"ok": bool, "entry": dict|None, "error": str, "person": Person}."""
    import people as _pp
    roster = roster if roster is not None else _pp.load_roster(instance=instance)
    person, err = _validate(instance, person_id, roster)
    if err:
        return {"ok": False, "entry": None, "error": err, "person": None}
    state = load(instance)
    pid = str(person_id)
    if pid in state:
        return {"ok": True, "entry": state[pid], "error": "", "person": person,
                "already": True}
    entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
             "reason": str(reason or "").strip(),
             "by": by or "self",
             "notice_last_ts": None,
             "notice_count": 0}
    state[pid] = entry
    _save(instance, state)
    logger.info("[Mute] %s muted %s (%s) by %s", instance, pid,
                person.display_name, entry["by"])
    return {"ok": True, "entry": entry, "error": "", "person": person}


def unmute(instance: str, person_id: str, by: str = "self",
           roster: dict = None) -> dict:
    """End a mute. Idempotent (unmuting an unmuted person is ok=False only
    in the honest-report sense — the state is already what she asked for).
    Never widens anything: the roster's can_message still governs sending."""
    import people as _pp
    roster = roster if roster is not None else _pp.load_roster(instance=instance)
    if not _instance_ok(instance):
        return {"ok": False, "error": f"bad instance id: {instance!r}",
                "person": None}
    person = roster.get(str(person_id))
    state = load(instance)
    pid = str(person_id)
    if pid in state:
        was = state[pid]
        del state[pid]
        _save(instance, state)
        logger.info("[Mute] %s UNmuted %s by %s", instance, pid, by or "self")
        return {"ok": True, "error": "", "person": person, "was": was}
    return {"ok": True, "error": "was not muted", "person": person}


def should_notice(instance: str, person_id: str) -> bool:
    """True when the muted person has NOT yet been noticed this episode
    (design ruling: once per mute episode)."""
    entry = muted_entry(instance, person_id)
    if not entry:
        return False
    return not entry.get("notice_last_ts")


def mark_noticed(instance: str, person_id: str) -> None:
    """Stamp the notice into the episode (bridge-side, after the send)."""
    state = load(instance)
    pid = str(person_id)
    if pid not in state:
        return
    state[pid]["notice_last_ts"] = datetime.now().astimezone().isoformat(
        timespec="seconds")
    state[pid]["notice_count"] = int(state[pid].get("notice_count") or 0) + 1
    _save(instance, state)


def archived_count_since(instance: str, person_id: str, since_ts: str,
                         root: str = None) -> int:
    """Muted arrivals archived since the mute's ts — the wake packet's
    factual count line. Fail-open to 0."""
    try:
        import chronicle as _ch
        root = root or _ch.DEFAULT_ROOT
        count = 0
        day = str(since_ts or "")[:10]
        from datetime import timedelta
        start = datetime.strptime(day, "%Y-%m-%d")
        end = datetime.strptime(datetime.now().strftime("%Y-%m-%d"),
                                "%Y-%m-%d")
        while start <= end:
            path = os.path.join(root, instance, str(person_id),
                                start.strftime("%Y-%m-%d") + ".jsonl")
            for r in _ch.iter_records(path):
                if (r.get("arrived_muted")
                        and str(r.get("ts") or "") >= str(since_ts or "")):
                    count += 1
            start += timedelta(days=1)
        return count
    except Exception as e:
        logger.warning("[Mute] archived count failed (fail-open): %s", e)
        return 0
