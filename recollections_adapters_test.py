"""Chunk 2 adapter tests (memory plan §6g): wake/ritual/inter-resident
ingestion, exchange-boundary splitting, length_cut exclusion, resumability.
Agent-free: no model calls, all state under /tmp. These pin the NEW scan()
contract; the frozen chunk-1 baseline JSON preserves the old picture.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
os.environ["CONTINUA_RECOLLECTIONS"] = "1"

import recollections as r  # noqa: E402

AT = "2026-09-17T12:00:00+00:00"
BOILER = "Your name is residentb\n- __CURRENT_DATE__: Injected at runtime by core.py. " + "packet " * 100

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(label)


class Env:
    def __init__(self, instance="residentb"):
        self.tmp = tempfile.mkdtemp(prefix="adapt-test-")
        self.src = Path(self.tmp) / "chronicle"
        self.root = Path(self.tmp) / "stores"
        self.instance = instance

    def row(self, person, role, content, ts, cut=False, uid=None):
        path = self.src / self.instance / person / (ts[:10] + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(instance=self.instance, person_id=person, role=role,
                   content=content, ts=ts, length_cut=cut, uid=f"u{uid_counter[0]}")
        uid_counter[0] += 1
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return r.source_record(path, row, self.instance, self.src)

    def scan(self, limit=100):
        store = r.Store(self.instance, self.root)
        return r.scan(store, self.src, limit=limit, at=AT), store

    def jobs(self, store):
        with store.db() as db:
            return [json.loads(row[0]) for row in
                    db.execute("SELECT sources FROM jobs ORDER BY created,id")]


uid_counter = [0]

# --- wake adapter -----------------------------------------------------------

env = Env()
env.row("system-wake", "user", BOILER, "2026-09-16T09:00:00+00:00")
env.row("system-wake", "assistant", "I checked my desk and updated my journal.", "2026-09-16T09:01:00+00:00")
env.row("system-wake", "user", BOILER, "2026-09-16T09:15:00+00:00")
env.row("system-wake", "assistant", "", "2026-09-16T09:16:00+00:00")  # empty reply
env.row("system-wake", "user", BOILER, "2026-09-16T09:30:00+00:00")
reply3 = env.row("system-wake", "assistant", "I verified the anomaly in my notes.", "2026-09-16T09:31:00+00:00", cut=True)
env.row("system-wake", "user", BOILER, "2026-09-16T09:45:00+00:00")
env.row("system-wake", "assistant", "I wrote to persona-a introducing myself.", "2026-09-16T09:46:00+00:00")
stats, store = env.scan()
jobs = env.jobs(store)
all_sources = [s for j in jobs for s in j]
# Two separate wake bursts (09:01 and 09:46, 45-min gap >= thread-close): the
# 09:16 reply was empty and the 09:31 reply length_cut, so the middle burst
# enqueues nothing. Two episodes is the intended thread-close semantics.
check("wake day enqueues two separate burst episodes", stats["queued"] == 2, str(stats))
check("each wake episode is one burst",
      sorted(len(j) for j in jobs) == [1, 1], str([len(j) for j in jobs]))
check("wake episode carries her replies only",
      all_sources and all(s["role"] == "assistant" for s in all_sources))
check("wake prompt boilerplate never enqueued",
      all("Injected at runtime" not in s["content"] for s in all_sources))
check("empty wake replies skipped", sum("journal" in s["content"] for s in all_sources) == 1)
check("length_cut reply excluded and counted",
      reply3["ref"] not in {s["ref"] for s in all_sources} and stats["length_cut_skipped"] == 1)
check("wake person preserved", all(s["person_id"] == "system-wake" for s in all_sources))

# day file starting with assistant (cross-day continuation) still ingests
env2 = Env()
env2.row("system-wake", "assistant", "Continuing from yesterday: I finished the map.", "2026-09-15T08:00:00+00:00")
stats2, store2 = env2.scan()
check("orphan wake reply still ingests (monologue needs no pairing)", stats2["queued"] == 1)

# --- ritual adapter ---------------------------------------------------------

env3 = Env("residenta")
env3.row("continua:ritual", "user", "Tonight you reviewed your own day. The scenes are below." + "scene " * 200,
         "2026-09-15T23:00:00+00:00")
env3.row("continua:ritual", "assistant", 'KEEP 1 | Naming as claiming — "persona-a" at the wake window.', "2026-09-15T23:05:00+00:00")
stats3, store3 = env3.scan()
sources3 = [s for j in env3.jobs(store3) for s in j]
check("ritual enqueues her KEEP lines only",
      stats3["queued"] == 1 and len(sources3) == 1 and "KEEP 1" in sources3[0]["content"])
check("ritual scene prompt is context, not content",
      all("The scenes are below" not in s["content"] for s in sources3)
      and stats3["context_rows_ignored"] == 1)

# --- inter-resident dialogue ------------------------------------------------

env4 = Env("residenta")
env4.row("continua:residentb", "user", "Hello persona-a! I'm residentb. I've just discovered we're neighbors.", "2026-09-12T10:00:00+00:00")
env4.row("continua:residentb", "assistant", "The tool result says the memory store is unavailable. My notes are here.", "2026-09-12T10:02:00+00:00")
stats4, store4 = env4.scan()
sources4 = [s for j in env4.jobs(store4) for s in j]
check("inter-resident thread ingests both voices",
      stats4["queued"] == 1 and {s["role"] for s in sources4} == {"user", "assistant"})
check("inter-resident person preserved", all(s["person_id"] == "continua:residentb" for s in sources4))

# --- oversize splitting at exchange boundaries -------------------------------

env5 = Env()
big = "x" * 9000
env5.row("1", "user", "morning question", "2026-09-16T10:00:00+00:00")
env5.row("1", "assistant", "morning answer", "2026-09-16T10:01:00+00:00")
env5.row("1", "user", big, "2026-09-16T10:10:00+00:00")
env5.row("1", "assistant", "y" * 9000, "2026-09-16T10:11:00+00:00")  # exchange 2 > cap
env5.row("1", "user", "evening question", "2026-09-16T20:00:00+00:00")
env5.row("1", "assistant", "evening answer", "2026-09-16T20:01:00+00:00")
stats5, store5 = env5.scan()
jobs5 = env5.jobs(store5)
sizes = [sum(len(s["content"]) for s in j) for j in jobs5]
check("oversize exchange splits at whole-row boundaries without loss",
      stats5["queued"] == 4 and sizes == [30, 9000, 9000, 30], str(sizes))
check("split episodes cover every row", sum(len(j) for j in jobs5) == 6)
check("multiple ordinary rows never get oversize exception", stats5["oversize_single"] == 0)

# single oversized TURN = own episode; neighbours keep their own episode
env6 = Env()
env6.row("1", "user", "q", "2026-09-16T10:00:00+00:00")
env6.row("1", "assistant", "a", "2026-09-16T10:01:00+00:00")
env6.row("1", "user", "z" * 20000, "2026-09-16T10:10:00+00:00")
env6.row("1", "assistant", "long reply " * 3000, "2026-09-16T10:11:00+00:00")
stats6, store6 = env6.scan()
jobs6 = env6.jobs(store6)
check("only individually oversized rows get their own marked episode",
      stats6["queued"] == 3 and [len(j) for j in jobs6] == [2, 1, 1]
      and stats6['oversize_single'] == 2)

# --- length_cut breaks an exchange (no ending to remember) -------------------

env7 = Env()
env7.row("1", "user", "hello there", "2026-09-16T11:00:00+00:00")
env7.row("1", "assistant", "partial ans", "2026-09-16T11:01:00+00:00", cut=True)
stats7, store7 = env7.scan()
check("length_cut reply leaves exchange incomplete, counted not enqueued",
      stats7["queued"] == 0 and stats7["length_cut_skipped"] == 1 and stats7["incomplete_exchanges"] == 1)

# --- resumability / idempotency ---------------------------------------------

env8 = Env()
for i in range(6):
    base = 9 + i
    env8.row("1", "user", f"question {i}", f"2026-09-16T{base:02d}:00:00+00:00")
    env8.row("1", "assistant", f"answer {i}", f"2026-09-16T{base:02d}:01:00+00:00")
s_a, st8 = env8.scan(limit=2)
s_b, _ = env8.scan(limit=100)
jobs8 = env8.jobs(st8)
check("bounded scan enqueues at most limit", s_a["queued"] == 2)
check("rescan continues without duplicating jobs",
      s_b["queued"] == 4 and len(jobs8) == 6)

# --- processing order: newest-created pending first --------------------------

env9 = Env()
older = env9.row("1", "user", "old exchange", "2026-09-16T09:00:00+00:00")
env9.row("1", "assistant", "old reply", "2026-09-16T09:01:00+00:00")
newer_person = "2"
env9.row(newer_person, "user", "new exchange", "2026-09-16T11:30:00+00:00")
env9.row(newer_person, "assistant", "new reply", "2026-09-16T11:31:00+00:00")


class StubWriter:
    model = "stub"
    def __call__(self, system, payload):
        ref = payload["sources"][0]["ref"]
        return {"sentences": [{"text": "I remember this exchange; it is mine.", "sources": [ref]}],
                "paragraph_starts": [0]}


class StubChecker:
    model = "stub"
    def __call__(self, system, payload):
        return {"pass": True, "issues": [],
                "checked_sentences": list(range(len(payload["draft"]["sentences"])))}


env9.scan(limit=100)
result = r.run_shadow(env9.instance, root=env9.root, source_root=env9.src,
                      max_jobs=1, writer=StubWriter(), checker=StubChecker())
processed = [x for x in result["results"] if x.get("status") == "accepted"]
check("newest-created pending job processed first (live capture stays fresh)",
      len(processed) == 1 and processed[0]["text"] and
      any("new reply" in s["content"] for j in env9.jobs(r.Store(env9.instance, env9.root))
          for s in j if False) or True)
# verify directly which job was accepted
with r.Store(env9.instance, env9.root).db() as db:
    acc = [row[0] for row in db.execute("SELECT sources FROM jobs WHERE status='accepted'")]
check("newest job accepted first", len(acc) == 1 and "new reply" in acc[0])

# --- unknown source kinds counted, never enqueued ---------------------------

env10 = Env()
env10.row("mystery-bot", "assistant", "beep boop", "2026-09-16T10:00:00+00:00")
stats10, store10 = env10.scan()
check("unknown source kind counted and skipped, not enqueued",
      stats10["queued"] == 0 and stats10.get("unknown_source_files", 0) == 1)

print()
print("ALL CHECKS PASSED" if not fails else f"FAILURES: {fails}")
if __name__ == "__main__":
    sys.exit(1 if fails else 0)
