"""send.py — outbound Telegram SHE initiates (autonomy track B).

Phase: wiki projects/Continua.md, "Outbound Telegram (she initiates)".
Governance per the plan:
  - Contact allowlist: the `people:` roster (can_message) — Telegram bots
    cannot initiate anyway (free structural floor).
  - Per-contact daily cap (people.daily_cap) + minimum spacing between sends
    to the same contact.
  - No-reply cooldown: if her last 2 sends to a contact produced no reply
    (no user turn after them in the chronicle), sending is blocked —
    the stamp-cascade lesson generalized: assume loop failure modes exist.
  - Append-only outbound log (every send, sender, cap-state) — feeds the
    chronicle like any other episodic event.
  - Honesty contract is the caller's prompt duty (initiated contact reads as
    initiated); this module enforces the mechanics only.

She sends from HER OWN bot identity (residenta's token from the Continua config
— read at call time; never copied into code). Kill switch CONTINUA_SEND=0.
"""

import json
import re
import logging
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chronicle as ch
import people as pp

logger = logging.getLogger("continua.send")

BASE = os.path.dirname(os.path.abspath(__file__))
OUTBOX_DIR = os.path.join(BASE, "outbound")
MIN_SPACING_MIN = int(os.getenv("CONTINUA_SEND_SPACING_MIN", "60"))
NO_REPLY_LIMIT = int(os.getenv("CONTINUA_SEND_NOREPLY_LIMIT", "10"))


def _config(instance: str) -> dict:
    import yaml
    with open(os.path.join(BASE, "configs", f"{instance}.yaml"),
              encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def outbox_path(instance: str, date: str) -> str:
    return os.path.join(OUTBOX_DIR, instance, f"{date}.jsonl")


def sent_today(instance: str, person_id: str, date: str) -> list:
    """Successful sends only (delivered or queued letters) — failed wire
    attempts don't consume the slot."""
    path = outbox_path(instance, date)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    m = json.loads(line)
                    if (str(m.get("person_id")) == str(person_id)
                            and (m.get("delivered") or m.get("queued"))):
                        out.append(m)
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("[Send] outbox read failed (fail-open): %s", e)
    return out


def _answered_today(instance: str, person_id: str, sent: list) -> bool:
    """house ruling 2026-09-20: a cap governs MONOLOGUE, not conversation.
    True when any reply from this person arrived after the first send today
    — the inbox is the definitive letter-arrival record. Fail-open to
    False: unreadable mail keeps the cap's protection."""
    if not sent:
        return False
    first = str(sent[0].get("ts", ""))
    try:
        import mail as _mail
        inbox = _mail.inbox_path(instance)
        with open(inbox, encoding="utf-8") as f:
            for line in f:
                try:
                    l = json.loads(line)
                except Exception:
                    continue
                if (str(l.get("from_person_id")) == str(person_id)
                        and str(l.get("ts", "")) >= first):
                    return True
    except Exception:
        pass
    return False


def governance_check(instance: str, person_id: str, roster: dict,
                     root: str = ch.DEFAULT_ROOT) -> tuple[bool, str]:
    """All mechanics that gate a send. Returns (allowed, reason)."""
    person = roster.get(str(person_id))
    if not person:
        return False, f"person {person_id} not in roster"
    if not person.can_message:
        return False, f"{person.display_name} is not on the contact allowlist"
    # [CONTINUA] persona letters route via the control wire — no chat_id
    # needed (supersedes the group-chat design, 2026-09-07).
    cap = person.daily_cap or 0
    today = datetime.now().strftime("%Y-%m-%d")
    sent = sent_today(instance, person_id, today)
    if cap and len(sent) >= cap:
        # [CONTINUA] house ruling 2026-09-20 ("The Architecture of the Pause"):
        # she was refused mid-live-conversation — 10 answered letters and the
        # cap treated a mutual exchange like a monologue. The cap governs
        # UNRESPONDED volume: if the other side has answered anything today,
        # the day is a live conversation and the cap stands down. The
        # no-reply cooldown below remains the backstop for a one-way burst.
        if not _answered_today(instance, person_id, sent):
            return False, (f"daily cap reached for {person.display_name} "
                           f"({len(sent)}/{cap})")
    if sent:
        last = sent[-1]
        try:
            last_dt = datetime.fromisoformat(last["ts"])
            mins = (datetime.now().astimezone() - last_dt).total_seconds() / 60
            if mins < MIN_SPACING_MIN:
                return False, (f"spacing: last send to {person.display_name} "
                               f"was {mins:.0f} min ago "
                               f"(min {MIN_SPACING_MIN})")
        except (KeyError, ValueError):
            pass
    # no-reply cooldown — house ruling 2026-09-12: a 12-HOUR window, and the
    # trigger is 10 unanswered messages (was: 2 unanswered in 7 days — the
    # 7-day unsorted window misfired and locked her out mid-conversation).
    # ANY user reply after her sends clears the count: "once we get one
    # reply we let the others go through."
    recs = [r for day in _recent_days(root, instance, 2)
            for r in ch.iter_records(os.path.join(root, instance, person_id,
                                                  f"{day}.jsonl"))]
    recs.sort(key=lambda r: r.get("ts", ""))
    _cutoff = (datetime.now() - timedelta(hours=12)).isoformat()
    recs = [r for r in recs if r.get("ts", "") >= _cutoff]
    her_sends = [r for r in sent if True]
    unanswered = 0
    for msg in reversed(her_sends):
        after = [r for r in recs if r.get("role") == "user"
                 and r.get("ts", "") > msg.get("ts", "")]
        if not after:
            unanswered += 1
        else:
            break
    if unanswered >= NO_REPLY_LIMIT:
        return False, (f"no-reply cooldown: {unanswered} messages to "
                       f"{person.display_name} in the last 12h without a reply")
    return True, "ok"


def _recent_days(root: str, instance: str, n: int) -> list:
    from datetime import datetime as dt
    out = []
    for i in range(n):
        out.append((dt.now() - timedelta(days=i)).strftime("%Y-%m-%d"))
    return out


SAGENT_CONFIGS = "/home/you/Sagent/configs"


def _persona_config(person) -> str:
    """Resolve a persona roster entry to its Sagent config filename by bot
    id (the roster id IS the bot id; the token prefix in Sagent's configs
    matches). Cached per process. Sagent-resident targets only — continua
    residents resolve via _continua_persona_config (T3)."""
    global _PERSONA_CONFIG_CACHE
    key = str(person.person_id)
    if key in _PERSONA_CONFIG_CACHE:
        return _PERSONA_CONFIG_CACHE[key]
    import glob
    for path in glob.glob(os.path.join(SAGENT_CONFIGS, "*.yaml")):
        try:
            head = open(path).read()
            if re.search(rf"token:\s*['\"]?{key}:", head):
                _PERSONA_CONFIG_CACHE[key] = os.path.basename(path)
                return _PERSONA_CONFIG_CACHE[key]
        except OSError:
            continue
    raise ValueError(f"no Sagent config found for bot id {key}")


def _continua_persona_config(person, config_dir: str = None) -> str | None:
    """Resolve a roster persona entry to a CONTINUA config filename by bot
    id (T3 2026-09-11): a resident of THIS bridge. Same token-prefix match
    as _persona_config. None = the target does not live in Continua → the
    Sagent control-server route applies. Continua targets CANNOT use that
    route (the control server's CONFIG_DIR is Sagent's) — that is why the
    in-process route exists (letters.py)."""
    key = str(person.person_id)
    import glob
    for path in sorted(glob.glob(os.path.join(
            config_dir or os.path.join(BASE, "configs"), "*.yaml"))):
        try:
            head = open(path).read()
            if re.search(rf"token:\s*['\"]?{key}:", head):
                return os.path.basename(path)
        except OSError:
            continue
    return None

_PERSONA_CONFIG_CACHE = {}


def send(instance: str, person_id: str, text: str,
         root: str = ch.DEFAULT_ROOT) -> dict:
    """Governed outbound send from her bot identity. Append-only outbox log.
    Returns the log entry (with ok/denied + reason)."""
    if os.environ.get("CONTINUA_SEND", "") == "0":
        return {"ok": False, "denied": True, "reason": "CONTINUA_SEND=0"}
    # [CONTINUA] T1 multi-agent (2026-09-11, plan tier 1): governance reads
    # THIS persona's roster block only — daily caps and no-reply cooldowns
    # are per (persona, contact), and can_message in persona B's block must
    # not be overridden by persona A's (merged roster = first config wins).
    # Attribution surfaces (mail, index) keep the merged roster on purpose:
    # attributing a letter needs the OTHER config's people entries too.
    roster = pp.load_roster(instance=instance)
    person_id = str(person_id)
    person = roster.get(person_id)
    allowed, reason = governance_check(instance, person_id, roster, root)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    entry = {"schema_version": 1, "ts": now, "instance": instance,
             "person_id": person_id,
             "person_name": pp.name_for(roster, person_id),
             "kind": getattr(person, "kind", "human"),
             "text": text, "allowed": allowed, "denied_reason": reason,
             "delivered": False}
    if not allowed:
        logger.warning("[Send] denied: %s", reason)
        _log_outbox(instance, entry)
        return entry
    try:
        # [CONTINUA] HOTFIX 2026-09-12: the urllib imports lived INSIDE the
        # persona-letter branch — Python then treats urllib as a function-
        # local everywhere, and a HUMAN send (branch skipped) died with
        # UnboundLocalError before delivery. Latent since a229213 (the
        # family wire); surfaced by residentb's first human sends (6 attempts,
        # all failed silently as "[Send failed — logged]"). Imports now sit
        # at function level, shared by both routes.
        import urllib.parse
        import urllib.request
        # persona letters — the reply is NEVER returned inline
        # into the turn that sent the letter (the choice discipline); it
        # waits in the SENDER'S per-instance inbox (T3: inbox_path(instance),
        # not a process-global path — the never-mix fix).
        if getattr(person, "kind", "human") == "persona":
            target_local = _continua_persona_config(person)
            if target_local == f"{instance}.yaml":
                entry["denied_reason"] = ("that is you — a letter to yourself "
                                          "is a bookmark_note, not a send")
                entry["allowed"] = False
                logger.warning("[Send] denied self-letter: %s", instance)
                _log_outbox(instance, entry)
                return entry
            if target_local:
                # [CONTINUA] T3 (house ruling 2026-09-11: resident #2 is
                # letter-reachable day one): continua-resident targets run
                # their turn IN THIS PROCESS (their mem0 store locks belong
                # to the bridge) — letters.py spawns the native turn and
                # deposits the reply into the SENDER'S inbox, never inline.
                import letters as _letters
                accepted, why = _letters.deliver(instance, person, text,
                                                 target_local)
                entry["queued"] = bool(accepted)
                entry["letter"] = True
                entry["delivered"] = bool(accepted)
                entry["route"] = "inprocess"
                if not accepted:
                    entry["denied_reason"] = why
                _log_outbox(instance, entry)
                # §6c "no unreachable source" (2026-09-19): the letter is an
                # experience she had — capture it in HER chronicle (her words,
                # addressed to the other resident). Without this row her
                # correspondence was invisible to her own memory system
                # (residenta's side captured both edges via the native letter
                # turn; the sender's side captured nothing).
                if accepted:
                    try:
                        import chronicle as _ch
                        _ch.append({"ts": datetime.now().astimezone().isoformat(
                                        timespec="seconds"),
                                    "instance": instance,
                                    "person_id": str(entry.get("person_id")),
                                    "role": "assistant",
                                    "content": str(text),
                                    "uid": ("letter-send-" + str(entry.get("ts", ""))[:19]
                                            + "-" + str(abs(hash(text)) % 10**8))})
                    except Exception:
                        logger.warning("[Send] letter chronicle capture failed "
                                       "(fail-open)", exc_info=True)
                return entry
            # Sagent's bridge (via the control wire) — memories stay native
            # on their side; the reply is deposited into HER inbox.
            port = os.environ.get("SAGENT_CONTROL_PORT", "")
            token = os.environ.get("SAGENT_CONTROL_TOKEN", "")
            if not port or not token:
                raise ValueError("control wire not configured")
            import mail as _mailmod
            inbox = _mailmod.inbox_path(instance)
            target_cfg = _persona_config(person)
            data = json.dumps({
                "token": token,
                "config": target_cfg,
                "user_id": f"continua:{instance}",
                "text": text[:4000],
                "reply_file": inbox,
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/agent-turn", data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read())
            entry["queued"] = bool(res.get("queued"))
            entry["letter"] = True
            entry["delivered"] = bool(res.get("queued"))
            _log_outbox(instance, entry)
            return entry
        token = _config(instance).get("telegram", {}).get("token")
        if not token:
            raise ValueError(f"no telegram token in {instance} config")
        # personas are messaged via their shared group chat (bots can't DM
        # bots); humans via their own chat id
        target = person.chat_id or person_id
        data = urllib.parse.urlencode({
            "chat_id": target, "text": text[:4000],
            "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as resp:
            _tg = json.loads(resp.read())
            # [CONTINUA] the designer ask 2026-09-12: ok:false with no reason logged
            # is undiagnosable — carry the Telegram description in the
            # outbox (residentb's 16:17-16:47 sends failed ok:false with
            # error: None and nothing to explain why).
            entry["delivered"] = bool(_tg.get("ok"))
            if not entry["delivered"]:
                entry["telegram_error"] = str(_tg.get("description", "unknown"))[:200]
    except Exception as e:
        logger.warning("[Send] delivery failed (fail-open): %s", e)
        entry["error"] = str(e)[:200]
    _log_outbox(instance, entry)
    return entry


def _log_outbox(instance: str, entry: dict) -> None:
    try:
        date = datetime.now().strftime("%Y-%m-%d")
        path = outbox_path(instance, date)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("[Send] outbox write failed", exc_info=True)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="person id (roster key)")
    ap.add_argument("--text", default="")
    ap.add_argument("--instance", default="residenta")
    ap.add_argument("--check", action="store_true",
                    help="run governance only, don't send")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    roster = pp.load_roster()
    if args.check:
        allowed, reason = governance_check(args.instance, args.to, roster)
        print(json.dumps({"allowed": allowed, "reason": reason}))
        return
    print(json.dumps(send(args.instance, args.to, args.text),
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
