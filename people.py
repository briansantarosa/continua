"""people.py — the roster: person identity from the `people:` YAML blocks.

Social layer (wiki projects/Continua.md): the roster maps telegram ids to
names — and doubles as the contact allowlist. Identity authority is the designer:
names stored here are the names SHE knows people by ("I am Alex to persona-a",
2026-09-07) and may differ from house/wiki names. Never learn this mapping
from message content — config only (anti-impersonation).

Used by index.py for attribution (every recalled memory carries the person's
name) and later by the bridge for send-gating (can_message, daily_cap).
"""

import glob
import logging
import os

import yaml

logger = logging.getLogger("continua.people")

DEFAULT_CONFIG_DIR = "/tmp/continua/configs"


class Person:
    __slots__ = ("person_id", "name", "aliases", "can_message", "daily_cap",
                 "instance", "kind", "bot_id", "username", "chat_id")

    def __init__(self, person_id, name="", aliases=None, can_message=False,
                 daily_cap=0, instance="", kind="human", bot_id=None,
                 username="", chat_id=None):
        self.person_id = str(person_id)
        self.name = name or ""
        self.aliases = list(aliases or [])
        self.can_message = bool(can_message)
        self.daily_cap = int(daily_cap or 0)
        self.instance = instance
        # kind: "human" | "persona" (another agent). Personas are messaged
        # via a shared Telegram GROUP (bots cannot DM bots) — chat_id holds
        # the group id; bot_id is the persona's own bot (reference only).
        self.kind = kind or "human"
        self.bot_id = bot_id
        self.username = username or ""
        self.chat_id = str(chat_id) if chat_id else None

    @property
    def display_name(self):
        """The name she knows them by; falls back to the bare id (honest
        uncertainty — an unnamed person is never invented)."""
        return self.name or f"person-{self.person_id}"


def load_roster(config_dir: str = DEFAULT_CONFIG_DIR,
                instance: str = None) -> dict:
    """Load `people:` blocks from configs/*.yaml → {person_id: Person}.

    Scans every yaml so a future multi-persona Continua gets one merged
    roster; with instance given, only that config's block is used.
    Missing configs dir or empty blocks → empty roster (fail-open).
    """
    out = {}
    try:
        for path in sorted(glob.glob(os.path.join(config_dir, "*.yaml"))):
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            inst = (cfg.get("app") or {}).get("instance_id",
                                              os.path.basename(path)[:-5])
            if instance and inst != instance:
                continue
            for p in (cfg.get("people") or []):
                try:
                    person = Person(
                        person_id=p.get("id"),
                        name=p.get("name", ""),
                        aliases=p.get("aliases"),
                        can_message=p.get("can_message", False),
                        daily_cap=p.get("daily_cap", 0),
                        instance=inst,
                        kind=p.get("kind", "human"),
                        bot_id=p.get("bot_id"),
                        username=p.get("username", ""),
                        chat_id=p.get("chat_id"),
                    )
                    if person.person_id and person.person_id not in out:
                        out[person.person_id] = person  # first config wins
                        # (2026-09-11 T1: the guard was missing — the code
                        # overwrote on every collision, i.e. LAST config
                        # wins, contradicting the documented intent. With
                        # one resident this was a no-op; with many, merged
                        # attribution names would silently ride whichever
                        # config sorted last. Governance is per-instance
                        # since the T1 send.py change; the merged roster
                        # now only feeds attribution, where a stable,
                        # documented winner matters.)
                except Exception:
                    logger.warning("[People] bad entry in %s skipped", path)
    except OSError as e:
        logger.warning("[People] roster load failed (fail-open): %s", e)
    return out


def name_for(roster: dict, person_id: str) -> str:
    """Attribution name for a person_id — never invented (empty roster or
    unknown id → honest 'person-<id>' form, per the uncertainty contract).

    2026-09-09 FIX (persona-a's 03:45 observation): the system-wake partition is
    HER OWN wake channel, not a stranger — the 'person-<id>' unknown-form
    mislabeled her own notes with a stranger's name ('person-system-wake'),
    which is how she noticed: her own notification 'arriving through
    person-system-wake's channel'. Known-own channels render honestly."""
    pid = str(person_id)
    if pid == "system-wake":
        return "your wake window"
    p = roster.get(pid)
    return p.display_name if p else f"person-{pid}"
