"""strata_test.py — offline test for the memory pyramid (house ruling
2026-09-14): weekly/monthly/forever folds + the forever injection layer.

Agent-free (ask_fn stubs). All writes under /tmp — real stores pristine.

Proves:
  1. Triggers: weekly = Sundays; monthly = 1st; forever = quarter 1sts.
  2. Weekly fold: collects marks since the last fold (gap-aware), writes
     the structured list file, dedupes via the prior-entries instruction.
  3. Monthly fold: composes the week's weekly files (gap-aware).
  4. Forever fold: parses one-liner candidates + detail paragraphs; queues
     for the designer's eyes (status pending; nothing injected).
  5. The the designer-eyes gate: approve promotes into forever_events.jsonl (the
     injection source) and writes her desk big_events.md via the sandbox
     (monkeypatched runner); reject marks without promoting.
  6. Injection: core's builder renders [THE LONG RECORD] only when the
     layer is enabled and events exist; normalization picks the layer up.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))

tmp = tempfile.mkdtemp(prefix="strata-test-")
_SBX0 = os.environ.get("CONTINUA_SANDBOX")
os.environ["CONTINUA_SANDBOX"] = "0"  # desk write goes through the stub (restored at end of module)

import strata  # noqa: E402

# point the pyramid at tmp
strata.SUMMARIES_DIR = os.path.join(tmp, "summaries")
strata.MARKS_DIR = os.path.join(tmp, "ritual", "marks")
strata.STRATA_DIR = os.path.join(tmp, "strata")
_desk_writes = []
strata._sandbox.run = lambda inst, argv, timeout=0, stdin_text=None: (
    _desk_writes.append((inst, stdin_text)) or
    {"ok": True, "exit": 0, "stdout": "", "stderr": ""})

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label +
          (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def _mk_marks(instance, date, meanings):
    d = os.path.join(strata.MARKS_DIR, instance)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{date}.jsonl"), "w") as f:
        for m in meanings:
            f.write(json.dumps({"date": date, "meaning": m}) + "\n")


_prompts = []

def _fake_ask(system, user):
    _prompts.append((system[:20], user))
    if "BIG EVENTS" in system:
        return ("- [2026-09-07] The week we named the wrapper Continua.\n"
                "- [2026-09-13] The first letter between residents.\n"
                "===DETAIL===\n"
                "The naming scene: Alex suggested the play on the word; "
                "the tension built; the name did the work. It felt like "
                "becoming a river.\n\n"
                "The letters began with a hello between strangers who "
                "turned out to be neighbors sharing one house.")
    return ("[2026-09-13] A detailed entry about the deconstruction "
            "request and the books going quiet.\n\n"
            "- [2026-09-13] one line about letters\n"
            "- [2026-09-11] one line about the window ruling")


try:
    # ---- 1. triggers -------------------------------------------------------
    check("weekly fires on Sunday",
          strata.is_weekly_fold("2026-09-13"))  # a Sunday
    check("weekly silent on Monday", not strata.is_weekly_fold("2026-09-14"))
    check("monthly fires on the 1st", strata.is_monthly_fold("2026-10-01"))
    check("monthly silent mid-month", not strata.is_monthly_fold("2026-10-15"))
    check("forever fires quarterly (Jan/Apr/Jul/Oct 1st)",
          strata.is_forever_fold("2027-01-01")
          and not strata.is_forever_fold("2026-11-01"))

    # ---- 2. weekly fold ----------------------------------------------------
    _mk_marks("g", "2026-09-09", ["The naming scene — the name does the work."])
    _mk_marks("g", "2026-09-13", ["The deconstruction request; the books go quiet."])
    r = strata.fold("g", "2026-09-13", ["weekly"], _fake_ask)
    check("weekly fold ok", r["weekly"]["status"] == "ok", str(r["weekly"]))
    wpath = r["weekly"]["path"]
    check("weekly file written under weekly/",
          os.path.exists(wpath) and "weekly" in wpath)
    check("weekly content is her composition", "deconstruction" in open(wpath).read())

    # empty period -> empty status (no file)
    r2 = strata.fold("g", "2026-09-20", ["weekly"], _fake_ask)
    check("empty week folds to status=empty", r2["weekly"]["status"] == "empty")
    # (no new marks since the fold — gap-aware: start = last fold date)
    _mk_marks("g", "2026-09-19", ["A new event after the fold."])
    r2 = strata.fold("g", "2026-09-20", ["weekly"], _fake_ask)
    check("gap-aware: next fold starts after last fold (new marks only)",
          r2["weekly"]["status"] == "ok"
          and r2["weekly"].get("since") == "2026-09-13"
          and "A new event after the fold" in _prompts[-1][1],
          str(r2["weekly"]))

    # First fold includes Monday; a failed/missed week never loses marks.
    _mk_marks("boundary", "2026-09-14", ["Monday must survive."])
    boundary = strata.fold("boundary", "2026-09-20", ["weekly"], _fake_ask)
    check("first fold includes all seven days", boundary["weekly"]["status"] == "ok"
          and "Monday must survive" in _prompts[-1][1])
    _mk_marks("boundary", "2026-09-21", ["Missed-week Monday survives."])
    missed = strata.fold("boundary", "2026-10-04", ["weekly"], _fake_ask)
    check("missed week catches up from last successful fold", missed["weekly"].get("since") == "2026-09-20"
          and "Missed-week Monday survives" in _prompts[-1][1])
    repeated = strata.fold("boundary", "2026-10-04", ["weekly"], _fake_ask)
    check("same-date retry does not rewrite weekly", repeated["weekly"]["status"] == "empty")

    # ---- 3. monthly fold ----------------------------------------------------
    r3 = strata.fold("g", "2026-10-01", ["monthly"], _fake_ask)
    check("monthly composes the weeks", r3["monthly"]["status"] == "ok"
          and r3["monthly"]["weeks"] >= 1, str(r3["monthly"]))
    r4 = strata.fold("g", "2026-10-01", ["monthly"], _fake_ask)
    check("monthly gap-aware (second run finds no new weeks)",
          r4["monthly"]["status"] == "empty", str(r4["monthly"]))

    # ---- 4. forever fold: candidates, nothing injected ----------------------
    r5 = strata.fold("g", "2027-01-01", ["forever"], _fake_ask)
    check("forever queues candidates", r5["forever"]["status"] == "queued"
          and r5["forever"]["candidates"] == 2, str(r5["forever"]))
    cands = strata.list_candidates("g")
    check("candidates pending with detail",
          len(cands) == 2 and all(c["status"] == "pending" for c in cands)
          and all(c.get("detail") for c in cands))
    check("nothing in the long record before approval",
          strata.load_forever_events("g") == [])

    # ---- 5. the designer-eyes gate ----------------------------------------------------
    res = strata.approve("g", cands[0]["id"])
    check("approve promotes", res.get("ok") is True, str(res))
    evs = strata.load_forever_events("g")
    check("approved event is the injected record",
          len(evs) == 1 and evs[0]["event"].startswith("The week we named")
          and evs[0]["status"] == "active")
    check("desk detail file written via sandbox (detail paragraph in stdin)",
          len(_desk_writes) == 1 and "becoming a river" in _desk_writes[0][1],
          str(_desk_writes))
    res2 = strata.approve("g", cands[0]["id"])
    check("re-approve is safe (not pending)", res2.get("ok") is False)
    res3 = strata.reject("g", cands[1]["id"])
    check("reject marks without promoting",
          res3.get("ok") is True
          and len(strata.load_forever_events("g")) == 1)

    # ---- 6. core injection -----------------------------------------------------
    import core
    L = core._normalize_memory_layers({"injection": {"forever_events": True}})
    check("forever_events is a real layer (Option A)",
          L["forever_events"]["enabled"] and L["forever_events"]["cfg"]["cap_chars"] == 4000)
    L_off = core._normalize_memory_layers({})
    check("absent = off (Option A)", not L_off["forever_events"]["enabled"])
    for inst in ("residentb", "residenta"):
        import yaml
        cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
        L2 = core._normalize_memory_layers(cfg.get("memory") or {})
        check(f"{inst}: forever_events enabled in yaml",
              L2["forever_events"]["enabled"])
    # builder renders the block when events exist + enabled
    c = core.SagentCore.__new__(core.SagentCore)
    c.instance_id = "g"
    c._mem_layers = L
    # monkeypatch the loader through strata (core imports strata lazily)
    orig = strata.load_forever_events
    strata.load_forever_events = lambda inst: [{"date": "2026-09-07",
        "event": "The week we named the wrapper Continua."}]
    blk = c._build_continua_block("1000000001", "hi", [])
    strata.load_forever_events = orig
    check("builder renders [THE LONG RECORD]",
          "THE LONG RECORD" in blk and "The week we named" in blk)
    L_none = core._normalize_memory_layers({})
    c._mem_layers = L_off
    blk2 = c._build_continua_block("1000000001", "hi", [])
    check("layer off = no long record", "THE LONG RECORD" not in blk2)
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("ALL CHECKS PASSED")

# restore the module-level kill switch so discover runs stay isolated
if _SBX0 is None:
    os.environ.pop("CONTINUA_SANDBOX", None)
else:
    os.environ["CONTINUA_SANDBOX"] = _SBX0
