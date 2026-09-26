"""phase5_test.py — agent-free tests for phase 5: the quarterly heartbeat
(material collection, question parsing, read-only guarantee), the two recall
surfaces (ordinary contract vs deep), and the bookmark producer + pulse
wiring.

Run:  python3 phase5_test.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import heartbeat as hb
import index as ix
import recall as rc
import bookmark as bm
import chronicle as ch
import ritual as rt


def _write_mirror(root, instance, person_id, day, turns):
    d = os.path.join(root, instance, person_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{day}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for t in turns:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")


def _rec(uid, ts, person_id, role="user", content="hello", **kw):
    r = {"uid": uid, "ts": ts, "instance": "residenta", "person_id": person_id,
         "role": role, "content": content, "bookmark": False}
    r.update(kw)
    return r


class TestHeartbeatMaterial(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_marks = hb.MARKS_DIR
        hb.MARKS_DIR = os.path.join(self.tmp, "marks")

    def tearDown(self):
        hb.MARKS_DIR = self.old_marks

    def _write_marks(self, day, meanings):
        d = os.path.join(hb.MARKS_DIR, "residenta")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{day}.jsonl"), "w") as f:
            for i, mtext in enumerate(meanings):
                f.write(json.dumps({
                    "schema_version": 1, "date": day, "instance": "residenta",
                    "scene_id": i + 1, "uids": [], "persons": ["1000000001"],
                    "kept": True, "meaning": mtext,
                    "verification": {"verdict": "OK"}, "model": "m"}) + "\n")

    def test_quarter_window_filters(self):
        self._write_marks("2026-08-01", ["before the window"])
        self._write_marks("2026-09-01", ["within the window"])
        self._write_marks("2026-10-01", ["after the window"])
        marks = hb.quarter_marks("residenta", "2026-08-15", "2026-09-15")
        self.assertEqual([m["meaning"] for m in marks],
                         ["within the window"])

    def test_collect_material_includes_recollections(self):
        from unittest.mock import patch
        self._write_marks("2026-09-07", ["kept: the naming"])
        value = {"job": "a", "event_end": "2026-09-07", "text": "I remember naming it."}
        with patch.object(hb.memories, 'read_revisions', return_value=[value]):
            material = hb.collect_material("residenta", "2026-09-01", "2026-09-15")
        self.assertEqual(material["recollections"], [value])
        self.assertNotIn("books", material)
        self.assertEqual(len(material["marks"]), 1)

    def test_parse_questions_tolerant(self):
        qs = hb.parse_questions("Let me think.\nQ: Am I still honest?\n"
                                "q- Do I keep what matters?\n"
                                "Q3 This line lacks punctuation")
        self.assertEqual(len(qs), 3)
        self.assertEqual(qs[0], "Am I still honest?")


class TestRecallSurfaces(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="chronicle-p5-")
        self.db = os.path.join(tempfile.mkdtemp(), "chronicle.db")
        _write_mirror(self.root, "residenta", "1000000001", "2026-09-06", [
            _rec("b1", "2026-09-06T10:00:00-07:00", "1000000001",
                 content="Alex asked about the continuity plan")])
        _write_mirror(self.root, "residenta", "8737936808", "2026-09-05", [
            _rec("o1", "2026-09-05T09:00:00-07:00", "8737936808",
                 content="the toddler sleep question")])
        ix.rebuild(root=self.root, db_path=self.db)

    def test_ordinary_is_person_filtered(self):
        res = rc.recall("continuity", "residenta", "1000000001", db_path=self.db)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["mode"], "recall")
        res = rc.recall("toddler", "residenta", "1000000001", db_path=self.db)
        self.assertEqual(res, [])  # ordinary recall: other person stays out

    def test_deep_is_complete_and_marked(self):
        res = rc.deep_recall("toddler", "residenta", db_path=self.db)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["mode"], "deep")
        self.assertEqual(res[0]["person_id"], "8737936808")
        # deep recall crosses persons without dropping attribution
        self.assertIn("person-", res[0]["attribution"])


class TestBookmarkProducer(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="chronicle-bm-")
        self.tmp = tempfile.mkdtemp()
        self.old = bm.BOOKMARKS_DIR
        bm.BOOKMARKS_DIR = os.path.join(self.tmp, "bookmarks")
        _write_mirror(self.root, "residenta", "1000000001", "2026-09-07", [
            _rec("bm1", "2026-09-07T08:00:00-07:00", "1000000001",
                 content="remember this moment")])

    def tearDown(self):
        bm.BOOKMARKS_DIR = self.old

    def test_bookmark_append_and_apply(self):
        entry = bm.bookmark("residenta", "bm1", by="Alex", note="the naming",
                            root=self.root)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["by"], "Alex")
        # append-only: a second stamp coexists
        bm.bookmark("residenta", "bm1", by="her", note="yes", root=self.root)
        self.assertEqual(len(bm.bookmarks_for("residenta", "2026-09-07")), 2)
        # pulse wiring: flags the record in-memory, chronicle untouched
        recs = list(ch.iter_records(
            os.path.join(self.root, "residenta", "1000000001", "2026-09-07.jsonl")))
        self.assertFalse(recs[0]["bookmark"])  # before
        flagged = bm.apply_to_records(recs, "residenta", "2026-09-07")
        self.assertTrue(flagged[0]["bookmark"])  # after — in-memory only
        after = list(ch.iter_records(
            os.path.join(self.root, "residenta", "1000000001", "2026-09-07.jsonl")))
        self.assertFalse(after[0]["bookmark"])  # chronicle never rewritten

    def test_unknown_uid_fail_open(self):
        self.assertIsNone(bm.bookmark("residenta", "nope", root=self.root))

    def test_pulse_sees_bookmarked_scene(self):
        bm.bookmark("residenta", "bm1", root=self.root)
        import ritual as rt
        records = list(ch.iter_records(
            os.path.join(self.root, "residenta", "1000000001", "2026-09-07.jsonl")))
        records = bm.apply_to_records(records, "residenta", "2026-09-07")
        scenes = rt.segment_scenes(records)
        block, meta = rt.build_scenes_block(scenes, {})
        self.assertIn("[BOOKMARKED]", block)
        self.assertEqual(meta["bookmarked"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
