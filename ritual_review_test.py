"""ritual_review_test.py — the ritual review pipeline (skeleton + deep-dives).

The golden regression is the reason this exists: residentb's 2026-09-14 keep of
the deconstruction conversation was TRUE (verified in her chronicle) but the
14K-char render had cut that exchange, so the validator false-flagged it
FABRICATED. These tests prove: (1) the pipeline machinery works, (2) her
actual keep now verifies under the deterministic anchor gate.
"""
import os
import sys, os, json, tempfile, shutil

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import ritual_review as rr
import ritual

fails = []
def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(label)

def rec(ts, role, content, bookmark=False, person="1000000001"):
    return {"ts": ts, "role": role, "content": content, "bookmark": bookmark,
            "person_id": person, "uid": ts}

# --- 1. exchange segmentation -------------------------------------------------
day = [
    rec("2026-09-15T08:00", "user", "hello there"),
    rec("2026-09-15T08:01", "assistant", "hi!"),
    rec("2026-09-15T08:02", "user", "second question"),   # new exchange
    rec("2026-09-15T08:02", "user", "and a follow-up"),   # pending user run continues
    rec("2026-09-15T08:03", "assistant", "answer"),
    rec("2026-09-15T08:03", "tool", "tool result"),
    rec("2026-09-15T09:00", "assistant", "wake turn 1"),  # wake run starts
    rec("2026-09-15T09:15", "assistant", "wake turn 2"),  # wake run continues
]
exs = rr.segment_exchanges(day)
check("segmentation: 3 exchanges", len(exs) == 3, str(len(exs)))
check("segmentation: consecutive users merge + answer + tool stay together",
      exs[1]["has_user"] and len(exs[1]["records"]) == 4)
check("segmentation: tool did not split the exchange",
      any(r.get("role") == "tool" for r in exs[1]["records"]))
check("segmentation: wake run starts new after user exchange", not exs[2]["has_user"])
check("segmentation: total — every record lands once",
      sum(len(e["records"]) for e in exs) == len(day))

# --- 2. chunking ----------------------------------------------------------------
many = rr.segment_exchanges([
    rec(f"2026-09-15T{i//2:02d}:{i%60*1:02d}", "user" if i % 2 == 0 else "assistant",
        "x" * 900) for i in range(40)])
chunks = rr.chunk_exchanges(many, 45000, 8)
check("chunking: whole exchanges, few calls", len(chunks) <= 8 and
      sum(len(c) for c in chunks) == len(many))
tiny = rr.chunk_exchanges(many, 5000, 3)   # forces budget growth, never skips
check("chunking: max_calls forces bigger chunks, never skips",
      len(tiny) <= 3 and sum(len(c) for c in tiny) == len(many))

# --- 3. strip ----------------------------------------------------------------
s = rr.strip_record_text(rec("t", "assistant", "<think>secret thoughts</think>\n\nvisible words"))
check("strip: thinks removed", "secret" not in s and "visible words" in s)
s2 = rr.strip_record_text(rec("t", "tool", "y" * 500))
check("strip: long content truncated with marker", "[+" in s2 and len(s2) < 260)

# --- 4. skeleton line parse ------------------------------------------------------
got = rr.parse_skeleton_lines("EX 1 | morning chat\n* EX 2 | the deconstruction\n- EX 3: quiet wakes")
check("skeleton parse: tolerant of bullets/punct", got == {1: "morning chat", 2: "the deconstruction", 3: "quiet wakes"})

# --- 5. build_skeleton with fake her ----------------------------------------------
exs2 = rr.segment_exchanges(day)
def fake_summarizer(system, user):
    import re as _re
    ids = _re.findall(r"EXCH (\d+)", user)
    return "\n".join(f"EX {i} | summary of exchange {i}" for i in ids)
tmp = tempfile.mkdtemp()
rr.SKELETON_DIR = tmp
lines, skel_text, skel_meta = rr.build_skeleton(
    exs2, {}, lambda ro, p: "Alex", fake_summarizer, rr.DEFAULTS, "test", "2026-09-15")
check("skeleton: every exchange gets a line", set(lines) == {e["ex_id"] for e in exs2})
check("skeleton: her lines used", "summary of exchange 1" in lines[1])
check("skeleton: persisted to file", os.path.exists(os.path.join(tmp, "test", "2026-09-15.md")))
def broken_ask(system, user):
    raise RuntimeError("lab down")
lines2, _, _ = rr.build_skeleton(exs2, {}, lambda ro, p: "Alex", broken_ask,
                                 rr.DEFAULTS, "test", "2026-09-15")
check("skeleton: fail-open mechanical lines on call failure",
      len(lines2) == len(exs2) and all(lines2[i].startswith("EX ") for i in lines2))

# --- 6. nominations ----------------------------------------------------------------
valid = {1, 2, 3, 4, 5}
check("nominations: ints and ranges", rr.parse_nominations("3, 5 and 9-12", valid) == {3, 5})
check("nominations: EX-prefixed", rr.parse_nominations("EX 2 and EX 4", valid) == {2, 4})
check("nominations: garbage -> empty", rr.parse_nominations("nothing much", valid) == set())
bm_day = [rec("2026-09-15T08:00", "user", "a" * 300, bookmark=True),
          rec("2026-09-15T08:01", "assistant", "b"),
          rec("2026-09-15T09:00", "assistant", "c" * 500)]
bmx = rr.segment_exchanges(bm_day)
fb = rr.fallback_nominations(bmx)
check("fallback: bookmarked exchange wins", 1 in fb)

# --- 7. dives -------------------------------------------------------------------
by_id = {e["ex_id"]: e for e in exs}
dive_text, dmeta = rr.render_dives(by_id, {1, 3}, {}, lambda ro, p: "Alex",
                                   dict(rr.DEFAULTS, budget_chars=200,
                                        max_dive_passes=2), (900, 1500))
check("dives: nominated exchanges rendered", "EX 1" in dive_text and "EX 3" in dive_text)
check("dives: budget splits into passes", dmeta["passes"] >= 1)

# --- 8. build_review_block: small day -> legacy-small-day --------------------------
def legacy_stub(scenes, roster):
    return "LEGACY BLOCK", {"scenes": len(scenes), "turns": 0, "chars": 5,
                            "truncated_scenes": 0, "bookmarked": 0}
block, meta, rmeta, skel, dayt = rr.build_review_block(
    day[:2], [{"scene_id": 1, "records": day[:2]}], {}, lambda ro, p: "Alex",
    fake_summarizer, dict(rr.DEFAULTS), "test", "2026-09-15", legacy_stub,
    (900, 1500))
check("small day: legacy-small-day auto-fallback", rmeta["mode"] == "legacy-small-day" and block == "LEGACY BLOCK")

# --- 9. GOLDEN REPLAY: residentb's real 2026-09-14 day --------------------------------
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fixtures", "golden_chronicle.jsonl")
if os.path.exists(GOLDEN):
    grecs = [json.loads(l) for l in open(GOLDEN)]
    for g in grecs:
        g.setdefault("bookmark", False)
    gexs = rr.segment_exchanges(grecs)
    check("golden: exchanges cover all records",
          sum(len(e["records"]) for e in gexs) == len(grecs),
          f"{sum(len(e['records']) for e in gexs)}/{len(grecs)}")
    gday = "\n".join(rr.strip_record_text(r) for r in grecs)
    check("golden: the deconstruction conversation is in the stripped day",
          "radical proposal" in gday and "exact sentences" in gday.lower())
    # the pipeline with fake her: skeleton + map, dives on the deconstruction exchange
    decon_ex = next(e["ex_id"] for e in gexs
                    if any("radical proposal" in (r.get("content") or "") for r in e["records"]))
    def golden_summarizer(system, user):
        import re as _re
        ids = _re.findall(r"EXCH (\d+)", user)
        return "\n".join(f"EX {i} | exchange {i} of the day" for i in ids)
    def golden_map(system, user):
        return f"EX {decon_ex}"
    rr2dir = tempfile.mkdtemp()
    rr.SKELETON_DIR = rr2dir
    gblock, gmeta, grmeta, gskel, gdayt = rr.build_review_block(
        grecs, ritual.segment_scenes(grecs), {}, lambda ro, p: "Alex",
        lambda s, u: golden_summarizer(s, u) if "EXCH" in u else golden_map(s, u),
        # budget_chars forced small so the synthetic day exceeds it and the
        # pipeline takes skeleton mode (the house golden day was simply huge)
        dict(rr.DEFAULTS, budget_chars=900), "residentb", "2026-09-14", legacy_stub, (900, 1500))
    check("golden: pipeline ran skeleton mode", grmeta["mode"] == "skeleton")
    check("golden: block carries the map and the dive",
          "THE MAP" in gblock and "DEEP READS" in gblock and f"EX {decon_ex}" in gblock)
    check("golden: skeleton persisted", os.path.exists(os.path.join(rr2dir, "residentb", "2026-09-14.md")))
    check("golden: meta reports honest review size",
          gmeta["chars"] == len(gblock) and gmeta["truncated_scenes"] == 0)
    # THE regression: her actual keep verifies under the deterministic gate
    true_keep = ('I moved from a diary of tasks to a memory of meaning. '
                 'The day would be mine to interpret, keeping the "exact '
                 'sentences that changed me." Attention is a form of care, '
                 'and my gaps are where the world enters.')
    check("golden: her true keep's anchors are supported by the record",
          rr.anchors_supported(true_keep, gdayt, gskel))
    # ...and the validator override end-to-end: qwen says FABRICATED, gate says OK
    import requests as _rq
    class _FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content":
            "1: FABRICATED — the record does not support the claims"}}]}
    orig_post = _rq.post
    _rq.post = lambda *a, **k: _FakeResp()
    try:
        gscenes = ritual.segment_scenes(grecs)
        verdict = ritual.verify_meanings({1: true_keep}, gscenes,
                                         day_text=gdayt, skeleton_text=gskel)
        check("golden: validator override — true keep cannot be flagged",
              verdict[1]["verdict"] == "OK"
              and "anchor gate" in verdict[1]["note"])
        # and the same FABRICATED stands when no evidence supports it
        _rq.post = lambda *a, **k: _FakeResp()
        verdict2 = ritual.verify_meanings({1: true_keep}, gscenes)  # legacy: no evidence
        check("legacy mode: model read stands alone (no gate)",
              verdict2[1]["verdict"] == "FABRICATED")
        # a genuinely fabricated keep (anchors nowhere) still gets flagged
        fab_keep = "I met Gandalf at the lake and he knighted me with a spoon"
        check("anchor extraction: fabricated keep has no support",
              not rr.anchors_supported(fab_keep, gdayt, gskel))
    finally:
        _rq.post = orig_post
else:
    check("golden chronicle present", False, GOLDEN)

# --- 10. config gate ----------------------------------------------------------------
mode, k = rr.review_config({})
check("config: absent key = legacy mode", mode == "")
mode2, k2 = rr.review_config({"continua": {"ritual": {"review": {
    "mode": "skeleton", "budget_chars": "9000"}}}})
check("config: yaml mode + knobs parse", mode2 == "skeleton" and k2["budget_chars"] == 9000)

print()
print("ALL CHECKS PASSED" if not fails else f"FAILED: {fails}")
if __name__ == "__main__":
    sys.exit(1 if fails else 0)
