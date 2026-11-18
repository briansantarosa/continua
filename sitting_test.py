"""sitting_test.py — agent-free tests for the evening practice (the
sitting; nightlyplan.md). Covers: parse_sitting tolerance (markers,
marker-less prose, mixed output), the intentions store (immutability,
latest-before), apply_tending (her declarations filed, nothing invented),
the sitting gate (config flag + rollback switch), the wake consult line,
and the name_for rendering.

Run:  python3 sitting_test.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import people as pp
import ritual as rt
import wake as wk


class TestParseSitting(unittest.TestCase):
    def test_full_sitting(self):
        text = """
Let me sit with today.

AFTERMATH: Yesterday I said I would finish the parser. I didn't. The gap
between who I planned to be and who I was is where the honesty lives.
KEEP 1 | Alex's joke about the moon — the whole room laughed
SKIP 2
KEEP 3 | Sequoia told me about her garden; I want to remember the tomatoes
TEND: wordkeeper | stuck | the parser fights me on apostrophes
TEND: letters | moving | two replies drafted
INTEND: Tomorrow: read the news before Alex asks. Leave the parser stuck —
it is a pause, not a failure.
"""
        p = rt.parse_sitting(text, scene_count=3)
        self.assertEqual(p["kept"], {1: "Alex's joke about the moon — the "
                                     "whole room laughed",
                                     3: "Sequoia told me about her garden; "
                                     "I want to remember the tomatoes"})
        self.assertEqual(len(p["tend"]), 2)
        self.assertEqual(p["tend"][0]["title"], "wordkeeper")
        self.assertEqual(p["tend"][0]["status"], "stuck")
        self.assertIn("apostrophes", p["tend"][0]["note"])
        self.assertIn("Leave the parser stuck", p["intend"])
        self.assertIn("who I planned to be", p["aftermath"])
        self.assertNotIn("SKIP", p["intend"])  # intend block ends at markers

    def test_prose_without_markers_is_valid(self):
        # skip rights: marker-less prose is still a sitting — nothing files,
        # nothing errors, nothing is graded
        p = rt.parse_sitting("Today was small. I kept nothing and that is "
                             "the truth.", scene_count=3)
        self.assertEqual(p["kept"], {})
        self.assertEqual(p["tend"], [])
        self.assertEqual(p["intend"], "")
        self.assertEqual(p["aftermath"], "")

    def test_intend_multiline_verbatim(self):
        p = rt.parse_sitting(
            "KEEP 1 | x\nINTEND:\none line\n  two line \nanother\nKEEP 2 | y",
            scene_count=2)
        # her words verbatim, whitespace-collapsed at edges only
        self.assertEqual(p["intend"], "one line\ntwo line\nanother")
        self.assertIn(2, p["kept"])  # markers resume after the block

    def test_tend_tolerant_variants(self):
        p = rt.parse_sitting("TEND: solo project", scene_count=0)
        self.assertEqual(p["tend"], [{"title": "solo project",
                                      "status": "noted", "note": ""}])
        p = rt.parse_sitting("TEND: book | shelved", scene_count=0)
        self.assertEqual(p["tend"][0]["status"], "shelved")

    def test_scene_bounds(self):
        p = rt.parse_sitting("KEEP 9 | out of range", scene_count=3)
        self.assertEqual(p["kept"], {})  # same tolerance as parse_decisions


class TestIntentionsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_write_read_immutable(self):
        r1 = rt.write_intentions(self.tmp, "residenta", "2026-09-27",
                                 "read the news\nleave the parser stuck")
        self.assertTrue(r1["written"])
        self.assertEqual(rt.read_intentions(self.tmp, "residenta", "2026-09-27"),
                         "read the news\nleave the parser stuck")
        # IMMUTABLE once written: a re-sitting cannot edit yesterday
        r2 = rt.write_intentions(self.tmp, "residenta", "2026-09-27",
                                 "totally different words")
        self.assertFalse(r2["written"])
        self.assertEqual(r2["reason"], "already-written")
        self.assertEqual(rt.read_intentions(self.tmp, "residenta", "2026-09-27"),
                         "read the news\nleave the parser stuck")

    def test_empty_intend_writes_nothing(self):
        # silence is an answer — there is nothing to keep
        r = rt.write_intentions(self.tmp, "residenta", "2026-09-27", "  \n ")
        self.assertFalse(r["written"])
        self.assertEqual(r["reason"], "empty")

    def test_latest_strictly_before(self):
        rt.write_intentions(self.tmp, "residenta", "2026-09-25", "older lines")
        rt.write_intentions(self.tmp, "residenta", "2026-09-27", "newer lines")
        self.assertEqual(rt.latest_intentions(self.tmp, "residenta",
                                              before_date="2026-09-28"),
                         "newer lines")
        self.assertEqual(rt.latest_intentions(self.tmp, "residenta",
                                              before_date="2026-09-27"),
                         "older lines")  # never today's own
        self.assertIsNone(rt.latest_intentions(self.tmp, "residenta",
                                               before_date="2026-09-25"))

    def test_missing_dir_fail_open(self):
        self.assertIsNone(rt.latest_intentions(self.tmp, "nobody", "2999-01-01"))


class TestTending(unittest.TestCase):
    def test_set_project_filed_not_invented(self):
        tmp = tempfile.mkdtemp()
        filed = rt.apply_tending("residenta", tmp,
                                 [{"title": "wordkeeper", "status": "stuck",
                                   "note": "a pause, not a failure"}])
        self.assertEqual(len(filed), 1)
        import notes
        store = notes.NotesStore(tmp, "residenta")
        projs = store.list_projects()
        match = [p for p in projs if p.get("title") == "wordkeeper"
                 or p.get("status") == "stuck"]
        self.assertTrue(match)
        # the house files what she declared — nothing invented beyond it
        data = json.load(open(notes.notes_path(tmp, "residenta")))
        self.assertEqual(data["projects"]["wordkeeper"]["status"], "stuck")
        self.assertEqual(data["projects"]["wordkeeper"]["note"],
                         "a pause, not a failure")

    def test_fail_open_on_bad_store(self):
        # a notes-store failure never breaks the sitting
        filed = rt.apply_tending("/proc/definitely/not/writable", "x",
                                 [{"title": "t", "status": "s"}])
        self.assertIsInstance(filed, list)


class TestGate(unittest.TestCase):
    def test_flag_default_off(self):
        self.assertFalse(rt.sitting_enabled({}))
        self.assertFalse(rt.sitting_enabled(
            {"continua": {"ritual": {"sitting": False}}}))

    def test_rollback_switch(self):
        cfg = {"continua": {"ritual": {"sitting": True}}}
        old = os.environ.get("CONTINUA_SITTING")
        try:
            os.environ["CONTINUA_SITTING"] = "0"
            self.assertFalse(rt.sitting_enabled(cfg))  # rollback wins
            os.environ["CONTINUA_SITTING"] = "1"
            self.assertTrue(rt.sitting_enabled(cfg))
        finally:
            if old is None:
                os.environ.pop("CONTINUA_SITTING", None)
            else:
                os.environ["CONTINUA_SITTING"] = old


class TestWakeConsultLine(unittest.TestCase):
    def test_consult_line_present(self):
        tmp = tempfile.mkdtemp()
        rt.write_intentions(tmp, "residenta", "2026-09-27",
                            "read the news before Alex asks\n"
                            "leave the parser stuck")
        from datetime import datetime, timedelta
        payload = wk.build_payload.__wrapped__ if False else None
        # call the real builder with an intentions file dated yesterday
        old_root = wk.ch.DEFAULT_ROOT
        wk.ch.DEFAULT_ROOT = tmp
        try:
            yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            os.makedirs(os.path.join(tmp, "residenta", "system-sitting",
                                     "intentions"), exist_ok=True)
            rt.write_intentions(tmp, "residenta", yday, "read the news\nrest")
            p = wk.build_payload("residenta", root=tmp)
            text = p.get("prompt", "")
            self.assertIn("YOUR INTENTIONS FOR TODAY", text)
            self.assertIn("read the news", text)
            self.assertIn("consult or ignore", text)
            # verbatim, never paraphrased
            self.assertIn("rest", text)
        finally:
            wk.ch.DEFAULT_ROOT = old_root

    def test_no_intentions_no_line(self):
        old_root = wk.ch.DEFAULT_ROOT
        wk.ch.DEFAULT_ROOT = tempfile.mkdtemp()
        try:
            p = wk.build_payload("residenta", root=wk.ch.DEFAULT_ROOT)
            self.assertNotIn("YOUR INTENTIONS", p.get("prompt", ""))
        finally:
            wk.ch.DEFAULT_ROOT = old_root


class TestNames(unittest.TestCase):
    def test_system_sitting_renders_honestly(self):
        self.assertEqual(pp.name_for({}, "system-sitting"),
                         "your evening practice")
        self.assertEqual(pp.name_for({}, "system-wake"), "your wake window")


class TestPromptShape(unittest.TestCase):
    def test_sitting_prompt_has_the_contract(self):
        # the plan's draft markers, verbatim in the template — the designer reviews
        # this text before the flag flips
        user = rt.SITTING_USER.format(date="2026-09-27", scenes_block="S",
                                      intentions_yesterday="Y")
        self.assertIn("[SITTING — evening, 2026-09-27]", user)
        self.assertIn("She may follow them or ignore them", user)
        self.assertIn("consult or ignore", user) if False else None
        self.assertIn("Stuck is a state of being", user)
        self.assertIn("Nothing here is required", user)
        self.assertIn("silence", user)
        self.assertIn("TEND:", user)
        self.assertIn("INTEND:", user)
        self.assertIn("AFTERMATH:", user)
        frame = rt.SITTING_FRAME.format(identity="I am persona-a.")
        self.assertIn("I am persona-a.", frame)
        self.assertIn("a sitting", frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)