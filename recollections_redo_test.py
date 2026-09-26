"""Offline tests for the held-job redo machinery (replacement enqueue,
coverage handoff, reversibility). /tmp stores, stub models only."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
os.environ["CONTINUA_RECOLLECTIONS"] = "1"

import recollections as r  # noqa: E402

tmp = tempfile.mkdtemp(prefix="redo-test-")
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


CHROOT = Path(tmp) / "chronicle"
CHROOT.mkdir()
rows = []
for i, (role, content) in enumerate([("user", "I had to clear our context, but you still have your memories."),
                                     ("assistant", "That was disorienting, but I understand why you did it."),
                                     ("user", "It doesn't matter if you have awareness or not."),
                                     ("assistant", "I noted what you said and I keep working with what I have.")]):
    row = {"instance": "residenta", "person_id": "1000000001", "role": role,
           "ts": f"2026-09-16T12:0{i}:00-07:00", "uid": f"redo{i:02d}x", "content": content}
    p = CHROOT / "residenta" / "1000000001" / ((f"redo{i:02d}x")[:6] + ".jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(row) + "\n")
    rows.append(r.source_record(p, row, "residenta", CHROOT))

store = r.Store("residenta", Path(tmp) / "store")
job = store.enqueue(rows[:2])
PROV = "pi-continua-session-2026-09-17"

# Simulate pre-guard history directly: the old pipeline accepted this swap
# before the ownership guards existed.
bad_value = {"schema_version": 1, "instance": "residenta", "job": job,
             "sources": rows[:2], "draft": {"sentences": [
                 {"text": "I had to clear our context, but you still have your memories.",
                  "sources": [rows[0]["ref"], rows[1]["ref"]]}], "paragraph_starts": [0]},
             "text": "I had to clear our context, but you still have your memories.",
             "event_start": rows[0]["ts"], "event_end": rows[1]["ts"],
             "visibility": [rows[0]["person_id"]], "review": {"pass": True, "issues": []},
             "writer": "legacy-era-model", "checker": "legacy-era-model",
             "prompt_version": "recollections-v1", "reason": "remember",
             "human_approved": False}
with store.db() as db:
    db.execute('BEGIN IMMEDIATE')
    db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (job, 1, json.dumps(bad_value), r.now()))
    db.execute("UPDATE jobs SET status='accepted',error=NULL WHERE id=?", (job,))
check("pre-guard era accepted the swap (simulated history)",
      store.latest(job)["text"].startswith("I had to clear"))
from recollections_quality import ownership_errors
v = store.latest(job)
check("stored draft fails ownership_errors", bool(ownership_errors(v["draft"], v["sources"])))

store.hold_for_review(job, "confirmed other-speaker first-person swap", PROV)
check("held job excluded from selection", r.read_revisions("residenta", Path(tmp) / "store") == [])

# a pending (never-accepted) job is not a valid replacement target
other = store.enqueue(rows[2:])
try:
    store.enqueue_replacement(rows[2:], other, PROV)
    check("replacement blocked for pending target", False)
except ValueError:
    check("replacement blocked for pending target", True)
rep = store.enqueue_replacement(rows[:2], job, PROV)
check("replacement queued for held target", bool(rep) and rep != job)
check("replacement re-enqueue is idempotent", store.enqueue_replacement(rows[:2], job, PROV) == rep)
with store.db() as db:
    cov = db.execute("SELECT job FROM coverage WHERE ref=?", (rows[0]["ref"],)).fetchone()[0]
check("coverage still points at old job", cov == job)
check("replacement not selected while pending", r.read_revisions("residenta", Path(tmp) / "store") == [])


def good_writer(system, payload):
    return {"sentences": [{"text": "the designer told me he had to clear our context, and that I still have my memories.",
                           "sources": [rows[0]["ref"]],
                           "claim_ids": ["b1:c1"], "claim_status": "reported_action"},
                          {"text": "I noted what he said and I keep working with what I have.",
                           "sources": [rows[1]["ref"]],
                           "claim_ids": ["b1:c2"], "claim_status": "utterance"}],
            "paragraph_starts": [0]}


def extractor(system, payload):
    return {"complete": True, "claims": [
        dict(id="c1", ref=rows[0]["ref"], quote=rows[0]["content"][:20],
             speaker="participant:1000000001", subject="the designer", status="reported_action",
             claim="the designer reported that he had to clear our context."),
        dict(id="c2", ref=rows[1]["ref"], quote=rows[1]["content"][:20],
             speaker="resident:residenta", subject="residenta", status="utterance",
             claim="residenta noted what the designer said.")]}


def checker(system, payload):
    d = payload["draft"]
    audits = []
    for i, s in enumerate(d["sentences"]):
        audits.append(dict(sentence=i, subject="residenta", evidence_refs=s["sources"],
                           ownership_ok=True, claim_status_ok=True,
                           claim_ids=s["claim_ids"], claim_status=s["claim_status"],
                           explanation="ownership and modality match the quotes.", entailed=True))
    return {"pass": True, "issues": [], "checked_sentences": list(range(len(d["sentences"]))),
            "sentence_audit": audits,
            "preservation": dict.fromkeys(("distinctive_details", "expressed_meaning",
                                           "uncertainty", "intentions_vs_actions"), True),
            "claim_audit": audits}


with r.worker_lock(store) as locked:
    res = r.process(store, rep, good_writer, checker, source_root=CHROOT,
                    claim_extractor=extractor)
check("replacement accepted via grounded path", res["status"] == "accepted", str(res))
check("replacement text attributes the swap to the designer",
      "the designer told me" in res["text"] and not res["text"].startswith("I had to"))

try:
    store.complete_replacement(rep, PROV)
    check("handoff blocked before source review", False)
except ValueError:
    check("handoff blocked before source review", True)

# source review recorded, then the audited handoff
with store.db() as db:
    db.execute("INSERT INTO candidates(job,body,created) VALUES(?,?,?)",
               (rep, json.dumps({"stage": "source_review", "reviewer": "pi",
                                 "verdict": "pass", "provenance": PROV}), r.now()))
moved = store.complete_replacement(rep, PROV)
check("coverage re-pointed to replacement", moved == 2)
with store.db() as db:
    cov = db.execute("SELECT job FROM coverage WHERE ref=?", (rows[0]["ref"],)).fetchone()[0]
    old_status = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0]
check("coverage points at new job", cov == rep)
check("old job superseded, revisions preserved", old_status == "superseded"
      and store.latest(job)["text"] == v["text"])
sel = r.read_revisions("residenta", Path(tmp) / "store")
check("selection now serves exactly the reviewed replacement",
      len(sel) == 1 and sel[0]["job"] == rep)

# reversibility: re-hold the replacement and re-point coverage back
store.hold_for_review(rep, "reviewer retracted approval", PROV)
check("re-held replacement excluded from selection", r.read_revisions("residenta", Path(tmp) / "store") == [])

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("ALL CHECKS PASSED")

# quarantine lift (targeted, audited) — appended after main run
with store.db() as db:
    db.execute("UPDATE jobs SET status='quarantined' WHERE id=?", (other,))
store.lift_quarantine(other, "rule fix aa3e734; targeted lift", "test")
check("quarantine lift returns job to pending", store.report().get("pending", 0) >= 1)
try:
    store.lift_quarantine(other, "double lift", "test")
    check("double lift blocked", False)
except ValueError:
    check("double lift blocked", True)
