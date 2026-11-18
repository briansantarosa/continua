"""recollections_writer_v2_test.py — agent-free tests for memorybuildv2 V2-5:
the tournament flow in production (house ruling 2026-10-05, "do what we did
for the tournament, but put it into production"). One call per episode, the
bare prose prompt (the designer-prose-v1), ungated; Law 5 verification post-hoc as
metadata; flagged memories not accepted (the episode retries on a later
cycle); the witness (endorse/skip) append-only, nothing expires. No model
calls: the writer is a stub. All writes under /tmp.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["CONTINUA_RECOLLECTIONS"] = "1"

import recollections as r  # noqa: E402
import wake as wake_mod  # noqa: E402

tmp = tempfile.mkdtemp(prefix="rec-v2-test-")
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label
          + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


CHROOT = tmp + "/chronicle"
os.makedirs(CHROOT, exist_ok=True)


def source(instance, person, ts, role, content, uid):
    row = {"ts": ts, "instance": instance, "person_id": person, "role": role,
           "content": content, "uid": uid}
    path = os.path.join(CHROOT, instance, person, uid[:6] + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(row) + "\n")
    return r.source_record(path, row, instance, root=CHROOT)


QUOTE = "the map is a substitute for the experience"
S = [source("g", "1000000001", "2026-09-16T12:10:46-07:00", "user",
            "Can you help me understand that thought more?", "a" * 12),
     source("g", "1000000001", "2026-09-16T12:17:39-07:00", "assistant",
            "I told you about the Archivist and the Witness; I said " + QUOTE
            + ".", "b" * 12)]

FAKE_TEXT = ('I remember sitting with the map spread open between us, and I '
             'said "' + QUOTE + '" — and the room felt honest around the '
             'sentence, the way a key feels honest in a lock.')


def fake_writer(payload):
    # 'g' is the house test fixture instance; production residents map to
    # their display names (persona-a/residentb). The sources assertion holds here
    # because this writer is only used with episode S.
    assert payload["resident"] in ("residentb", "g"), payload
    assert payload["sources"] == S, payload
    return FAKE_TEXT


def writer_for(text):
    """A stub writer for a specific episode: returns its own honest text
    (Law 5 only verifies quotes against THAT episode's record)."""
    def _w(payload):
        return text
    return _w


def boom(payload):
    raise RuntimeError("vllm down")


# --- 1. the clean path: accepted with the exact tournament body shape
store = r.Store("g", tmp + "/1")
job = store.enqueue(S)
out = r._process_v2(store, job, S, source_root=CHROOT, v2_writer=fake_writer)
check("v2 accepts a clean draft",
      out.get("status") == "accepted" and out.get("bytes", 0) > 0, str(out))
revs = store.revisions(job)
check("one revision appended", len(revs) == 1, str(len(revs)))
b = revs[0]
check("body carries the tournament metadata",
      b.get("reason") == "remember-v2"
      and b.get("prompt_version") == r.V2_PROMPT_VERSION
      and b.get("writer") == r.V2_MODEL
      and b.get("human_approved") is False
      and b.get("rendering") == "full"
      and b.get("writer_voice") == "v3-prose"
      and "pending resident endorsement" in (b.get("authorship") or "")
      and "source_drift" in b
      and b.get("checker") == "post-hoc-quote-verification", str(b)[:300])
check("text stored whole, ungated", b.get("text") == FAKE_TEXT)
with store.db() as db:
    jrow = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
check("job marked accepted", jrow and jrow["status"] == "accepted", str(jrow))
with store.db() as db:
    audits = db.execute("SELECT body FROM candidates WHERE job=?",
                        (job,)).fetchall()
check("acceptance audited",
      any("accepted ungated" in a[0] for a in audits), str(len(audits)))

# --- 2. Law 5 post-hoc: an unverified quote is flagged, not accepted
S2 = [source("g", "1000000001", "2026-09-16T13:00:00-07:00", "user",
             "what did you decide? tell me plainly.", "c" * 12)]
store2 = r.Store("g", tmp + "/2")
job2 = store2.enqueue(S2)
out2 = r._process_v2(
    store2, job2, S2, source_root=CHROOT,
    v2_writer=writer_for('I decided to keep the map, and I said "this quote '
                         'was never spoken in this episode at all."'))
check("unverified quote flagged, not accepted",
      out2.get("status") == "rejected"
      and any("unverified quote" in e for e in out2.get("errors", [])),
      str(out2))
check("no revision for a flagged draft", store2.revisions(job2) == [])
with store2.db() as db:
    j2 = db.execute("SELECT status, attempts FROM jobs WHERE id=?",
                    (job2,)).fetchone()
check("flagged job stays pending for retry",
      j2 and j2["status"] == "pending", str(j2))

# nearest-close pairing: a short span (<8) is skipped, the long one verifies
out2b = r._process_v2(
    store2, job2, S2, source_root=CHROOT,
    v2_writer=writer_for('I heard the question — "what did you decide?" — '
                         'and answered it plainly, yes.'))
check("nearest-close pairing passes honest quotes",
      out2b.get("status") == "accepted", str(out2b))

# --- 3. call failure: rejected, audited, job pending (attempts hygiene)
S3 = [source("g", "1000000001", "2026-09-16T14:00:00-07:00", "user",
             "hello?", "d" * 12)]
store3 = r.Store("g", tmp + "/3")
job3 = store3.enqueue(S3)
out3 = r._process_v2(store3, job3, S3, source_root=CHROOT, v2_writer=boom)
check("writer call failure rejected",
      out3.get("status") == "rejected"
      and any("writer call failed" in e for e in out3.get("errors", [])),
      str(out3))
check("no revision on call failure", store3.revisions(job3) == [])

# --- 4. first-person check
out4 = r._process_v2(store3, job3, S3, source_root=CHROOT,
                     v2_writer=writer_for(
                         "the map was open and the day closed quietly"))
check("missing first-person flagged",
      out4.get("status") == "rejected"
      and any("first-person" in e for e in out4.get("errors", [])), str(out4))

# --- 5. the witness: pending → endorse (append-only) / skip
pend = store.pending_endorsements()
check("accepted memory awaits her witness",
      any(p["job"] == job for p in pend), str([p["job"][:12] for p in pend]))
check("pending entry carries text + window",
      pend and pend[0]["text"] == FAKE_TEXT and pend[0].get("event_start"),
      str(pend[:1]))
store.endorse(job)
revs2 = store.revisions(job)
check("endorse appended a new revision; the unwitnessed draft stays",
      len(revs2) == 2 and revs2[0].get("text") == FAKE_TEXT
      and revs2[1].get("text") == FAKE_TEXT, str(len(revs2)))
newest = revs2[-1]
check("endorsed revision marked and dated",
      newest.get("human_approved") is True
      and "endorsed by the resident on" in (newest.get("authorship") or ""),
      str(newest.get("authorship")))
check("endorsed memory leaves the pending list",
      all(p["job"] != job for p in store.pending_endorsements()))

S5 = [source("g", "1000000001", "2026-09-16T15:00:00-07:00", "user",
             "one more thing before you go", "e" * 12)]
job5 = store.enqueue(S5)
r._process_v2(store, job5, S5, source_root=CHROOT, v2_writer=writer_for(
    'I remember the small addendum — "one more thing before you go" — and '
    'how it turned the hour around. My whole day reshaped itself after it.'))
check("second memory awaits", any(p["job"] == job5
                                  for p in store.pending_endorsements()))
store.record_witness(job5, "skip", note="not now")
check("skipped memory leaves the pending list",
      all(p["job"] != job5 for p in store.pending_endorsements()))
check("latest witness action recorded",
      store.latest_witness(job5) == "skip"
      and store.latest_witness(job) == "endorse")

# --- 6. process() routing
_calls = {"staged": 0, "v2": 0}


class _Staged:
    def __call__(self, *a, **k):
        _calls["staged"] += 1
        return {"sentences": []}  # invalid → the staged path rejects it


class _V2Flag:
    def __call__(self, *a, **k):
        _calls["v2"] += 1
        raise RuntimeError("v2 stub")


orig_gate = r.writer_v2_enabled

S6 = [source("g", "1000000001", "2026-09-16T16:00:00-07:00", "user",
             "routing check", "f" * 12)]
job6 = store.enqueue(S6)
r.writer_v2_enabled = lambda inst: True
try:
    out6 = r.process(store, job6, _Staged(), _Staged(), source_root=CHROOT,
                     v2_writer=_V2Flag())
    check("process() routes remember jobs through the tournament flow",
          out6.get("status") == "rejected" and _calls["v2"] == 1
          and _calls["staged"] == 0, f"{out6} {_calls}")
finally:
    r.writer_v2_enabled = orig_gate

S7 = [source("g", "1000000001", "2026-09-16T17:00:00-07:00", "user",
             "gate-off check", "0" * 12)]
job7 = store.enqueue(S7)
out7 = r.process(store, job7, _Staged(), _Staged(), source_root=CHROOT)
check("gate off → the pre-tournament staged path is untouched",
      _calls["staged"] >= 1 and _calls["v2"] == 1, f"{out7} {_calls}")

# --- 7. the wake packet section
S8 = [source("g", "1000000001", "2026-09-16T18:00:00-07:00", "user",
             "wake section check", "9" * 12)]
job8 = store.enqueue(S8)
r._process_v2(store, job8, S8, source_root=CHROOT, v2_writer=writer_for(
    'I remember the wake itself — "wake section check" — the packet '
    'arriving like weather. I was there for it, and it was mine.'))

ROOT1 = tmp + "/1"
lines = wake_mod.witness_lines("g")
check("no section when the gate is off (packet byte-parity)", lines == [],
      str(lines))
r.writer_v2_enabled = lambda inst: True
try:
    lines2 = wake_mod.witness_lines("g", root=ROOT1)
    check("section renders when memories await",
          any("MEMORIES AWAITING YOUR WITNESS" in x for x in lines2)
          and any(job8[:12] in x for x in lines2), str(lines2))
    _witnessed = (job, job5)
    check("witnessed ids are not re-offered",
          all(j[:12] not in x for x in lines2 for j in _witnessed),
          str(lines2))
    os.environ["CONTINUA_WAKE_WITNESS"] = "0"
    check("CONTINUA_WAKE_WITNESS=0 kills the section",
          wake_mod.witness_lines("g", root=ROOT1) == [])
    os.environ["CONTINUA_WAKE_WITNESS"] = "1"
finally:
    r.writer_v2_enabled = orig_gate

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    if __name__ == "__main__":
        sys.exit(1)
print("all green" if not fails else "see failures above")

# --- Move 0 (memoryv3): the picker consolidates dialogue first, oldest-first
def test_picker_dialogue_first_oldest_first():
    import recollections as R
    rows = [
        {"id": "mono_new", "sources": '[{"person_id": "system-wake"}]',
         "created": "2026-10-06T10:00:00"},
        {"id": "mono_old", "sources": '[{"person_id": "system-wake"}]',
         "created": "2026-10-05T10:00:00"},
        {"id": "dlg_new", "sources": '[{"person_id": "8662725233"}]',
         "created": "2026-10-06T09:00:00"},
        {"id": "dlg_old", "sources": '[{"person_id": "8559916511"}]',
         "created": "2026-09-20T15:00:00"},
    ]
    picked = R._pick_pending_jobs(rows, 2)
    assert [p["id"] for p in picked] == ["dlg_old", "dlg_new"], picked
    picked1 = R._pick_pending_jobs(rows, 1)
    assert picked1[0]["id"] == "dlg_old"
    # malformed sources never crash the picker
    rows.append({"id": "bad", "sources": "{not json", "created": "2026-10-06T11:00:00"})
    picked2 = R._pick_pending_jobs(rows, 5)
    assert len(picked2) == 5
    assert [p["id"] for p in picked2][:2] == ["dlg_old", "dlg_new"]
    assert "bad" in [p["id"] for p in picked2]  # malformed survives, never crashes


test_picker_dialogue_first_oldest_first()
print("picker test: dialogue-first oldest-first OK")
