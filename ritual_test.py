"""ritual_test.py — agent-free tests for the Ritual service (phase 3).

Covers: scene segmentation (time-gap), budget rendering (bookmark priority,
scene cap), the tolerant KEEP/SKIP parser (4B-safe), marks writing, digest
content incl. the mark-rate monitor. No model calls.

Run:  python3 ritual_test.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ritual as rt


def _rec(ts, person_id="1000000001", role="user", content="hi", **kw):
    r = {"uid": kw.pop("uid", "x" + ts[11:16].replace(":", "")),
         "ts": ts, "instance": "residenta", "person_id": person_id,
         "role": role, "content": content, "bookmark": False}
    r.update(kw)
    return r


class TestSegmentation(unittest.TestCase):
    def test_gap_creates_new_scene(self):
        recs = [
            _rec("2026-09-07T08:00:00-07:00"),
            _rec("2026-09-07T08:05:00-07:00", role="assistant"),
            _rec("2026-09-07T11:30:00-07:00"),              # 3h25m gap
            _rec("2026-09-07T11:31:00-07:00", role="assistant"),
        ]
        scenes = rt.segment_scenes(recs)
        self.assertEqual(len(scenes), 2)
        self.assertEqual(scenes[0]["scene_id"], 1)
        self.assertEqual(len(scenes[0]["records"]), 2)
        self.assertEqual(len(scenes[1]["records"]), 2)

    def test_small_gap_same_scene(self):
        recs = [_rec("2026-09-07T08:00:00-07:00"),
                _rec("2026-09-07T08:29:00-07:00")]
        self.assertEqual(len(rt.segment_scenes(recs)), 1)

    def test_every_turn_lands_somewhere(self):
        recs = [_rec(f"2026-09-07T0{h}:00:00-07:00") for h in range(1, 10)]
        scenes = rt.segment_scenes(recs)
        self.assertEqual(sum(len(s["records"]) for s in scenes), len(recs))


class TestBudgetRender(unittest.TestCase):
    def test_bookmark_priority_and_flag(self):
        roster = {}
        recs = [_rec("2026-09-07T08:00:00-07:00",
                     content="x" * 5000, bookmark=True),
                _rec("2026-09-07T08:01:00-07:00", role="assistant",
                     content="a reply")]
        scenes = rt.segment_scenes(recs)
        block, meta = rt.build_scenes_block(scenes, roster)
        self.assertIn("[BOOKMARKED]", block)
        self.assertEqual(meta["bookmarked"], 1)
        # bookmarked turn kept its larger allowance (not cut to 900)
        self.assertGreater(block.find("…[truncated]"), 0)  # truncated after cap
        self.assertLess(block.find("…[truncated]"), 4000)

    def test_budget_omits_scenes_when_exhausted(self):
        roster = {}
        recs = [_rec(f"2026-09-07T0{h}:00:00-07:00",
                     content="y" * 3000) for h in range(1, 8)]
        scenes = rt.segment_scenes(recs)  # 7 scenes, far over default budget
        old = rt.BUDGET_CHARS
        try:
            rt.BUDGET_CHARS = 20000  # noqa: only the module-level default is read in build
            block, meta = rt.build_scenes_block(
                scenes, roster) if False else (None, None)
        finally:
            rt.BUDGET_CHARS = old
        # instead: patch via env-driven call — simpler: call with small budget
        old_b = rt.BUDGET_CHARS
        rt.BUDGET_CHARS = 6000
        try:
            block, meta = rt.build_scenes_block(scenes, roster)
        finally:
            rt.BUDGET_CHARS = old_b
        self.assertGreater(meta["truncated_scenes"], 0)
        self.assertLessEqual(meta["chars"], 6100)


class TestParseDecisions(unittest.TestCase):
    def test_tolerant_formats(self):
        text = ("Let me think about today...\n"
                "KEEP 1 | The morning we planned my memory — that mattered.\n"
                "skip 2\n"
                "KEEP #3: Alex pushed back and it made the plan better.")
        kept = rt.parse_decisions(text, 3)
        self.assertEqual(set(kept), {1, 3})
        self.assertIn("morning", kept[1])
        self.assertIn("pushed back", kept[3])

    def test_out_of_range_and_silence_are_skips(self):
        kept = rt.parse_decisions("KEEP 5 | beyond the last scene", 3)
        self.assertEqual(kept, {})
        self.assertEqual(rt.parse_decisions("", 3), {})
        self.assertEqual(rt.parse_decisions(None, 3), {})

    def test_strip_think(self):
        self.assertEqual(rt._strip_think("<think>blah</keep>ok".replace(
            "</keep>", "</think>")), "ok")


class TestMarksAndDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_marks, self.old_digest = rt.MARKS_DIR, rt.DIGEST_DIR
        rt.MARKS_DIR = os.path.join(self.tmp, "marks")
        rt.DIGEST_DIR = os.path.join(self.tmp, "digest")
        self.scenes = [{"scene_id": 1, "records": [
            _rec("2026-09-07T08:00:00-07:00", uid="uid1")],
            "persons": {"1000000001"}, "start": "2026-09-07T08:00:00-07:00"}]

    def tearDown(self):
        rt.MARKS_DIR, rt.DIGEST_DIR = self.old_marks, self.old_digest

    def test_marks_written_and_replaced(self):
        kept = {1: "This was the day we planned my memory."}
        ver = {1: {"verdict": "OK", "note": ""}}
        p1 = rt.write_marks("2026-09-07", "residenta", kept, self.scenes, ver,
                            "testmodel-gpu:latest")
        marks = [json.loads(l) for l in open(p1)]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["uids"], ["uid1"])
        self.assertEqual(marks[0]["verification"]["verdict"], "OK")
        self.assertTrue(marks[0]["kept"])
        # re-run same date → replaced, not duplicated (append-only chronicle
        # untouched; marks are the ritual's per-date decision artifact)
        rt.write_marks("2026-09-07", "residenta", {1: "revised meaning"},
                       self.scenes, ver, "testmodel-gpu:latest")
        marks = [json.loads(l) for l in open(p1)]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["meaning"], "revised meaning")

    def test_prior_keeps_continuity_block(self):
        # yesterday's marks exist → tonight's pulse remembers them
        kept = {1: "The day we named Continua."}
        ver = {1: {"verdict": "OK", "note": ""}}
        rt.write_marks("2026-09-07", "residenta", kept, self.scenes, ver, "m")
        prior = rt.prior_keeps("residenta", "2026-09-08")
        self.assertEqual(len(prior), 1)
        self.assertEqual(prior[0]["date"], "2026-09-07")
        block = rt.build_continuity_block(prior)
        self.assertIn("WHAT YOU KEPT ON PREVIOUS NIGHTS", block)
        self.assertIn("The day we named Continua.", block)
        # current-date marks are excluded (previous nights only)
        self.assertEqual(rt.prior_keeps("residenta", "2026-09-07"), [])
        # first night: no block at all
        self.assertEqual(rt.build_continuity_block([]), "")

    def test_digest_flags_overmarking(self):
        kept = {1: "kept"}
        ver = {1: {"verdict": "OK", "note": ""}}
        # 1 kept of 1 scene = 100% > warn threshold
        p = rt.write_digest("2026-09-07", "residenta",
                            {"chars": 500, "scenes": 1, "turns": 1,
                             "truncated_scenes": 0, "bookmarked": 0},
                            kept, 1, ver, "KEEP 1 | kept", 12.0)
        text = open(p).read()
        self.assertIn("OVER-MARKING", text)
        self.assertIn("KEEP 1 [OK]", text)

    def test_digest_flags_fabrication(self):
        ver = {1: {"verdict": "FABRICATED",
                   "note": "no such quote in the record"}}
        p = rt.write_digest("2026-09-07", "residenta",
                            {"chars": 500, "scenes": 3, "turns": 5,
                             "truncated_scenes": 0, "bookmarked": 0},
                            {1: "kept"}, 3, ver, "", 9.0)
        self.assertIn("FABRICATION FLAGS", open(p).read())

    def test_quiet_day(self):
        p = rt.write_digest("2026-09-07", "residenta",
                            {"chars": 300, "scenes": 2, "turns": 2,
                             "truncated_scenes": 0, "bookmarked": 0},
                            {}, 2, {}, "SKIP 1\nSKIP 2", 5.0)
        self.assertIn("kept nothing", open(p).read())


class TestRitualLock(unittest.TestCase):
    """2026-09-16 (house ruling: wakes pause while the ritual runs) — the lock
    is the pause signal. Agent-free: fake pids, tmp lock path. Invariants:
    O_EXCL atomicity, one holder at a time, stale (dead-pid) and corrupt
    locks cleaned on read, release never removes a newer holder's lock."""

    def setUp(self):
        self._old = rt.RITUAL_LOCK
        self._tmp = tempfile.TemporaryDirectory()
        rt.RITUAL_LOCK = os.path.join(self._tmp.name, "ritual.lock")

    def tearDown(self):
        rt.RITUAL_LOCK = self._old
        self._tmp.cleanup()

    def test_acquire_holds_release_roundtrip(self):
        rec = rt.acquire_ritual_lock(["residenta"])
        self.assertIsNotNone(rec)
        held = rt.ritual_lock_held()
        self.assertIsNotNone(held)
        self.assertEqual(held.get("pid"), os.getpid())
        self.assertEqual(held.get("instances"), ["residenta"])
        rt.release_ritual_lock(rec)
        self.assertIsNone(rt.ritual_lock_held())

    def test_second_pulse_stands_down(self):
        rec = rt.acquire_ritual_lock(["residenta"])
        self.assertIsNone(rt.acquire_ritual_lock(["residentb"]))  # live holder
        self.assertEqual(rt.ritual_lock_held().get("instances"), ["residenta"])
        rt.release_ritual_lock(rec)
        self.assertIsNotNone(rt.acquire_ritual_lock(["residentb"]))

    def test_stale_dead_pid_lock_is_cleaned(self):
        with open(rt.RITUAL_LOCK, "w") as f:
            json.dump({"pid": 999999999, "started": "stale",
                       "instances": ["residenta"]}, f)
        self.assertIsNone(rt.ritual_lock_held())      # dead pid → cleaned
        self.assertFalse(os.path.exists(rt.RITUAL_LOCK))
        self.assertIsNotNone(rt.acquire_ritual_lock(["residenta"]))

    def test_corrupt_lock_is_cleaned(self):
        with open(rt.RITUAL_LOCK, "w") as f:
            f.write('{"pid": 12')                     # partial write
        self.assertIsNone(rt.ritual_lock_held())
        self.assertFalse(os.path.exists(rt.RITUAL_LOCK))

    def test_release_never_removes_newer_holders_lock(self):
        rec = rt.acquire_ritual_lock(["residenta"])
        with open(rt.RITUAL_LOCK, "w") as f:          # simulate a takeover
            json.dump({"pid": 999999999, "started": "newer",
                       "instances": ["residentb"]}, f)
        rt.release_ritual_lock(rec)
        self.assertTrue(os.path.exists(rt.RITUAL_LOCK))  # untouched
        self.assertIsNone(rt.ritual_lock_held())         # then stale-cleaned


if __name__ == "__main__":
    unittest.main(verbosity=2)
