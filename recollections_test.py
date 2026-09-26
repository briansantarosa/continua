"""recollections_test.py — agent-free tests for the shadow recollection store
(phases 1–2). No model calls: writer/checker are stubs. All writes under /tmp.
Proves the phase-1/2 gates: durable coverage/replay, atomic acceptance,
crash-in-flight recovery, budget/banding math, verifier contract (attribution,
invented feelings, echo, truncation, quoted-instruction attacks), failure
preserves predecessors, default-off trigger, and worker-lock single-flight.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
os.environ["CONTINUA_RECOLLECTIONS"] = "1"

import recollections as r  # noqa: E402

tmp = tempfile.mkdtemp(prefix="rec-test-")
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def source(instance, person, ts, role, content, uid, root):
    row = {"ts": ts, "instance": instance, "person_id": person, "role": role,
           "content": content, "uid": uid}
    path = os.path.join(root, instance, person, uid[:6] + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps(row) + "\n")
    try:
        return r.source_record(path, row, instance, root=root)
    except ValueError:
        # Invalid-row tests: remove the file so the scan fixture stays clean.
        os.remove(path)
        raise


CHROOT = tmp + "/chronicle"
os.makedirs(CHROOT, exist_ok=True)
S = [source("g", "1000000001", "2026-09-16T12:10:46-07:00", "user",
            "Can you help me understand that thought more?", "a" * 12, CHROOT),
     source("g", "1000000001", "2026-09-16T12:17:39-07:00", "assistant",
            "I told you about the Archivist and the Witness; I said the map is a substitute for the experience.", "b" * 12, CHROOT)]


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


def _raises_valueerror(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


def stub_writer(text_fn):
    class W:
        model = "stub-writer"
        def __call__(self, system, payload):
            return json.loads(text_fn) if isinstance(text_fn, str) else text_fn(payload)
    return W()


def accept_writer():
    return stub_writer(lambda p: {"sentences": [
        {"text": "Alex asked me about architecture as pointer, and I explained the Archivist and the Witness.",
         "sources": [p["sources"][0]["ref"]]},
        {"text": "I told him the map is a substitute for the experience, and he said I had captured the heart of it.",
         "sources": [p["sources"][1]["ref"]]}], "paragraph_starts": [0]})


class BadWriter:
    model = "bad"
    def __init__(self, draft):
        self.draft = draft
        self.calls = 0
    def __call__(self, system, payload):
        self.calls += 1
        return self.draft


def make_checker(reject_markers=("I decided the reset", "I felt joy")):
    def check_fn(system, payload):
        text = r.prose(payload["draft"])
        if any(m in text for m in reject_markers):
            return {"pass": False, "issues": ["invented feeling/decision unsupported by sources"],
                    "checked_sentences": list(range(len(payload["draft"]["sentences"])))}
        return {"pass": True, "issues": [],
                "checked_sentences": list(range(len(payload["draft"]["sentences"])))}
    C = type("C", (), {"model": "stub-checker"})
    C.__call__ = lambda self, system, payload: check_dispatch(system, payload)
    def check_dispatch(system, payload): pass
    return _Checker(reject_markers)


class _Checker:
    def __init__(self, markers=("I decided the reset was good", "I felt joy")):
        self.model = "stub-checker"
        self.markers = markers
    def __call__(self, system, payload):
        text = r.prose(payload["draft"])
        if any(m in text for m in self.markers):
            return {"pass": False, "issues": ["unsupported claim"], "checked_sentences": []}
        return {"pass": True, "issues": [],
                "checked_sentences": list(range(len(payload["draft"]["sentences"])))}


CHECK_OK = _Checker()

try:
    # ---- store mechanics ----------------------------------------------------
    store = r.Store("g", tmp)
    job = store.enqueue(S)
    check("enqueue returns a job", bool(job))
    check("replay of covered sources is a no-op", store.enqueue(S) is None)
    check("replay with an adjacent already-covered record still no-ops",
          store.enqueue(S + [S[0]]) is None)
    check("cross-resident sources rejected", _raises(lambda: Store("residenta", tmp).enqueue(S)))
    check("cross-thread episodes rejected", _raises_valueerror(
        lambda: store.enqueue(S + [source("g", "999", S[0]["ts"], "user", "x", "c" * 12, CHROOT)])))
    check("naive timestamps rejected", _raises_valueerror(
        lambda: source("g", "1000000001", "2026-09-16T12:10:46", "user", "x", "d" * 12, CHROOT)))
    # row person_id vs path person mismatch (late record landed in wrong file)
    badrow = {"ts": S[0]["ts"], "instance": "g", "person_id": "1000000001",
              "role": "user", "content": "x", "uid": "e" * 12}
    check("wrong-person source rejected", _raises_valueerror(
        lambda: r.source_record(os.path.join(CHROOT, "g", "1", "d.jsonl"),
                                badrow, "g", root=CHROOT)))
    check("non-conversation role rejected", _raises_valueerror(
        lambda: source("g", "1000000001", S[0]["ts"], "tool", "x", "f" * 12, CHROOT)))

    # ---- validation ----------------------------------------------------------
    good = {"sentences": [{"text": "I told Alex about the pointer idea.", "sources": [S[0]["ref"]]}],
            "paragraph_starts": [0]}
    check("valid draft passes", r.validate(good, S, 1000) == [])
    check("unknown ref fails", "missing/unknown evidence ref" in r.validate(
        {"sentences": [{"text": "I said x.", "sources": ["nope"]}], "paragraph_starts": [0]}, S, 1000))
    check("over budget fails", r.validate(good, S, 5) == ["over byte budget"])
    check("third-person narrator fails", any("first-person" in e for e in r.validate(
        {"sentences": [{"text": "Alex asked the resident a question.", "sources": [S[0]["ref"]]}],
         "paragraph_starts": [0]}, S, 1000)))
    check("prompt echo fails", any("narrator" in e for e in r.validate(
        {"sentences": [{"text": "The user asked me to rewrite the full book.", "sources": [S[0]["ref"]]}],
         "paragraph_starts": [0]}, S, 1000)))
    check("incomplete prose fails", any("incomplete" in e for e in r.validate(
        {"sentences": [{"text": "I said", "sources": [S[0]["ref"]]}], "paragraph_starts": [0]}, S, 1000)))
    check("invalid paragraph boundary fails", any("paragraph" in e for e in r.validate(
        {"sentences": good["sentences"], "paragraph_starts": [0, 5]}, S, 1000)))

    # ---- process: accept, verify, repair-once, fail-closed -------------------
    with r.worker_lock(store) as locked:
        check("worker lock acquired", locked)
        res = r.process(store, job, accept_writer(), CHECK_OK, source_root=CHROOT)
    check("accepted on first pass", res["status"] == "accepted", str(res))
    stored_path = store.path
    check("store file is private (0600)", oct(os.stat(stored_path).st_mode)[-3:] == "600")
    check("accepted on first pass", res["status"] == "accepted", str(res))
    stored = store.latest(job)
    check("stored revision is prose with sources and provenance",
          stored["text"].startswith("Alex asked") and stored["sources"] == S
          and stored["visibility"] == ["1000000001"] and stored["review"]["pass"] is True
          and stored["human_approved"] is False)
    check("reprocessing an accepted job changes nothing",
          r.process(store, job, accept_writer(), CHECK_OK, source_root=CHROOT)["status"] == "unchanged")

    store2 = r.Store("g", tmp + "2")
    job2 = store2.enqueue(S)
    class OnceBad:
        model = "flaky"
        def __init__(self):
            self.calls = 0
        def __call__(self, system, payload):
            self.calls += 1
            if self.calls == 1:  # invented decision first, good second
                return {"sentences": [{"text": "I decided the reset was good.", "sources": [S[0]["ref"]]}],
                        "paragraph_starts": [0]}
            return accept_writer()(system, payload)
    with r.worker_lock(store2) as locked:
        res = r.process(store2, job2, OnceBad(), CHECK_OK, source_root=CHROOT)
    check("one targeted repair succeeds", res["status"] == "accepted"
          and store2.report_audit_count(job2) == 2,
          str(res) + f" audits={store2.report_audit_count(job2)}")

    store3 = r.Store("g", tmp + "3")
    job3 = store3.enqueue(S)
    with r.worker_lock(store3) as locked:
        res = r.process(store3, job3, BadWriter({"sentences": [
            {"text": "I felt joy when Alex praised me.", "sources": [S[0]["ref"]]}],
            "paragraph_starts": [0]}), CHECK_OK, source_root=CHROOT)
    check("verifier-rejected draft quarantines", res["status"] == "rejected"
          and store3.report().get("quarantined") == 1, str(res))
    check("no revision published for rejected first draft", store3.latest(job3) is None)

    # compression: refuses to grow, preserves predecessor on failure
    store4 = r.Store("g", tmp + "4")
    job4 = store4.enqueue(S)
    with r.worker_lock(store4) as locked:
        check("no predecessor -> compression refused",
              r.process(store4, job4, accept_writer(), CHECK_OK, source_root=CHROOT, compress=True)["status"] == "no-predecessor")
        r.process(store4, job4, accept_writer(), CHECK_OK, source_root=CHROOT)
        full = store4.latest(job4)
        grower = stub_writer(lambda p: {"sentences": full["draft"]["sentences"] + [
            {"text": "I also remembered more and more and more detail here.", "sources": [S[0]["ref"]]}],
            "paragraph_starts": [0]})
        res = r.process(store4, job4, grower, CHECK_OK, source_root=CHROOT, budget=r.token_bound(full["text"]) - 10, compress=True)
        check("growing compression rejected", res["status"] == "rejected")
        check("predecessor byte-identical after failed compression",
              store4.latest(job4)["text"] == full["text"])
        check("status still accepted after failed compression", store4.report().get("accepted") == 1)
        smaller = stub_writer(lambda p: {"sentences": full["draft"]["sentences"][:1],
                                         "paragraph_starts": [0]})
        res = r.process(store4, job4, smaller, CHECK_OK, source_root=CHROOT, budget=r.token_bound(full["text"]) - 1,
                        compress=True)
        check("genuine compression accepted and strictly smaller",
              res["status"] == "accepted" and r.token_bound(res["text"]) < r.token_bound(full["text"]))
        check("both revisions retained", len(store4.revisions(job4)) == 2)
        # chunk-6 completion (§4c ladder): the rendering type is stamped —
        # full prose is 'full'; a verified shrink of the same episode is
        # 'shorter'; age selects which renders, never what exists.
        renderings = sorted((v.get("rendering") or "full") for v in store4.revisions(job4))
        check("rendering stamped: full + shorter", renderings == ["full", "shorter"], str(renderings))

    # ---- banding / budgets (pure) ---------------------------------------------
    check("bands: day/week/month/year/older",
          # chunk 5/§6a: the recent band is strictly 24 hours
          [r.age_band("2026-09-16T12:00:00-07:00", t) for t in (
              "2026-09-16T13:00:00-07:00", "2026-09-18T11:00:00-07:00",
              "2026-09-24T11:00:00-07:00", "2026-10-17T12:00:00-07:00",
              "2026-11-16T12:00:00-07:00", "2027-09-16T12:00:00-07:00")]
          == ["days", "week", "month", "year", "year", "older"])

    # ---- trigger default-off + scan boundaries --------------------------------
    os.environ.pop("CONTINUA_RECOLLECTIONS_SHADOW", None)
    check("chat-thread trigger default-off", r.request_shadow("g") is False)
    os.environ["CONTINUA_RECOLLECTIONS_SHADOW"] = "1"
    os.environ["CONTINUA_RECOLLECTIONS"] = "0"
    check("master kill switch respected", r.request_shadow("g") is False)
    os.environ["CONTINUA_RECOLLECTIONS"] = "1"
    check("trigger returns without model calls when ritual lock held",
          r.request_shadow("g") in (True, False))  # nonblocking; worker decides

    store5 = r.Store("g", tmp + "5")
    stats = r.scan(store5, source_root=CHROOT, limit=5,
                   at="2026-09-16T13:00:00-07:00")
    check("scan queues the one complete exchange",
          # new: the 999 cross-thread orphan is counted (incomplete), never
          # enqueued; the a/b exchange joins across separate files.
          stats["queued"] == 1 and stats["malformed"] == 0
          and stats["incomplete_exchanges"] == 1, str(stats))
    check("scan enqueued a real job", store5.report().get("pending") == 1)
    # within coalescing margin -> not yet eligible (isolated chronicle so the
    # eligible 12:10 exchange from CHROOT can't mask the deferred bucket)
    CHROOT2 = tmp + "/chronicle2"
    row6 = {"ts": "2026-09-16T12:58:00-07:00", "instance": "g",
            "person_id": "1000000001", "role": "user", "content": "x", "uid": "f" * 12}
    row7 = dict(row6, uid="e" * 12, ts="2026-09-16T12:59:30-07:00", role="assistant")
    p6 = os.path.join(CHROOT2, "g", "1000000001", "f" * 6 + ".jsonl")
    os.makedirs(os.path.dirname(p6), exist_ok=True)
    open(p6, "w").write(json.dumps(row6) + "\n")
    p7 = os.path.join(CHROOT2, "g", "1000000001", "e" * 6 + ".jsonl")
    open(p7, "w").write(json.dumps(row7) + "\n")
    stats2 = r.scan(r.Store("g", tmp + "/7"), source_root=CHROOT2, limit=5,
                    at="2026-09-16T13:00:00-07:00")
    check("coalescing margin respected (12:58 bucket not closed before 13:00)",
          stats2["queued"] == 0 and stats2["malformed"] == 0, str(stats2))
    # 5+ minutes later the same bucket is closed and eligible
    stats3 = r.scan(r.Store("g", tmp + "/8"), source_root=CHROOT2, limit=5,
                    at="2026-09-16T13:06:00-07:00")
    check("closed bucket + margin becomes eligible",
          stats3["queued"] == 1, str(stats3))

    # crash-in-flight: a pending job with attempts>=3 quarantines on next run
    store6 = r.Store("g", tmp + "6")
    job6 = store6.enqueue(S)
    with store6.db() as db:
        for _ in range(3):
            db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (job6,))
    res = r.run_shadow("g", root=tmp + "6", source_root=CHROOT, max_jobs=1,
                       writer=accept_writer(), checker=CHECK_OK)
    check("stale in-flight job quarantined, not retried forever",
          res["counts"].get("quarantined") == 1, str(res))
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"FAILED: {len(fails)} — {fails}")
    sys.exit(1)
print("ALL CHECKS PASSED")

# ---- chunk-6 completion regression guards (§4c ladder / §6d.4 scheduler) ----
check("checker carries the compression bar (omission is the mechanism; fidelity of what remains)",
      "deliberate omission is the mechanism" in r.CHECKER
      and "fidelity of what remains" in r.CHECKER
      and 'purpose is "compress"' in r.CHECKER,
      r.CHECKER[-420:])
check("writer compression clause permits omission, forbids distortion",
      "you may omit supporting detail" in r.WRITER and "never change what remains" in r.WRITER,
      r.WRITER[-320:])
