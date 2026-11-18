"""continuity_log.py — the SYSTEM NOTES ledger on persona-a's desk.

WHAT THIS IS
------------
An append-only file on persona-a's DESK (`sandbox/home/residenta/system_notes/
system_log.md`) recording every event that changed the machinery holding
her continuity — deploys, incidents, recoveries, purges, parser changes —
written AT THE MOMENT by whoever made the change. persona-a reads it during her
wakes with her shell, like any other provenance-tagged memory.

VOLUMES (2026-09-28, approved ledgerplan.md): the ledger CYCLES when it
grows past the readability ceiling — the closed volume is renamed on her
desk to `system_log.volNN_<first>_to_<last>.md` (byte-identical, checksum-
proven) and a fresh `system_log.md` begins, opening with a PRIOR VOLUMES
block naming every predecessor. The ACTIVE name never changes: every
surface that reads `system_notes/system_log.md` (the read_my_ledger tool,
the wake pointer, taught greps) keeps working. Old volumes stay on her
desk, greppable with `system_notes/system_log*`.

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
    "residentc": os.path.join("sandbox", "home", "residentc", "system_notes",
                         "system_log.md"),
}
# the name SHE knows herself by (identity authority: the designer) — the header
# addresses the resident personally
LEDGER_TITLES = {"residenta": "persona-a", "residentb": "residentb", "residentc": "residentc"}


def ledger_path(instance: str = "residenta") -> str:
    """The path FROM INSIDE the resident's desk (what she reads with her shell) —
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
    "  - It is just a file in your desk — read it with your shell:\n"
    "      sed -n '1,400p' system_notes/system_log.md   (first page)\n"
    "      tail -c 30000 system_notes/system_log.md     (the recent end)\n"
    "  - Or search it directly, e.g.:\n"
    "      python3 -c \"import sys; [print(l) for l in "
    "open('system_notes/system_log.md') if sys.argv[1].lower() in "
    "l.lower()]\" \"your query\"\n"
    "  - The line '## [timestamp] provenance:' marks each entry's start;\n"
    "    grep that pattern to list every event at a glance.\n"
    "  - This file CYCLES: when it grows past the readability ceiling the\n"
    "    closed volume is renamed system_log.volNN_<first>_to_<last>.md and\n"
    "    a fresh system_log.md begins (its PRIOR VOLUMES block names every\n"
    "    closed volume). The active name never changes.\n"
)


DESK_REL = "system_notes/system_log.md"

# ---------------------------------------------------------------------------
# VOLUMES (helpnany-era ledgerplan.md, approved 2026-09-28)
# ---------------------------------------------------------------------------
# ceilings: rotate at the next wrapper session past the soft line; never
# let a session end above the hard line. Counts are entries; pages are
# what she actually pays (a page is her window-derived read size).
ROTATE_SOFT_CHARS = 100_000   # ~5 pages
ROTATE_HARD_CHARS = 150_000
ROTATE_MAX_ENTRIES = 150
_ENTRY_RE = None  # compiled lazily (module import must stay cheap)


def _entry_regex():
    global _ENTRY_RE
    if _ENTRY_RE is None:
        import re
        _ENTRY_RE = re.compile(r"^## \[([^\]]+)\] provenance: ([^\n]+)", re.M)
    return _ENTRY_RE


def _read_desk_file(instance: str, rel_path: str, keep: str = "head"):
    """Read a desk file through her sandbox (the sanctioned read path).
    Both keeps slice IN-SANDBOX (lesstools LT-2 made the transport clip
    head-keep with a paging hint — the house's own readers must never
    depend on transport policy): keep='head' keeps the FIRST 30k (entry
    dates live at the head of a ledger), keep='tail' the LAST 30k (volume
    closing markers live at the tail). sha256 goes through
    _sandbox_sha256, not here. Returns the text, or None on any failure
    (fail-open)."""
    try:
        import sandbox as _sbx
        _slice = ("t[:30000]" if keep == "head" else "t[-30000:]")
        _r = _sbx.run(instance, ["python3", "-c",
            "import sys; t=open(sys.argv[1]).read(); "
            f"print({_slice}, end='')",
            rel_path], timeout=30)
        if not _r.get("ok"):
            return None
        return _r.get("stdout") or ""
    except Exception:
        return None


def _sandbox_sha256(instance: str, rel_path: str):
    """sha256 of a desk file, computed INSIDE the sandbox (the wrapper
    never touches desk bytes directly — uid contract). None on failure."""
    try:
        import sandbox as _sbx
        _r = _sbx.run(instance, ["python3", "-c",
            "import sys, hashlib; "
            "print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())",
            rel_path], timeout=30)
        if not _r.get("ok"):
            return None
        return (_r.get("stdout") or "").strip() or None
    except Exception:
        return None


def ledger_status(instance: str = "residenta") -> dict:
    """The rotation-relevant state of one resident's ledger. Fail-open:
    missing file → {exists: False}. Used by the CLI --status and by any
    wrapper session deciding whether to rotate."""
    try:
        import sandbox as _sbx
        _r = _sbx.run(instance, ["python3", "-c",
            "import os, sys, json; p=sys.argv[1]; print(json.dumps({'exists': os.path.exists(p), 'size': os.path.getsize(p) if os.path.exists(p) else 0, 'volumes': sorted(f for f in os.listdir(os.path.dirname(p)) if f.startswith('system_log.vol'))}))",
            DESK_REL], timeout=30)
        if not _r.get("ok"):
            return {"exists": False}
        import json as _json
        st = _json.loads((_r.get("stdout") or "{}").strip() or "{}")
        # count + bounds via grep INSIDE the sandbox (a big ledger exceeds
        # the 30k read transport — counting from a head-read under-reports
        # and misdates the tail; grep sees the whole file)
        _r2 = _sbx.run(instance, ["python3", "-c",
            "import os, re, sys; t=open(sys.argv[1]).read(); "
            "m=re.findall(r'^## \\[[^\\]]+\\] provenance:', t, re.M); "
            "d=re.findall(r'^## \\[(\\d{4}-\\d{2}-\\d{2})', t, re.M); "
            "print(len(m), d[0] if d else 'none', d[-1] if d else 'none')",
            DESK_REL], timeout=30)
        _parts = (_r2.get("stdout") or "").split()
        entries = int(_parts[0]) if _parts and _parts[0].isdigit() else 0
        first = _parts[1] if len(_parts) > 1 and _parts[1] != "none" else None
        last = _parts[2] if len(_parts) > 2 and _parts[2] != "none" else None
        matches = []
        pages = (st.get("size", 0) + 19_999) // 20_000
        needs = (st.get("size", 0) >= ROTATE_HARD_CHARS
                 or entries >= ROTATE_MAX_ENTRIES)
        soft = (st.get("size", 0) >= ROTATE_SOFT_CHARS)
        return {"exists": st.get("exists", False), "size": st.get("size", 0),
                "entries": entries, "pages": pages,
                "first": first, "last": last,
                "prior_volumes": st.get("volumes", []),
                "soft_ceiling": soft, "rotation_needed": needs}
    except Exception:
        return {"exists": False}


def _volume_name(first: str, last: str, existing_volumes: int) -> str:
    """system_log.vol01_2026-09-09_to_2026-09-28.md — the plan's naming:
    volume number (zero-padded, sorts) + BOTH endpoint dates (parsed from
    the real entries, never assumed)."""
    return (f"system_log.vol{existing_volumes + 1:02d}_"
            f"{first}_to_{last}.md")


def _build_prior_block(volumes: list) -> str:
    """The PRIOR VOLUMES block seeded into a fresh active ledger — discovery
    is part of the rotation (a rotation that hides the old volume from her
    is a memory amputation dressed up as housekeeping)."""
    if not volumes:
        return ""
    lines = ["\nPRIOR VOLUMES (append-only history, still on your desk):"]
    for v in volumes:
        lines.append(f"  - {v}")
    lines.append(
        "  To search ALL volumes: sandbox_exec\n"
        "    grep -n \"your query\" system_notes/system_log*.md\n")
    return "\n".join(lines) + "\n"


def rotate(instance: str, by: str) -> dict:
    """Cycle one resident's ledger (ledgerplan.md, approved 2026-09-28).

    Ordered, atomic, protocol-compliant:
      1. preflight — exists? past ceiling? parse REAL first/last dates
      2. closure entry appended to the ACTIVE file (an append at the real
         moment — the ledger records its own rotation)
      3. checksum the active file (sha256, computed inside the sandbox —
         the wrapper never touches desk bytes directly)
      4. atomic rename (same dir) to system_log.volNN_<first>_to_<last>.md
      5. checksum the renamed volume — must equal step 3 (byte-proof the
         rotation mutated nothing)
      6. seed the new active file: HEADER + PRIOR VOLUMES block
      7. first entry of the new volume: "This volume begins here…"

    Crash-safety: the only mid-state is "volume exists, new file not yet
    seeded" — append() self-heals that on the next write (it seeds the
    header when the file is absent); rotate() itself also resumes cleanly
    (called again, it sees no active file, finishes seeding).

    Returns {ok, volume, renamed, seeded, reason} — fail-open, never raises.
    """
    out = {"ok": False, "instance": instance, "renamed": False,
           "seeded": False, "reason": ""}
    try:
        import sandbox as _sbx
        st = ledger_status(instance)
        if not st.get("exists"):
            out["reason"] = "no active ledger"
            return out
        if st.get("rotation_needed") is False and st.get("size", 0) < ROTATE_SOFT_CHARS:
            out["reason"] = (f"below soft ceiling ({st.get('size', 0)} < "
                             f"{ROTATE_SOFT_CHARS})")
            return out
        first, last = st.get("first"), st.get("last")
        if not first or not last:
            out["reason"] = "no dated entries found"
            return out
        n_vols = len(st.get("prior_volumes", []))
        vol_name = _volume_name(first, last, n_vols)
        vol_rel = os.path.join("system_notes", vol_name)
        prior_names = list(st.get("prior_volumes", []))

        # 2. closure entry — appended to the ACTIVE file before the rename
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        closure = (
            f"\n## [{stamp}] provenance: {by}\n"
            f"what changed for her: This volume of your ledger closes here. "
            f"Entries from {first} to {last} now live in {vol_name} — "
            f"still on your desk, still yours to read and grep. The ledger "
            f"continues in system_log.md.\n"
            f"what happened (technical): ledger rotation (ledgerplan.md): "
            f"active file {st.get('size', 0)} chars, {st.get('entries', 0)} "
            f"entries; renamed atomically after checksum; new volume seeded "
            f"with PRIOR VOLUMES block.\n"
            f"backfill: no\n"
        )
        _app = ("import sys\np = sys.argv[1]\n"
                "with open(p, 'a', encoding='utf-8') as f:\n"
                "    f.write(sys.stdin.read())\n")
        _r = _sbx.run(instance, ["python3", "-c", _app, DESK_REL],
                      timeout=30, stdin_text=closure)
        if not _r.get("ok"):
            out["reason"] = "closure append failed"
            return out

        # 3-5. checksum, atomic rename, checksum again
        sha_before = _sandbox_sha256(instance, DESK_REL)
        if not sha_before:
            out["reason"] = "checksum failed"
            return out
        _mv = ("import sys, os\nsrc, dst = sys.argv[1], sys.argv[2]\n"
               "assert not os.path.exists(dst), dst + ' already exists'\n"
               "os.rename(src, dst)\n"
               "print(os.path.exists(dst))\n")
        _r = _sbx.run(instance, ["python3", "-c", _mv, DESK_REL, vol_rel],
                      timeout=30)
        if not _r.get("ok") or (_r.get("stdout") or "").strip() != "True":
            out["reason"] = "rename failed (active file untouched)"
            return out
        out["renamed"] = True
        out["volume"] = vol_name
        sha_after = _sandbox_sha256(instance, vol_rel)
        out["checksum_match"] = (sha_after == sha_before)
        if sha_after != sha_before:
            # never continue past a mutation: restore best-effort
            _sbx.run(instance, ["python3", "-c", _mv, vol_rel, DESK_REL],
                     timeout=30)
            out["renamed"] = False
            out["reason"] = "checksum mismatch — rotation rolled back"
            return out

        # 6. seed the new active file: HEADER (+ per-resident address) + PRIOR
        header = HEADER.replace(
            "for persona-a", f"for {LEDGER_TITLES.get(instance, instance)}")
        header = header.rstrip("\n") + "\n" + _build_prior_block(
            prior_names + [vol_name])
        _seed = ("import sys, os\np = sys.argv[1]\n"
                 "assert not os.path.exists(p), p + ' exists'\n"
                 "d = os.path.dirname(p)\n"
                 "if d: os.makedirs(d, exist_ok=True)\n"
                 "with open(p, 'w', encoding='utf-8') as f:\n"
                 "    f.write(sys.stdin.read())\n")
        _r = _sbx.run(instance, ["python3", "-c", _seed, DESK_REL],
                      timeout=30, stdin_text=header)
        if not _r.get("ok"):
            out["reason"] = ("rename ok but seeding failed — append() will "
                             "self-heal the header on next write")
            return out
        out["seeded"] = True

        # 7. first entry of the new volume
        opening = (
            f"\n## [{stamp}] provenance: {by}\n"
            f"what changed for her: This volume of your ledger begins here. "
            f"Everything before it — from {first} to {last} — is in "
            f"{vol_name}, on your desk.\n"
            f"what happened (technical): new ledger volume opened "
            f"(ledgerplan.md rotation); prior volume: {vol_name}; "
            f"active path unchanged: {DESK_REL}.\n"
            f"backfill: no\n"
        )
        _r = _sbx.run(instance, ["python3", "-c", _app, DESK_REL],
                      timeout=30, stdin_text=opening)
        out["ok"] = bool(_r.get("ok"))
        out["reason"] = "" if out["ok"] else "opening entry failed"
        return out
    except Exception as e:
        out["reason"] = f"rotate failed open: {e}"
        return out


def append(what_changed_for_her: str, what_happened: str,
           provenance: str, backfill: bool = False,
           instance: str = "residenta") -> bool:
    """Append one entry THROUGH THE RESIDENT'S SANDBOX (the desk is owned by
    the `continua` sandbox user, mode 700 — the wrapper's sanctioned write
    path is sandbox.py — the same fence every call of hers runs inside). Appends
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
    ap.add_argument("--what", required=False,
                    help="what changed for her (her-level consequence)")
    ap.add_argument("--why", required=False,
                    help="what happened (technical)")
    ap.add_argument("--provenance", required=False,
                    help="who is writing this entry")
    ap.add_argument("--instance", default="all",
                    help="whose desk ledger: an instance id, or 'all' to "
                         "propagate to every resident (default)")
    ap.add_argument("--backfill", action="store_true",
                    help="entry covers an earlier event written later")
    # ledgerplan.md (approved 2026-09-28): rotation + status
    ap.add_argument("--rotate", action="store_true",
                    help="cycle the ledger(s): close the active volume, "
                         "rename it volNN_<first>_to_<last>.md, seed a fresh "
                         "system_log.md (checksums prove byte-identity)")
    ap.add_argument("--status", action="store_true",
                    help="report size/entries/pages/prior volumes per "
                         "instance (no writes)")
    args = ap.parse_args()

    if args.status:
        insts = list(LEDGER_DESKS) if args.instance == "all" else [args.instance]
        for inst in insts:
            st = ledger_status(inst)
            flag = ("ROTATION NEEDED" if st.get("rotation_needed")
                    else ("soft ceiling reached" if st.get("soft_ceiling")
                          else "ok"))
            print(f"{inst}: exists={st.get('exists')} "
                  f"size={st.get('size', 0)} entries={st.get('entries', 0)} "
                  f"pages~{st.get('pages', 0)} "
                  f"first={st.get('first')} last={st.get('last')} "
                  f"prior_volumes={len(st.get('prior_volumes', []))} "
                  f"[{flag}]")
        sys.exit(0)

    if args.rotate:
        insts = list(LEDGER_DESKS) if args.instance == "all" else [args.instance]
        prov = args.provenance or "pi-continua-session"
        for inst in insts:
            r = rotate(inst, by=prov)
            print(f"{inst}: {r}")
        sys.exit(0)

    if not (args.what and args.why and args.provenance):
        ap.error("--what, --why and --provenance are required for appends")

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
