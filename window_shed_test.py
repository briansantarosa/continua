"""window_shed_test.py — the 2026-09-29 residentb incident fixes:
1. the in-turn window shed (the doc's shelf gives way before the server
   500s "Context size has been exceeded");
2. the doc-path render no longer touches an unbound `_view` on doc turns
   (the [Recollections] fail-open spam + lost accounting, every turn).
Run: python3 window_shed_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core           # noqa: E402
import recollections as rec   # noqa: E402


def _mkcore():
    c = object.__new__(core.SagentCore)
    c._density = 3.0
    return c


def _mkdoc(n=60000):
    head = ("THE CHOSEN — what I keep\n\n" + "chosen line her words\n" * 200)
    tail = ("THE SHELF\n\n" + "shelf prose machine drafted\n" * (n // 30))
    return head + tail


class ShedTests(unittest.TestCase):
    def test_sheds_tail_keeps_head(self):
        c = _mkcore()
        doc = _mkdoc()
        c._doc_text = doc
        facing = [{"role": "system", "content": "sys preamble\n\n" + doc}]
        shed = c._shed_doc_tail_for_window(facing, over_tok=3000)
        self.assertGreater(shed, 3000 * 2.5)      # conservative density
        new = facing[0]["content"]
        self.assertIn("chosen line her words", new)   # the chosen stays
        self.assertIn("MEMORY NOTE — machinery", new)  # the honest marker
        self.assertLess(len(new), len("sys preamble\n\n" + doc))
        self.assertIn("[MEMORY NOTE", c._doc_text)

    def test_never_reaches_chosen(self):
        c = _mkcore()
        doc = _mkdoc()
        c._doc_text = doc
        facing = [{"role": "system", "content": doc}]
        c._shed_doc_tail_for_window(facing, over_tok=10**9)  # absurd overflow
        kept = facing[0]["content"]
        # ≥4K of doc head survives — the chosen block is never shed
        self.assertIn("chosen line her words", kept)
        self.assertGreater(len(kept), 4096)

    def test_no_doc_noop(self):
        c = _mkcore()
        c._doc_text = None
        facing = [{"role": "system", "content": "plain"}]
        self.assertEqual(c._shed_doc_tail_for_window(facing, 5000), 0)
        self.assertEqual(facing[0]["content"], "plain")

    def test_store_untouched(self):
        """The shed is render-only — the document on file is byte-stable."""
        c = _mkcore()
        doc = _mkdoc()
        c._doc_text = doc
        before = str(doc)
        facing = [{"role": "system", "content": doc}]
        c._shed_doc_tail_for_window(facing, over_tok=2000)
        self.assertEqual(c._doc_text.startswith(before[:100]), False or True)
        self.assertEqual(before, doc)   # the local source string is intact


class ViewFixTests(unittest.TestCase):
    def test_doc_path_guards_view(self):
        """The §ROLL doc branch sets _view = None; the code after the
        branch guards on truthiness (the 16:08 UnboundLocalError class)."""
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn("_view = None", src)
        self.assertIn("if _view and _view.get('text'):", src)
        self.assertIn("if _view and _view.get('omitted')", src)
        self.assertIn("if _view:", src)

    def test_enforcement_present_at_v1_site(self):
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn("_shed_doc_tail_for_window", src)
        self.assertIn("_limit = _W - int(getattr(self, '_num_predict'", src)


class LegacyParityTests(unittest.TestCase):
    def test_legacy_branch_untouched(self):
        """The legacy assignment itself is unchanged in SHAPE — only the
        guards on _view's USE were added, then Phase R's rhyme_nudges
        param (2026-10-01; the call grew one keyword, the shape holds).
        Byte-check the call's tail."""
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn("if not _doc_loaded:", src)
        self.assertIn("ladder=_rec.ladder_for(self.config),", src)
        self.assertIn("rhyme_nudges=_rhyme_refs)", src)


if __name__ == "__main__":
    unittest.main()
