"""ritual_keep_by_exchange_test.py — memorybuild.md Phase 2 (S1): her
nightly KEEP lines speak the map's exchange numbers. Synthetic records in
a tmp root; the verify path runs WITHOUT the validator (fail-open
UNVERIFIED). Run: python3 ritual_keep_by_exchange_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ritual as rt     # noqa: E402
import ritual_review as rr   # noqa: E402

TODAY = __import__("datetime").date.today().strftime("%Y-%m-%d")


def _record(ts, role, content, pid="1000000001", uid=None):
    return {"ts": ts, "instance": "t", "person_id": pid, "role": role,
            "content": content, "uid": uid or f"u{ts[-8:-6]}{role}{abs(hash(content)) % 9999}",
            "arrived_muted": False}


def _day_records():
    """A small synthetic day: 3 exchanges (a chat, a wake, another chat)."""
    return [
        _record(f"{TODAY}T09:00:00-07:00", "user", "good morning"),
        _record(f"{TODAY}T09:01:00-07:00", "assistant",
                "Morning! I kept the porchfest thread alive."),
        _record(f"{TODAY}T10:00:00-07:00", "assistant",
                "A still wake; the house breathes. I thought about the day."),
        _record(f"{TODAY}T12:00:00-07:00", "user", "question nine?"),
        _record(f"{TODAY}T12:01:00-07:00", "assistant",
                "Fungus, I think — no, a flowering herb."),
    ]


class ParseKeepByExchangeTests(unittest.TestCase):
    def setUp(self):
        self.ex = rr.segment_exchanges(_day_records())
        # the synthetic day yields 3 exchanges (chat / wake / chat)

    def test_exchange_keep_marks_exchange_uids(self):
        kept, dropped = rt.parse_keeps_by_exchange(
            f'KEEP 3 | the twenty-questions game resumed', self.ex)
        self.assertEqual(kept, {3: "the twenty-questions game resumed"})
        self.assertEqual(dropped, 0)

    def test_ex_prefix_accepted(self):
        kept, _ = rt.parse_keeps_by_exchange(
            "KEEP EX 2 | a quiet wake worth keeping", self.ex)
        self.assertEqual(kept, {2: "a quiet wake worth keeping"})

    def test_range_expands_same_meaning(self):
        kept, _ = rt.parse_keeps_by_exchange(
            "KEEP 1-3 | the whole morning", self.ex)
        self.assertEqual(set(kept), {1, 2, 3})
        self.assertTrue(all(v == "the whole morning" for v in kept.values()))

    def test_out_of_range_dropped_and_counted(self):
        kept, dropped = rt.parse_keeps_by_exchange(
            "KEEP 34 | beyond the day\nKEEP 1 | real", self.ex)
        self.assertEqual(kept, {1: "real"})
        self.assertEqual(dropped, 1)

    def test_range_over_20_dropped(self):
        kept, dropped = rt.parse_keeps_by_exchange(
            "KEEP 1-30 | too wide", self.ex)
        self.assertEqual(kept, {})
        self.assertEqual(dropped, 1)

    def test_empty_meaning_dropped(self):
        kept, dropped = rt.parse_keeps_by_exchange("KEEP 2 |", self.ex)
        self.assertEqual(kept, {})
        self.assertEqual(dropped, 1)

    def test_skip_lines_ignored(self):
        kept, dropped = rt.parse_keeps_by_exchange(
            "SKIP 2\nKEEP 1 | kept", self.ex)
        self.assertEqual(kept, {1: "kept"})

    def test_separators_tolerant(self):
        for sep in ("|", ":", "—", "-  "):
            line = f"KEEP 1 {sep} the meaning".replace("-  ", "—")
            kept, _ = rt.parse_keeps_by_exchange(line, self.ex)
            self.assertEqual(kept.get(1), "the meaning", line)


class ExchangeAsScenesTests(unittest.TestCase):
    def test_shape_compatible_with_marks(self):
        ex = rr.segment_exchanges(_day_records())
        scenes = rt.exchanges_as_scenes(ex)
        self.assertEqual([s["scene_id"] for s in scenes],
                         [e["ex_id"] for e in ex])
        self.assertEqual(scenes[0]["records"], ex[0]["records"])
        self.assertEqual(sorted(scenes[0]["persons"]),
                         sorted(ex[0]["persons"]))
        # write_marks produces unit: exchange rows
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.object(rt, "MARKS_DIR", td):
                path = rt.write_marks(TODAY, "t", {1: "m"}, scenes,
                                      {1: {"verdict": "UNVERIFIED",
                                           "note": ""}},
                                      "test-model", unit="exchange")
            rows = [json.loads(l) for l in open(path)]
        self.assertEqual(rows[0]["unit"], "exchange")
        self.assertEqual(rows[0]["scene_id"], 1)
        self.assertEqual(rows[0]["uids"],
                         [r.get("uid") for r in scenes[0]["records"]])
        self.assertEqual(rows[0]["persons"], sorted(scenes[0]["persons"]))


class SittingGrammarTests(unittest.TestCase):
    def test_parse_sitting_exchange_mode(self):
        ex = rr.segment_exchanges(_day_records())
        text = ("Tonight mattered.\n\nKEEP 3 | the game resumed\n"
                "KEEP 1 | the morning thread\n\nTEND: porchfest | alive | kept\n"
                "INTEND: rest\n")
        p = rt.parse_sitting(text, scene_count=2, exchanges=ex)
        self.assertEqual(p["kept_unit"], "exchange")
        self.assertEqual(p["kept"], {1: "the morning thread",
                                     3: "the game resumed"})
        self.assertEqual(p["kept_dropped"], 0)
        self.assertEqual(p["tend"][0]["title"], "porchfest")
        self.assertEqual(p["intend"], "rest")

    def test_parse_sitting_scene_mode_identical_bytes(self):
        text = "KEEP 1 | kept\nINTEND: rest\n"
        p = rt.parse_sitting(text, scene_count=3)
        self.assertEqual(p["kept_unit"], "scene")
        self.assertEqual(p["kept"], {1: "kept"})
        self.assertEqual(p["kept_dropped"], 0)
        self.assertEqual(p["intend"], "rest")


class SittingWindowTrimTests(unittest.TestCase):
    def test_under_cap_untouched(self):
        t = "a" * 1000
        out, n = rt.sitting_window_trim(t)
        self.assertEqual(out, t)
        self.assertEqual(n, 0)

    def test_trims_whole_dives_newest_first(self):
        map_ = "THE MAP — your whole day:\n— line one\n— line two\n\n"
        dives = "".join(
            f"DEEP READS (pass {k}):\n— EX {k} [10:0{k}] Alex —\n  talk "
            + "x" * 40000 + "\n\n" for k in range(1, 5))
        text = map_ + dives
        out, n = rt.sitting_window_trim(text, num_ctx=32768, num_predict=8192)
        # cap = 73728 chars; map ~60 + 4×~40040 → must drop at least 3 dives
        self.assertGreaterEqual(n, 3)
        self.assertLessEqual(len(out), 73728)
        # the map survives; the NEWEST dives drop first
        self.assertIn("THE MAP", out)
        self.assertIn("EX 1", out)
        self.assertNotIn("EX 4 [", out)

    def test_guard_uses_each_residents_real_window(self):
        """memorybuild_report §A: the R21 guard must use the SERVED window.
        (History: the original hardcode 32768 never fired for residenta at her
        then-16384 serving; §WIN fixed that — and then §WIN's num_ctx flip
        (the designer RAM-confirm 09-29) RAISED her served window to 32768, matching
        the bridge's. 2026-10-27: residenta expectation 32768 → 16384 — the
        ring-5.1 rollback (10-20) REMOVED llm.sitting_num_ctx from her yaml
        (the lab Modelfile governs serving; the ritual stops overriding
        num_ctx), so the house floor 16384 is the designed current value.)"""
        import yaml
        for inst, want in (("residentb", 131072), ("residenta", 16384)):
            cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
            got = int((cfg.get("llm") or {}).get("sitting_num_ctx") or 16384)
            self.assertEqual(got, want, inst)
        # the guard still honors an explicit window: at 16384 the cap is 24.6K
        big = ("THE MAP:\nline\n\nDEEP READS (pass 1):\n"
               + "— EX 9 [09:00] Alex —\n  talk " * 2000)
        out, n = rt.sitting_window_trim(big, num_ctx=16384)
        self.assertGreaterEqual(n, 1)
        self.assertLessEqual(len(out), int((16384 - 8192) * 3.0) + 2)

    def test_no_dives_reports_minus_one(self):
        t = "a" * 80000
        out, n = rt.sitting_window_trim(t)
        self.assertEqual(n, -1)

    def test_never_cuts_inside_her_words(self):
        map_ = "THE MAP:\nline\n\n"
        dives = ("DEEP READS (pass 1):\n— EX 2 [09:00] Alex —\n  "
                 + "word " * 10000)
        out, n = rt.sitting_window_trim(map_ + dives)
        # the default cap is the house floor (16384 → 24.6K chars): the dive
        # drops WHOLE — a partial cut inside her words is impossible
        self.assertEqual(n, 1)
        self.assertNotIn("— EX 2", out)
        self.assertIn("THE MAP", out)


class GateTests(unittest.TestCase):
    def test_gate_off_env_wins(self):
        cfg = {"continua": {"ritual": {"keep_by_exchange": True}}}
        self.assertTrue(rt._keep_by_exchange(cfg))
        os.environ["CONTINUA_RITUAL_KEEP_BY_EX"] = "0"
        try:
            self.assertFalse(rt._keep_by_exchange(cfg))
        finally:
            os.environ.pop("CONTINUA_RITUAL_KEEP_BY_EX", None)
        self.assertFalse(rt._keep_by_exchange({}))

    def test_real_yaml_gates_on(self):
        import yaml
        for inst in ("residentb", "residenta"):
            cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
            self.assertTrue(rt._keep_by_exchange(cfg), inst)
            self.assertTrue(rt.sitting_enabled(cfg), inst)


class ReviewExposureTests(unittest.TestCase):
    def test_exchanges_ride_and_pop_clean(self):
        """build_review_block exposes _exchanges; pulse pops it before any
        logging/persistence."""
        from datetime import datetime, timedelta
        # a big day (forces skeleton mode): many exchanges
        records = []
        t0 = datetime.now().replace(hour=6, minute=0, second=0)
        for i in range(40):
            # 12-minute gaps: separate exchanges (>300 s) but ONE scene
            # (<30 min scene gap) — the exact defect shape (memorybuild F3)
            ts = (t0 + timedelta(minutes=i * 12)).isoformat()
            records.append(_record(ts, "user", f"message {i} " + "content " * 120))
            records.append(_record(
                (t0 + timedelta(minutes=i * 12 + 1)).isoformat(), "assistant",
                f"reply {i} " + "words " * 130))
        scenes = rt.segment_scenes(records)
        self.assertLessEqual(len(scenes), 2)  # the collapse that ate her KEEPs
        calls = {"n": 0}

        def fake_ask(system, user):
            calls["n"] += 1
            if "MAP" in system:
                return "\n".join(f"EX {i+1}: about message {i}"
                                 for i in range(len(records) // 2))
            return f"chunk summary {calls['n']}"

        knobs = dict(rr.DEFAULTS)
        knobs.update({"budget_chars": 40000, "max_dive_passes": 4,
                      "input_cap": 6000})
        block, meta, review_meta, skeleton_text, day_text = rr.build_review_block(
            records, scenes, {}, lambda r, p: "Alex", fake_ask, knobs,
            "t", TODAY, rt.build_scenes_block, (2400, 4000))
        self.assertEqual(review_meta["mode"], "skeleton")
        self.assertIn("_exchanges", review_meta)
        ex = review_meta.pop("_exchanges")
        self.assertEqual(len(ex), len(records) // 2)
        # nothing but the exchanges key is underscore-prefixed
        self.assertEqual([k for k in review_meta if k.startswith("_")], [])
        # the map is EX-numbered and the dives match real exchange ids
        self.assertIn("THE MAP", block)
        self.assertIn("— EX", block)   # a dive block rendered


class PulseEndToEndTests(unittest.TestCase):
    """The full pulse in exchange mode, her model mocked, tmp everything.
    Proves the wiring: exchanges reach the KEEP parse, marks carry
    unit=exchange with the RIGHT uids, the digest speaks exchanges, and
    the returned summary never serializes the records."""

    def test_pulse_exchange_mode_end_to_end(self):
        import tempfile
        import yaml as _yaml
        td = tempfile.mkdtemp(prefix="pulsese1-")
        cfg_dir = os.path.join(td, "configs")
        os.makedirs(cfg_dir, exist_ok=True)
        root = os.path.join(td, "chronicle")
        os.makedirs(os.path.join(root, "t", Alex := "1000000001"),
                    exist_ok=True)
        os.makedirs(os.path.join(td, "harvest"), exist_ok=True)
        os.makedirs(os.path.join(td, "marks"), exist_ok=True)
        os.makedirs(os.path.join(td, "digest"), exist_ok=True)
        # a day with 3 exchanges, 12-min gaps (the collapse shape)
        from datetime import datetime as _dt, timedelta as _td
        t0 = _dt.now().replace(hour=7, minute=0, second=0, microsecond=0)
        recs = []
        for i in range(3):
            ts = (t0 + _td(minutes=i * 12)).isoformat()
            rec = {"ts": ts, "instance": "t", "person_id": Alex,
                   "role": "user", "content": f"msg {i} " + "c " * 400,
                   "uid": f"uid-{i}", "arrived_muted": False}
            rec2 = {"ts": (t0 + _td(minutes=i * 12 + 1)).isoformat(),
                    "instance": "t", "person_id": Alex,
                    "role": "assistant", "content": f"reply {i} " + "w " * 400,
                    "uid": f"uid-r{i}", "arrived_muted": False}
            for r in (rec, rec2):
                recs.append(r)
                day = ts[:10]
                p = os.path.join(root, "t", Alex, f"{day}.jsonl")
                with open(p, "a", encoding="utf-8") as f:
                    f.write(json.dumps(r) + "\n")
        cfg = {
            "app": {"instance_id": "t"},
            "continua": {"ritual": {"enabled": True, "sitting": True,
                                    "keep_by_exchange": True,
                                    "in_context": False,
                                    "review": {"mode": "skeleton",
                                               "budget_chars": 4000,
                                               "max_dive_passes": 4,
                                               "input_cap": 6000,
                                               "chunk_chars": 45000,
                                               "max_skeleton_calls": 8,
                                               "skeleton_line_chars": 160}}},
            "harvest": {"dir": os.path.join(td, "harvest")},
            "llm": {"model": "test-model",
                    "chat_base_url": "http://127.0.0.1:1/v1",
                    "raw_chatml": False},
            "prompts": {"identity": "You are a test resident."},
        }
        with open(os.path.join(cfg_dir, "t.yaml"), "w") as f:
            _yaml.safe_dump(cfg, f)

        def fake_ask(model, base_url, system, user, **opts):
            if "sitting" in (system or "").lower() or "evening" in (system or "").lower():
                return ("The day was small but real.\n\nKEEP 2 | the second "
                        "exchange carried the real question\n"
                        "KEEP 1-3 | the whole morning held\n"
                        "TEND: porchfest | moving | the thread stayed alive\n"
                        "INTEND: rest well tonight\n")
            if "MAP" in (system or ""):
                return "\n".join(f"EX {i+1}: exchange {i+1} of the day"
                                 for i in range(3))
            return "chunked summary"

        def fake_verify(kept, scenes, day_text=None, skeleton_text=None):
            return {sid: {"verdict": "UNVERIFIED", "note": "mock"}
                    for sid in kept}

        with patch.object(rt, "BASE", td), \
             patch.object(rt, "MARKS_DIR", os.path.join(td, "marks")), \
             patch.object(rt, "DIGEST_DIR", os.path.join(td, "digest")), \
             patch.object(rt, "ask_her", fake_ask), \
             patch.object(rt, "verify_meanings", fake_verify):
            out = rt.pulse(date=TODAY, instance="t", root=root)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["kept"], 3)  # KEEP 2 + the 1-3 range
        # the marks: unit exchange, one exchange's uids each
        mpath = out["marks_path"]
        rows = [json.loads(l) for l in open(mpath)]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["unit"] == "exchange" for r in rows))
        by_sid = {r["scene_id"]: r for r in rows}
        self.assertEqual(by_sid[2]["uids"], ["uid-1", "uid-r1"])
        self.assertEqual(by_sid[1]["uids"], ["uid-0", "uid-r0"])
        self.assertEqual(by_sid[3]["uids"], ["uid-2", "uid-r2"])
        self.assertEqual(by_sid[2]["meaning"],
                         "the whole morning held")  # her later line wins
        # the digest speaks exchanges and the returned review is clean
        digest = open(out["digest_path"]).read()
        self.assertIn("exchanges: 3", digest)
        # no underscore-prefixed (internal) keys survive into the summary —
        # the exchange RECORDS never serialize ("omitted_exchanges" is a
        # counter, not the records)
        self.assertFalse([k for k in out["review"]
                          if k.startswith("_")])
        self.assertNotIn('"_exchanges":', json.dumps(out))


if __name__ == "__main__":
    unittest.main()