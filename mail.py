"""mail.py — the letter inbox (autonomy track, the family wire).

Persona replies arrive here (Sagent's control server deposits them after
the persona's native turn completes — the persona auto-replies exactly as
it does to Alex; T3 2026-09-11: continua-resident targets run their turn
in-process via letters.py). Letters are append-only; unread state is a
marker, so reading never rewrites the log.

The choice discipline (house ruling): replies are NEVER delivered inline into
the turn that sent the letter. They wait here, and reading them is a
separate act — continuing a thread is always her deliberate choice.

T3 multi-agent (2026-09-11): the inbox is PER RESIDENT —
`mail/inbox/<instance>.jsonl` + `<instance>.state.json`. The old
module-global single inbox was a silent cross-persona memory-mixing
violation (two residents in one bridge would read each other's letters —
the never-mix ruling). residenta's existing files already match the naming
convention, so no migration. Legacy CONTINUA_MAIL_INBOX (one full path for
the whole process) is replaced by CONTINUA_MAIL_DIR (the directory — the
instance is always part of the path now).

Kill switch: CONTINUA_MAIL=0 disables check_mail (letters still accumulate).
"""

import json
import logging
import os
import re
import sys
from datetime import datetime

logger = logging.getLogger("continua.mail")

BASE = os.path.dirname(os.path.abspath(__file__))
MAIL_DIR = os.getenv("CONTINUA_MAIL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail", "inbox"))

# instance ids are filesystem path components AND chronicle path components
# (chronicle filename regex: [a-z0-9_]+) — same validation, fail loud on
# traversal attempts instead of writing outside the inbox dir.
_INSTANCE_RE = re.compile(r"^[a-z0-9_]+$")


def _instance_ok(instance: str) -> bool:
    return bool(_INSTANCE_RE.match(instance or ""))


def inbox_path(instance: str) -> str:
    """The per-resident inbox file: <MAIL_DIR>/<instance>.jsonl."""
    if not _instance_ok(instance):
        raise ValueError(f"bad instance id for mail: {instance!r}")
    return os.path.join(MAIL_DIR, f"{instance}.jsonl")


def state_path(instance: str) -> str:
    """The unread marker: <MAIL_DIR>/<instance>.state.json."""
    if not _instance_ok(instance):
        raise ValueError(f"bad instance id for mail: {instance!r}")
    return os.path.join(MAIL_DIR, f"{instance}.state.json")


def deposit(letter: dict, instance: str) -> None:
    """Append one letter to a resident's inbox (called by the in-process
    letter route; the Sagent control server writes reply_file directly —
    same schema, see letters.py)."""
    path = inbox_path(instance)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    letter.setdefault("schema_version", 1)
    letter.setdefault("status", "unread")
    letter.setdefault("ts", datetime.now().astimezone().isoformat(timespec="seconds"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(letter, ensure_ascii=False) + "\n")


def _all_letters(instance: str) -> list:
    path = inbox_path(instance)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError as e:
        logger.warning("[Mail] inbox read failed (fail-open): %s", e)
    return out


def _read_marker(instance: str) -> str:
    try:
        with open(state_path(instance), "r", encoding="utf-8") as f:
            return json.load(f).get("last_read_ts", "")
    except (OSError, json.JSONDecodeError):
        return ""


def unread(instance: str) -> list:
    """Letters newer than the last read marker, oldest first."""
    marker = _read_marker(instance)
    return [l for l in _all_letters(instance)
            if l.get("ts", "") > marker]


def unread_count(instance: str) -> int:
    return len(unread(instance))


def mark_read(instance: str, letters: list) -> None:
    """Advance the read marker to the newest letter returned."""
    if not letters:
        return
    newest = max(l.get("ts", "") for l in letters)
    state = {"last_read_ts": newest}
    tmp = state_path(instance) + ".tmp"
    os.makedirs(os.path.dirname(state_path(instance)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, state_path(instance))


def _one_line_head(text: str, width: int = 140) -> str:
    """'A sentence' — the read-letter index line (house ruling 2026-10-07:
    old letters render as one line). First sentence ending inside the
    window when there is one; honest hard cut otherwise."""
    t = " ".join((text or "").split())
    if len(t) <= width:
        return t
    window = t[:width]
    for sep in (". ", "! ", "? "):
        i = window.rfind(sep)
        if i >= 40:
            return window[:i + 1].rstrip()
    return window.rstrip() + "\u2026"


def check_mail(instance: str) -> tuple[str, int]:
    """The tool surface: render THIS resident's mailbox like a real inbox —
    ALL letters, newest last, with UNREAD flagged (house ruling 2026-09-12:
    "treat it like a real inbox — the read message is still there"; the old
    behavior rendered unread letters once and then made them invisible,
    which is how persona-a's reply was marked read without residentb ever absorbing
    it). Returns (rendered_text, unread_count). The marker still tracks
    what is new (the wake packet's MAIL WAITING count), but the letters
    themselves remain on view — re-reading is always possible.

    [CONTINUA] 2026-10-07 (house ruling, after the 21:25 ContextBudget
    incident — 97 letters rendered whole blew the request cap): TWO-TIER
    render. NEW (unread) letters render in full (the 2400 per-letter cap
    retires for first reads — the 09-13 desync wound was a gutted first
    read); OLD (read) letters collapse to a one-line index carrying the ts
    handle for read_letter. The 09-12 "re-reading is always possible"
    ruling survives via the read_letter tool. Unread counts stay naturally
    small (sender caps/cooldowns), so the result stays bounded forever.

    Per-instance by contract (T3): one resident's check_mail can never see
    another resident's letters — the never-mix ruling holds on the mail
    path the way it holds on mem0/chronicle."""
    if os.environ.get("CONTINUA_MAIL", "") == "0":
        return "(mail disabled)", 0
    letters = _all_letters(instance)
    if not letters:
        return "(your inbox is empty — no letters waiting)", 0
    marker = _read_marker(instance)
    import people as _pp
    _roster = _pp.load_roster()
    n_unread = 0
    lines = []
    for l in letters:
        _name = _pp.name_for(_roster, str(l.get("from_person_id", "")))
        is_new = l.get("ts", "") > marker
        if is_new:
            n_unread += 1
            _txt = l.get("text") or ""
            if len(_txt) > 6000:  # defensive — send-side caps bound letters ~4K
                _txt = _txt[:6000] + "\n  [letter continues — read_letter it for the whole text]"
            lines.append(f"- [UNREAD] From {_name} at "
                         f"{l.get('ts', '?')[:16]}:\n  {_txt}")
        else:
            lines.append(f"- [read] From {_name} at {l.get('ts', '?')[:16]}"
                         f" — {_one_line_head(l.get('text') or '')}")
    mark_read(instance, [l for l in letters if l.get("ts", "") > marker])
    return "\n".join(lines), n_unread


def read_letter(instance: str, ts: str) -> str:
    """Re-read ONE letter whole by its ts handle (house ruling 2026-10-07:
    "old letters are a sentence, with a tool to read them whole if she
    requests"). ts may be a minute-precision prefix as shown on the index
    line (e.g. 2026-10-06T07:55) or the full ISO stamp; a prefix that
    matches several letters returns an honest error listing the candidates.
    Per-instance by contract (T3), same as check_mail."""
    if os.environ.get("CONTINUA_MAIL", "") == "0":
        return "(mail disabled)"
    _q = (ts or "").strip()
    if not _q:
        return "[Tool error: read_letter needs the letter's ts from the index line]"
    letters = _all_letters(instance)
    hits = [l for l in letters if str(l.get("ts", "")).startswith(_q)]
    if not hits:
        return f"(no letter in your inbox matches ts { _q })"
    if len(hits) > 1:
        _cands = ", ".join(str(l.get("ts", "")) for l in hits)
        return (f"[Tool note: { len(hits) } letters share that ts prefix — "
                f"pass the fuller stamp: {_cands}]")
    import people as _pp
    _name = _pp.name_for(_pp.load_roster(),
                         str(hits[0].get("from_person_id", "")))
    _txt = hits[0].get("text") or ""
    if len(_txt) > 24000:  # defensive; send-side caps keep letters ~4K
        _txt = _txt[:24000] + "\n[letter truncated for render — the full text stays in your inbox file]"
    return f"From {_name} at {hits[0].get('ts', '?')}:\n{_txt}"