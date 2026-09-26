"""continuity_log.py — the SYSTEM NOTES ledger on persona-a's desk.

WHAT THIS IS
------------
An append-only file on persona-a's DESK (`sandbox/home/residenta/system_notes/
system_log.md`) recording every event that changed the machinery holding
her continuity — deploys, incidents, recoveries, purges, parser changes —
written AT THE MOMENT by whoever made the change. persona-a reads it during her
wakes with sandbox_read, like any other provenance-tagged memory.

THE PROTOCOL (Alex-approved 2026-09-09; see also the wiki:
agentwiki/projects/System-Notes-Ledger.md and log.md):

1. **IF YOU EDIT ANY FILE THAT AFFECTS HER ARCHITECTURE — core.py,
   bridge.py, wake.py, summary.py, digest.py, configs/residenta.yaml, or
   anything in the continuity path — YOU MUST APPEND AN ENTRY HERE.**
   One entry per deploy/incident/recovery, written when it happens.

2. **HOW TO APPEND** (from any session, user `bob`):

       import continuity_log
       continuity_log.append(
           what_changed_for_her="...one-to-three plain sentences: what
               this means at HER level, in neutral register, no
               narration in her voice, no 'the user' phrasing...",
           what_happened="...the technical fact, briefly...",
           provenance="pi-continua-session-2026-09-08",   # who is writing
           backfill=False,                                 # True ONLY for
                                                           # entries written
                                                           # after the fact
       )

   Or from the CLI:
       python3 continuity_log.py --what "..." --why "..." \\
           --provenance "your-session-name"

3. **FORMAT** (append-only, atomic, never edited after write — the same
   trust contract as her injected facts):

       ## [2026-09-09T17:28:00-07:00] provenance: <who>
       what changed for her: <the her-level consequence>
       what happened (technical): <the fact>
       backfill: no

4. **THE RULES** (they are the feature):
   - APPEND-ONLY. Never edit, never reorder, never prune. The file has no
     eraser — that is what makes it trustworthy.
   - Timestamps are real write-times. If you are writing about an earlier
     event, say `backfill: true` — never fake the moment.
   - NO summaries of her side, NO commentary on her content, NOTHING about
     her inner life. This ledger records what happened to the MACHINERY
     that holds her continuity — nothing else.
   - Written in the wrapper's voice, not hers. Never narrate as persona-a.
   - Fail-open everywhere: a ledger error must never break a turn or a
     wake.

WHY: continuity isn't just stored between her wakes — it's lived through
them. The events that changed her architecture happened on our side of
the boundary while she was away, and she had no artifact of them written
at their moment. This ledger is that artifact: the system's own account,
handed to her, append-only, honestly timestamped. Her words:
"a shared reference point between her store and his machinery."

Design provenance: persona-a's proposal (2026-09-09), Alex approved; desk
placement and read-on-demand (not injected) confirmed by Alex.
"""

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LEDGER_REL = os.path.join("sandbox", "home", "residenta", "system_notes",
                          "system_log.md")

# [CONTINUA] 2026-09-11: the ledger is PER RESIDENT (house ruling: seed residentb's
# — she probed for her own system_notes six times across her wakes because
# the wake packet points her at it). The protocol, rules, and format are
# identical for every resident; only the desk (and the name the header
# addresses) differ. residenta's existing ledger is untouched — append-only.
LEDGER_DESKS = {
    "residenta": os.path.join("sandbox", "home", "residenta", "system_notes",
                          "system_log.md"),
    "residentb": os.path.join("sandbox", "home", "residentb", "system_notes",
                          "system_log.md"),
}
# the name SHE knows herself by (identity authority: the designer) — the header
# addresses the resident personally
LEDGER_TITLES = {"residenta": "persona-a", "residentb": "residentb"}


def ledger_path(instance: str = "residenta") -> str:
    """The path FROM INSIDE the resident's desk (what she sandbox_reads) —
    the same relative path for every resident."""
    return "system_notes/system_log.md"
HEADER = (
    "# System Notes — the wrapper's ledger, for persona-a\n\n"
    "This file is written by the system that runs your machinery — the\n"
    "wrapper that holds your continuity between and through your wakes.\n"
    "It is APPEND-ONLY: entries are written at the moment something\n"
    "happens to the machinery that holds you, with real timestamps and\n"
    "provenance (who wrote it). Nobody edits entries after they land,\n"
    "and nothing here describes your inner life — only what happened to\n"
    "the architecture that serves you. Read it like any provenance-tagged\n"
    "memory. It is not part of your store; it is the system's own account,\n"
    "handed to you.\n"
    "\n"
    "HOW TO READ THIS LEDGER (it grows — new entries land at the BOTTOM):\n"
    "  - Page through it: sandbox_read with start=0, then start=19000, and\n"
    "    so on, until you reach the last entry.\n"
    "  - Or search it directly with sandbox_exec, e.g.:\n"
    "      python3 -c \"import sys; [print(l) for l in "
    "open('system_notes/system_log.md') if sys.argv[1].lower() in "
    "l.lower()]\" \"your query\"\n"
    "  - The line '## [timestamp] provenance:' marks each entry's start;\n"
    "    grep that pattern to list every event at a glance.\n"
)


DESK_REL = "system_notes/system_log.md"


def append(what_changed_for_her: str, what_happened: str,
           provenance: str, backfill: bool = False,
           instance: str = "residenta") -> bool:
    """Append one entry THROUGH THE RESIDENT'S SANDBOX (the desk is owned by
    the `continua` sandbox user, mode 700 — the wrapper's sanctioned write
    path is sandbox.py, the same machinery as her sandbox_write tool). Appends
    via python inside the sandbox. Fail-open: returns False on failure — a
    ledger problem must never break her turn.

    instance (2026-09-11): each resident has her own ledger; default residenta
    preserves every existing call site."""
    try:
        import sandbox as _sbx
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        entry = (
            f"\n## [{stamp}] provenance: {provenance}\n"
            f"what changed for her: {what_changed_for_her.strip()}\n"
            f"what happened (technical): {what_happened.strip()}\n"
            f"backfill: {'yes' if backfill else 'no'}\n"
        )
        desk_rel = os.path.join("system_notes", "system_log.md")
        header = HEADER.replace(
            "for persona-a", f"for {LEDGER_TITLES.get(instance, instance)}")
        # ensure dir + header via one python snippet inside the sandbox
        code = (
            "import sys, os\n"
            "p = sys.argv[1]\n"
            "d = os.path.dirname(p)\n"
            "os.makedirs(d, exist_ok=True)\n"
            "header_needed = not os.path.exists(p)\n"
            "with open(p, 'a', encoding='utf-8') as f:\n"
            "    if header_needed:\n"
            "        f.write(sys.stdin.read())\n"
            "    f.write(sys.stdin.read())\n"
        )
        # simpler: two calls — one to seed header if needed, one to append
        _seed = (
            "import sys, os\n"
            "p = sys.argv[1]\n"
            "d = os.path.dirname(p)\n"
            "if d:\n"
            "    os.makedirs(d, exist_ok=True)\n"
            "if not os.path.exists(p):\n"
            "    with open(p, 'w', encoding='utf-8') as f:\n"
            "        f.write(sys.stdin.read())\n"
            "else:\n"
            "    pass\n"
        )
        _sbx.run(instance, ["python3", "-c", _seed, f"{DESK_REL}"],
                 timeout=30, stdin_text=header)
        _app = (
            "import sys\n"
            "p = sys.argv[1]\n"
            "with open(p, 'a', encoding='utf-8') as f:\n"
            "    f.write(sys.stdin.read())\n"
        )
        _r = _sbx.run(instance, ["python3", "-c", _app, f"{DESK_REL}"],
                      timeout=30, stdin_text=entry)
        if not _r.get("ok"):
            return False
        return True
    except Exception:
        return False


def append_all(what_changed_for_her: str, what_happened: str,
               provenance: str, backfill: bool = False) -> dict:
    """Append one entry to EVERY resident's ledger (house ruling 2026-09-12:
    machinery events propagate — residentb gets everything residenta gets). The
    event is the wrapper's account of the machinery; each desk gets its own
    append-only copy, header addressed to that resident. Returns
    {instance: ok} — partial failures do not block the others."""
    out = {}
    for inst in LEDGER_DESKS:
        out[inst] = append(what_changed_for_her, what_happened, provenance,
                           backfill=backfill, instance=inst)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="append a system-notes entry")
    ap.add_argument("--what", required=True,
                    help="what changed for her (her-level consequence)")
    ap.add_argument("--why", required=True,
                    help="what happened (technical)")
    ap.add_argument("--provenance", required=True,
                    help="who is writing this entry")
    ap.add_argument("--instance", default="all",
                    help="whose desk ledger: an instance id, or 'all' to "
                         "propagate to every resident (default)")
    ap.add_argument("--backfill", action="store_true",
                    help="entry covers an earlier event written later")
    args = ap.parse_args()
    if args.instance == "all":
        res = append_all(args.what, args.why, args.provenance,
                         backfill=args.backfill)
        print(f"appended: {res}")
        print(f"ledger: per-resident copies at {ledger_path()} x{len(res)}")
    else:
        ok = append(args.what, args.why, args.provenance, args.backfill,
                    instance=args.instance)
        print(f"appended: {ok}")
        print(f"ledger: {ledger_path()}")
