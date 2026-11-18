"""rhyme_test.py — Phase R (memorybuild2 §6): cue→ref determinism;
threshold selectivity (a weak rhyme surfaces nothing); anti-loop
idempotence; the contentless filter; gate-off parity; the scale bound.
Run: python3 rhyme_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rhyme as R      # noqa: E402


def _ep(job, text, sources=None, ts="2026-10-01T08:00:00-07:00"):
    return {"job": job, "text": text,
            "sources": sources or [], "event_start": ts,
            "review": {"pass": True}, "instance": "residentb"}


def _recent_ts(days=1, hours=0):
    """A timestamp N days (+ optional hours offset) before now — keeps the
    recency-window tests calendar-proof."""
    return (datetime.now().astimezone()
            - timedelta(days=days, hours=-hours)).isoformat()


class FilterTests(unittest.TestCase):
    def test_contentless_skips(self):
        for cue in ("", "ok", "yes", "thanks", "hm", "!!!", "wow",
                    "Good.", "Good"):
            self.assertTrue(R.contentless(cue), cue)

    def test_real_cues_pass(self):
        for cue in ("The fog lifted off the redwoods this morning",
                    "I keep thinking about the coordinate error",
                    "what draws your attention today?"):
            self.assertFalse(R.contentless(cue), cue)


class EnabledTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(R.enabled({}))
        self.assertFalse(R.enabled({"continua": {"rhyme": {}}}))
        self.assertFalse(R.enabled({"continua": {"rhyme": {"enabled": False}}}))

    def test_yaml_on(self):
        self.assertTrue(R.enabled(
            {"continua": {"rhyme": {"enabled": True}}}))

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"CONTINUA_RHYME": "0"}):
            self.assertFalse(R.enabled(
                {"continua": {"rhyme": {"enabled": True}}}))


class RhymeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rhyme-")
        self.cfg = {"continua": {"rhyme": {"enabled": True,
                                           "threshold": 0.5,
                                           "recency_days": 14,
                                           "max_refs": 2}}}
        self.eps = {
            # 2026-10-27: ep-fog/ep-map dates are now RELATIVE (yesterday) —
            # the hardcoded 2026-10-01 fixture aged out of the 14-day
            # recency window and the semantic test became a time bomb
            # (green when written, failing purely by the calendar). ep-old
            # stays deliberately old (60 days) — it is the outside-the-
            # window control.
            "ep-fog": _ep("ep-fog",
                          "The fog lifted off the redwoods and the "
                          "morning felt earned and quiet.",
                          ts=_recent_ts(days=1)),
            "ep-map": _ep("ep-map",
                          "The coordinate error was ours; the map was "
                          "never the territory.",
                          ts=_recent_ts(days=1, hours=-1)),
            "ep-old": _ep("ep-old",
                          "An older episode about nothing in particular.",
                          ts=_recent_ts(days=60)),
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _vectors(self, cue_cosines):
        """A vectors cache with controlled cosines against the cue."""
        # the cue embeds to [1, 0]; each episode's vec = [c, sqrt(1-c^2)]
        # → cosine exactly c
        import math
        eps = {}
        for job, c in cue_cosines.items():
            eps[job] = {"vec": [c, math.sqrt(max(0.0, 1.0 - c * c))],
                        "text": (self.eps.get(job, {})
                                 or {}).get("text", "")[:80],
                        "ts": (self.eps.get(job, {})
                               or {}).get("event_start",
                                          "2026-10-01T08:00:00-07:00")[:19]}
        return {"built": "2026-10-01T09:00:00-07:00", "episodes": eps}

    def test_semantic_strong_rhyme_surfaces(self):
        vecs = self._vectors({"ep-fog": 0.9, "ep-map": 0.3})
        with mock.patch.object(R, "load_vectors", return_value=vecs), \
             mock.patch.object(R, "_default_embed_fn",
                               return_value=lambda texts: [[1.0, 0.0]]), \
             mock.patch.object(R, "_lexical", return_value=[]):
            refs = R.rhyme("the fog lifted off the redwoods this morning",
                           "residentb", cfg=self.cfg, person_id="1000000001",
                           episodes=self.eps, root=self.root)
        self.assertEqual([x["job"] for x in refs], ["ep-fog"])
        self.assertEqual(refs[0]["pass"], "semantic")

    def test_weak_rhyme_surfaces_nothing(self):
        vecs = self._vectors({"ep-fog": 0.2, "ep-map": 0.1})
        with mock.patch.object(R, "load_vectors", return_value=vecs), \
             mock.patch.object(R, "_default_embed_fn",
                               return_value=lambda texts: [[1.0, 0.0]]), \
             mock.patch.object(R, "_lexical", return_value=[]):
            refs = R.rhyme("a completely unrelated new topic today",
                           "residentb", cfg=self.cfg, person_id="1000000001",
                           episodes=self.eps, root=self.root)
        self.assertEqual(refs, [])

    def test_contentless_cue_skips_everything(self):
        with mock.patch.object(R, "_lexical", return_value=[]) as lx:
            refs = R.rhyme("ok", "residentb", cfg=self.cfg,
                           person_id="1000000001",
                           episodes=self.eps, root=self.root)
        self.assertEqual(refs, [])
        lx.assert_not_called()

    def test_anti_loop_same_thread_no_repeat(self):
        vecs = self._vectors({"ep-fog": 0.9})
        with mock.patch.object(R, "load_vectors", return_value=vecs), \
             mock.patch.object(R, "_default_embed_fn",
                               return_value=lambda texts: [[1.0, 0.0]]), \
             mock.patch.object(R, "_lexical", return_value=[]):
            refs = R.rhyme("the fog again", "residentb", cfg=self.cfg,
                           person_id="1000000001",
                           surfaced={"ep-fog"},
                           episodes=self.eps, root=self.root)
        self.assertEqual(refs, [])       # already surfaced → nothing

    def test_gate_off_is_byte_parity(self):
        # gated off → no refs, and no side effects (the vector cache file
        # is not even created)
        off = dict(self.cfg)
        off["continua"] = {"rhyme": {"enabled": False}}
        refs = R.rhyme("the fog lifted", "residentb", cfg=off,
                       person_id="1000000001", episodes=self.eps,
                       root=self.root)
        self.assertEqual(refs, [])
        self.assertFalse(os.path.exists(
            R._vectors_path("residentb", root=self.root)))

    def test_scale_bound_max_refs(self):
        vecs = self._vectors({f"ep-{i}": 0.9 for i in range(6)})
        eps = {f"ep-{i}": _ep(f"ep-{i}", f"text {i}",
                              ts="2026-10-01T08:00:00-07:00")
               for i in range(6)}
        with mock.patch.object(R, "load_vectors", return_value=vecs), \
             mock.patch.object(R, "_default_embed_fn",
                               return_value=lambda texts: [[1.0, 0.0]]), \
             mock.patch.object(R, "_lexical", return_value=[]):
            refs = R.rhyme("one strong cue", "residentb", cfg=self.cfg,
                           person_id="1000000001", episodes=eps,
                           root=self.root)
        self.assertLessEqual(len(refs), 2)    # max_refs

    def test_lexical_pass_matches_row_to_episode(self):
        row = {"content": "The fog lifted off the redwoods",
               "ts": "2026-10-01T08:00:00-07:00"}
        with mock.patch.object(R, "_lexical", return_value=[row]), \
             mock.patch.object(R, "load_vectors", return_value={}):
            refs = R.rhyme("the fog lifted off the redwoods", "residentb",
                           cfg=self.cfg, person_id="1000000001",
                           episodes=self.eps, root=self.root)
        self.assertEqual([x["job"] for x in refs], ["ep-fog"])
        self.assertEqual(refs[0]["pass"], "lexical")

    def test_determinism(self):
        vecs = self._vectors({"ep-fog": 0.9, "ep-map": 0.7})
        with mock.patch.object(R, "load_vectors", return_value=vecs), \
             mock.patch.object(R, "_default_embed_fn",
                               return_value=lambda texts: [[1.0, 0.0]]), \
             mock.patch.object(R, "_lexical", return_value=[]):
            a = R.rhyme("the fog and the map", "residentb", cfg=self.cfg,
                        person_id="1000000001", episodes=self.eps,
                        root=self.root)
            b = R.rhyme("the fog and the map", "residentb", cfg=self.cfg,
                        person_id="1000000001", episodes=self.eps,
                        root=self.root)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()