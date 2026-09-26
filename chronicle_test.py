"""chronicle_test.py — agent-free tests for the Continua mirror (phase 1).

Pre-build checklist ruling #3: tests run without an agent where possible.
These cover the mirror schema, append (incl. kill switch + fail-open),
the length_cut instrument (finish_reason + legacy heuristic), and the
harvest backfill (mapping + idempotency) — all pure stdlib, no LLM.

Run:  python3 chronicle_test.py
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chronicle as ch


class TestUid(unittest.TestCase):
    def test_uid_matches_harvest_derivation(self):
        """Byte-compatible with harvest_hook.py: sha1(ts|role|content[:160])."""
        import hashlib
        ts, role, content = "2026-09-06T11:28:00-07:00", "assistant", "hello world"
        expected = hashlib.sha1(
            f"{ts}|{role}|{content[:160]}".encode()).hexdigest()[:12]
        self.assertEqual(ch._uid(ts, role, content), expected)

    def test_uid_stable_and_short(self):
        u1 = ch._uid("t", "user", "x" * 500)
        u2 = ch._uid("t", "user", "x" * 500)
        self.assertEqual(u1, u2)
        self.assertEqual(len(u1), 12)


class TestLengthCut(unittest.TestCase):
    def test_legacy_mid_sentence_detected(self):
        """The num_predict cliff fixture: turn 6 ended 'Wait, let'."""
        self.assertTrue(ch.mid_sentence_tail("...a long answer that ends Wait, let"))

    def test_legacy_clean_endings(self):
        for ok in ("Done.", "Really?", "Wow!", "…", 'He said "fine."',
                   "(like this.)", "它结束了。",
                   # emoji sign-offs are her voice — complete, not cuts
                   "The dance continues. \U0001F332\u2728",
                   "want to do for you today? \U0001F60A",
                   # XML structural endings are parseable, not mid-prose
                   "save the memory</parameter>\n</call>",
                   "thinking done\n</think>"):
            self.assertFalse(ch.mid_sentence_tail(ok), msg=ok)

    def test_legacy_mid_word(self):
        self.assertTrue(ch.mid_sentence_tail("I was just typin"))

    def test_empty_is_not_cut(self):
        self.assertFalse(ch.mid_sentence_tail(""))
        self.assertFalse(ch.mid_sentence_tail("   "))

    def test_new_line_finish_reason_length(self):
        self.assertTrue(ch.compute_length_cut("assistant", "any text", "length"))

    def test_new_line_finish_reason_stop(self):
        self.assertFalse(ch.compute_length_cut("assistant", "any text", "stop"))

    def test_legacy_assistant_mid_sentence(self):
        self.assertTrue(ch.compute_length_cut("assistant", "ends Wait, let", None))

    def test_user_never_cut(self):
        self.assertFalse(ch.compute_length_cut("user", "ends Wait, let", "length"))
        self.assertFalse(ch.compute_length_cut("user", "ends Wait, let", None))


class TestAppend(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="chronicle-test-")

    def _rec(self, **kw):
        rec = dict(ts="2026-09-07T10:00:00-07:00", instance="residenta",
                   person_id="1000000001", role="assistant", content="Hi Alex.")
        rec.update(kw)
        return rec

    def test_roundtrip_and_schema(self):
        out = ch.append(self._rec(finish_reason="stop", eval_count=42,
                                  model="testmodel-gpu:latest"), root=self.root)
        self.assertIsNotNone(out)
        self.assertEqual(out["schema_version"], 1)
        path = ch.day_path(self.root, "residenta", "1000000001", "2026-09-07")
        recs = list(ch.iter_records(path))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["person_id"], "1000000001")   # person-tagged
        self.assertEqual(r["finish_reason"], "stop")
        self.assertFalse(r["length_cut"])
        self.assertIsNone(r["salience"])                  # reserved for the Ritual
        self.assertEqual(r["source"], "live")
        self.assertIsNone(r["harvest_path"])
        self.assertEqual(r["model"], "testmodel-gpu:latest")

    def test_user_line_has_no_assistant_fields(self):
        ch.append(self._rec(role="user", content="hello"), root=self.root)
        path = ch.day_path(self.root, "residenta", "1000000001", "2026-09-07")
        r = list(ch.iter_records(path))[0]
        self.assertEqual(r["role"], "user")
        self.assertIsNone(r["finish_reason"])
        self.assertFalse(r["length_cut"])

    def test_length_cut_computed_not_trusted(self):
        out = ch.append(self._rec(finish_reason="length", **{}), root=self.root)
        self.assertTrue(out["length_cut"])

    def test_kill_switch_is_noop(self):
        with patch.dict(os.environ, {"CONTINUA_CAPTURE": "0"}):
            self.assertIsNone(ch.append(self._rec(), root=self.root))
        path = ch.day_path(self.root, "residenta", "1000000001", "2026-09-07")
        self.assertFalse(os.path.exists(path))

    def test_fail_open_on_bad_record(self):
        # missing required field → swallowed, returns None, never raises
        bad = {"ts": "2026-09-07T10:00:00-07:00", "instance": "residenta"}
        self.assertIsNone(ch.append(bad, root=self.root))

    def test_fail_open_on_unwritable_root(self):
        blocker = os.path.join(self.root, "not-a-dir")
        open(blocker, "w").close()
        self.assertIsNone(ch.append(self._rec(), root=blocker))


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="chronicle-backfill-")
        self.harvest = tempfile.mkdtemp(prefix="harvest-test-")
        lines = [
            {"ts": "2026-09-06T11:20:00-07:00", "user": "1000000001",
             "session": "2026-09-06", "instance": "residenta", "turn": 1,
             "role": "user", "content": "Do you want continuity?",
             "memory_injection": None, "reasoning_content": None,
             "uid": "aaaaaaaaaaaa", "frame_density": 0.0,
             "think_status": None},
            {"ts": "2026-09-06T11:28:00-07:00", "user": "1000000001",
             "session": "2026-09-06", "instance": "residenta", "turn": 2,
             "role": "assistant",
             "content": "A long answer ending Wait, let",
             "reasoning_content": "thinking...", "latency_s": 273.0,
             "memory_injection": "some memory",
             "uid": "bbbbbbbbbbbb", "frame_density": 0.4,
             "think_status": "ok"},
        ]
        self.harvest_file = os.path.join(
            self.harvest, "residenta_1000000001_2026-09-06.jsonl")
        with open(self.harvest_file, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def test_mapping(self):
        n = ch.backfill_harvest_file(self.harvest_file, root=self.root)
        self.assertEqual(n, 2)
        path = ch.day_path(self.root, "residenta", "1000000001", "2026-09-06")
        recs = list(ch.iter_records(path))
        by_uid = {r["uid"]: r for r in recs}
        # user line: person-tagged, no assistant fields, not cut
        u = by_uid["aaaaaaaaaaaa"]
        self.assertEqual(u["role"], "user")
        self.assertEqual(u["person_id"], "1000000001")
        self.assertFalse(u["length_cut"])
        # assistant legacy line: mid-sentence → length_cut True (the cliff)
        a = by_uid["bbbbbbbbbbbb"]
        self.assertTrue(a["length_cut"])
        self.assertIsNone(a["finish_reason"])          # legacy
        self.assertEqual(a["reasoning"], "thinking...")
        self.assertEqual(a["memory_injection"], "some memory")
        self.assertEqual(a["source"], "harvest-backfill")
        self.assertEqual(a["harvest_path"], "residenta_1000000001_2026-09-06.jsonl")
        self.assertIsNone(a["salience"])
        self.assertNotIn("frame_density", a)           # training annotation stays out
        self.assertNotIn("think_status", a)

    def test_idempotent_by_uid(self):
        ch.backfill_harvest_file(self.harvest_file, root=self.root)
        n2 = ch.backfill_harvest_file(self.harvest_file, root=self.root)
        self.assertEqual(n2, 0)
        path = ch.day_path(self.root, "residenta", "1000000001", "2026-09-06")
        self.assertEqual(len(list(ch.iter_records(path))), 2)

    def test_unrecognized_filename_rejected(self):
        bogus = os.path.join(self.harvest, "not-a-harvest-file.jsonl")
        with open(bogus, "w") as f:
            f.write("{}\n")
        self.assertEqual(ch.backfill_harvest_file(bogus, root=self.root), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
