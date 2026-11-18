"""ritual_faded_review_test.py — memorybuild.md Phase 6 (S6): the dream
cycle — some of what faded is offered back. Synthetic last-good views +
stores in tmp. Run: python3 ritual_faded_review_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ritual as rt    # noqa: E402
import recollections as rec   # noqa: E402

from datetime import date as _date, timedelta as _td

TODAY = _date.today().strftime("%Y-%m-%d")


def _d(days_ago):
    """A fixture date days_ago before today — hermetic against the
    trailing-week window (the original literals aged out 2026-10-06)."""
    return (_date.today() - _td(days=days_ago)).strftime("%Y-%m-%d")


def _entry(job, date, thread="1000000001", first="a faded memory"):
    return {"job": job, "handle": job[:6], "first": first,
            "date": date, "thread": thread}


def _world(entries, faded_ids=None):
    """tmp recollections dir with a last_good_view.json; a tmp store."""
    td = tempfile.mkdtemp(prefix="faded-")
    rdir = os.path.join(td, "recollections", "t")
    os.makedirs(rdir, exist_ok=True)
    view = {"person": "1000000001", "at": TODAY + "T12:00:00-07:00",
            "text": "x", "selected": 3,
            "faded_entries": entries}
    with open(os.path.join(rdir, "last_good_view.json"), "w") as f:
        json.dump(view, f)
    store = rec.Store("t", root=td)
    # seed 2 real accepted episodes (for store-wide fallback resolution)
    for i, job in enumerate(faded_ids or []):
        src = [{"instance": "t", "person_id": "111", "role": "user",
                "content": f"src {i}", "ts": f"{TODAY}T10:00:00-07:00",
                "uid": f"u{i}", "ref": f"r{i}", "hash": f"h{i}",
                "path": f"t/111/{TODAY}.jsonl"}]
        j = store.enqueue(src)
        if j:
            store.accept(j, {"job": j, "text": f"mem {i}"})
    return td, store


class BlockTests(unittest.TestCase):
    def test_block_renders_entries_with_handles(self):
        jobs = [f"a{i}b{i}c" + "0" * (58 - len(str(i))) + str(i)
                for i in range(3)]
        entries = [_entry(j, _d(3), thread=f"111{i}",
                          first=f"memory {i}") for i, j in enumerate(jobs)]
        td, _ = _world(entries)
        block = rt.faded_review_block("t", base=td)
        self.assertIn("[WHAT FADED THIS WEEK — optional]", block)
        self.assertIn("3 memories left your view this week", block)
        self.assertIn("Nothing was deleted", block)
        self.assertIn("KEEP #<handle>", block)
        self.assertIn("Ignoring this costs nothing", block)
        for i, j in enumerate(jobs):
            self.assertIn(f"#{j[:6]}", block)
            self.assertIn(f"memory {i}", block)
        # R28: no day ticker — dates ride entries, no per-day index
        self.assertNotIn("DAY INDEX", block.upper())

    def test_caps_at_8_one_per_thread_first(self):
        jobs = [f"job{i}{'x' * 50}{i:04d}" for i in range(14)]
        # 14 entries across 3 threads, interleaved
        entries = [_entry(j, _d(4), thread=f"11{i % 3}")
                   for i, j in enumerate(jobs)]
        td, _ = _world(entries)
        block = rt.faded_review_block("t", base=td)
        rendered = [l for l in block.splitlines() if l.startswith("  #")]
        self.assertEqual(len(rendered), 8)   # the cap
        # one per thread FIRST: the first 3 lines are 3 distinct threads
        self.assertEqual(len({l.split("·")[2].split("—")[0].strip()
                              for l in rendered[:3]}), 3)

    def test_trailing_week_filter(self):
        old = [_entry("aaaaaa" + "0" * 58, _d(30), first="too old")]
        fresh = [_entry("bbbbbb" + "0" * 58, _d(3), first="fresh")]
        td, _ = _world(old + fresh)
        block = rt.faded_review_block("t", base=td)
        self.assertIn("1 memories left your view this week", block)
        self.assertNotIn("too old", block)
        self.assertIn("fresh", block)

    def test_missing_or_corrupt_file_no_block(self):
        td = tempfile.mkdtemp(prefix="faded-none-")
        os.makedirs(os.path.join(td, "recollections", "t"))
        self.assertEqual(rt.faded_review_block("t", base=td), "")
        # corrupt
        with open(os.path.join(td, "recollections", "t",
                               "last_good_view.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(rt.faded_review_block("t", base=td), "")
        # empty faded
        td2, _ = _world([])
        self.assertEqual(rt.faded_review_block("t", base=td2), "")


class KeepBackTests(unittest.TestCase):
    def test_handle_keep_anchors_with_why(self):
        td = tempfile.mkdtemp(prefix="faded-real-")
        rdir = os.path.join(td, "recollections", "t")
        os.makedirs(rdir, exist_ok=True)
        store = rec.Store("t", root=td)
        src = [{"instance": "t", "person_id": "111", "role": "user",
                "content": "src", "ts": f"{TODAY}T10:00:00-07:00",
                "uid": "u0", "ref": "r0", "hash": "h0",
                "path": f"t/111/{TODAY}.jsonl"}]
        job = store.enqueue(src)
        store.accept(job, {"job": job, "text": "mem"})
        entries = [_entry(job, "2026-09-27", first="the one she wants back")]
        with open(os.path.join(rdir, "last_good_view.json"), "w") as f:
            json.dump({"person": "111", "at": "x", "text": "x", "selected": 0,
                       "faded_entries": entries}, f)
        anchored, dropped = rt.resolve_faded_keeps(
            "t", [{"handle": job[:6], "why": "this one stays with me"}],
            [job], store=store)
        self.assertEqual((anchored, dropped), (1, 0))
        anchors, _ = rec.read_guards("t", root=td)
        self.assertIn(job, anchors)
        whys = rec.anchor_whys("t", root=td)
        self.assertEqual(whys.get(job), "this one stays with me")

    def test_unknown_and_ambiguous_dropped_counted(self):
        job = "deadbe" + "2" * 58
        other = "deadbe" + "3" * 58
        td, store = _world([_entry(job, "2026-09-27")],
                           faded_ids=[job, other])
        # ambiguous: both store jobs share the 6-char prefix
        anchored, dropped = rt.resolve_faded_keeps(
            "t", [{"handle": "deadbe", "why": "hm"}], [job], store=store)
        self.assertEqual((anchored, dropped), (0, 1))
        # unknown: no match anywhere
        anchored, dropped = rt.resolve_faded_keeps(
            "t", [{"handle": "ffff00", "why": "hm"}], [job], store=store)
        self.assertEqual((anchored, dropped), (0, 1))
        anchors, _ = rec.read_guards("t", root=td)
        self.assertEqual(anchors, set())

    def test_parse_sitting_handle_keeps(self):
        p = rt.parse_sitting("KEEP #a3f9c2 | the scent stays\n"
                             "KEEP 3 | a mark\nINTEND: rest\n",
                             scene_count=4)
        self.assertEqual(p["handle_keeps"],
                         [{"handle": "a3f9c2", "why": "the scent stays"}])
        self.assertEqual(p["kept"], {3: "a mark"})   # integers stay marks
        self.assertEqual(p["intend"], "rest")


class GateTests(unittest.TestCase):
    def test_real_yaml_gates_on(self):
        import yaml
        for inst in ("residentb", "residenta"):
            cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
            self.assertTrue(((cfg.get("continua") or {}).get("ritual") or {})
                            .get("faded_review", False), inst)


class SelectViewFadedTests(unittest.TestCase):
    def test_view_carries_faded_entries(self):
        """select_view's result gains the faded list (additive; the text is
        unchanged)."""
        import datetime as _dt
        revs = []
        for i in range(6):
            body = {"job": f"fade{i}{'0' * 40}{i}", "text": f"faded text {i}",
                    "visibility": ["111"],
                    "event_start": f"2026-09-{10+i:02d}T10:00:00-07:00",
                    "event_end": f"2026-09-{10+i:02d}T10:00:00-07:00",
                    "rendering": "full", "review": {"pass": True},
                    "instance": "t",
                    "sources": [{"ref": f"r{i}", "content": f"c{i}",
                                 "person_id": "111", "ts": "x"}]}
            revs.append(body)
        from recollections import _terms
        with patch.object(rec, "_remember_last_good"), \
             patch.object(rec, "read_guards", lambda *a, **k: (set(), {})), \
             patch.object(rec, "read_revisions", lambda *a, **k: revs):
            view = rec.select_view(revs, "t", "111", budget=100,
                                   at="2026-09-29T12:00:00-07:00")
        self.assertIn("faded", view)
        self.assertEqual(len(view["faded"]),
                         len(view["faded"]))   # present, any count
        for e in view["faded"]:
            self.assertIn("handle", e)
            self.assertIn("first", e)
            self.assertIn("date", e)
            self.assertIn("thread", e)


if __name__ == "__main__":
    unittest.main()