"""ritual_release_test.py — memorybuild.md Phase 5 (S5): she can let go of a
past keep. Synthetic marks/priors in tmp; the release files are append-only.
Run: python3 ritual_release_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ritual as rt   # noqa: E402
import recollections as rec   # noqa: E402

TODAY = __import__("datetime").date.today().strftime("%Y-%m-%d")

MARK_ROW = {"schema_version": 1, "date": "2026-09-20", "instance": "t",
            "scene_id": 2, "unit": "exchange", "uids": ["u-a", "u-b"],
            "kept": True, "meaning": "the cold glass morning"}


def _prior():
    return [
        {"date": "2026-09-20", "scene_id": 2, "meaning": "the cold glass morning",
         "uids": ["u-a", "u-b"], "unit": "exchange"},
        {"date": "2026-09-19", "scene_id": 5, "meaning": "the porchfest plan",
         "uids": ["u-c"], "unit": "exchange"},
        {"date": "2026-09-18", "scene_id": 1, "meaning": "a quiet wake",
         "uids": ["u-d"], "unit": "scene"},
    ]


class _World:
    """tmp marks + releases dirs; every test runs with rt.RELEASES_DIR
    pointed at the tmp dir (never production)."""

    def __init__(self):
        self.td = tempfile.mkdtemp(prefix="release-")
        self.marks = os.path.join(self.td, "ritual", "marks", "t")
        # the resolver appends the INSTANCE itself — the patch target is the
        # releases ROOT; the reader path includes t
        self.releases_root = os.path.join(self.td, "ritual", "releases")
        self.releases = os.path.join(self.releases_root, "t")
        os.makedirs(self.marks, exist_ok=True)
        os.makedirs(self.releases, exist_ok=True)

    def __enter__(self):
        self._patch = patch.object(rt, "RELEASES_DIR", self.releases_root)
        self._patch.start()
        return self

    def __exit__(self, *a):
        self._patch.stop()

    def release_rows(self, date=TODAY):
        p = os.path.join(self.releases, f"{date}.jsonl")
        if not os.path.exists(p):
            return []
        return [json.loads(l) for l in open(p) if l.strip()]

    def released_uids_direct(self):
        out = set()
        for row in self.release_rows():
            out.update((row.get("target") or {}).get("uids") or [])
        return out

    def marks_bytes(self, date="2026-09-20"):
        p = os.path.join(self.marks, f"{date}.jsonl")
        return open(p).read() if os.path.exists(p) else None

    def write_mark(self, row=MARK_ROW, date="2026-09-20"):
        with open(os.path.join(self.marks, f"{date}.jsonl"), "w") as f:
            f.write(json.dumps(row) + "\n")


class ResolveTests(unittest.TestCase):
    def _run(self, releases, world=None, prior=None):
        world = world or _World()
        with world:
            return rt.resolve_releases("t", TODAY, prior or _prior(),
                                       releases)

    def test_release_writes_row_with_mark_uids(self):
        w = _World()
        w.write_mark()
        written, dropped = self._run(
            [{"n": 1, "note": "I let the cold glass go"}], w)
        self.assertEqual((written, dropped), (1, 0))
        rows = w.release_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["by"], "her")
        self.assertEqual(r["note"], "I let the cold glass go")
        self.assertEqual(r["provenance"], "memoryplan S5")
        self.assertEqual(r["target"]["uids"], ["u-a", "u-b"])
        self.assertEqual(r["target"]["scene_id"], 2)
        self.assertEqual(r["target"]["mark_date"], "2026-09-20")
        self.assertEqual(r["target"]["unit"], "exchange")

    def test_marks_file_untouched(self):
        w = _World()
        w.write_mark()
        before = w.marks_bytes()
        self._run([{"n": 1, "note": "gone"}], w)
        self.assertEqual(w.marks_bytes(), before)

    def test_stale_number_writes_nothing(self):
        w = _World()
        written, dropped = self._run(
            [{"n": 9, "note": "no such keep"}], w)
        self.assertEqual((written, dropped), (0, 1))
        self.assertEqual(w.release_rows(), [])

    def test_two_releases_one_row_each(self):
        w = _World()
        written, _ = self._run([{"n": 1, "note": "first"},
                                {"n": 3, "note": "second"}], w)
        self.assertEqual(written, 2)
        rows = w.release_rows()
        self.assertEqual([r["note"] for r in rows], ["first", "second"])

    def test_no_note_still_releases(self):
        w = _World()
        written, _ = self._run([{"n": 2, "note": ""}], w)
        self.assertEqual(written, 1)
        self.assertEqual(w.release_rows()[0]["note"], "")


class ParseTests(unittest.TestCase):
    def test_parse_sitting_releases(self):
        text = ("The night was quiet.\n\nRELEASE 2 | I don't need that "
                "reminder anymore\nRELEASE 1 | let the morning go\n"
                "INTEND: rest\n")
        p = rt.parse_sitting(text, scene_count=4)
        self.assertEqual([r["n"] for r in p["releases"]], [2, 1])
        self.assertEqual(p["releases"][0]["note"],
                         "I don't need that reminder anymore")
        self.assertEqual(p["intend"], "rest")

    def test_release_does_not_eat_intend_block(self):
        text = ("INTEND: rest well\nRELEASE 1 | done with it\nanother line")
        p = rt.parse_sitting(text, scene_count=4)
        self.assertEqual(p["intend"], "rest well")
        self.assertEqual(len(p["releases"]), 1)

    def test_release_tolerant_separators(self):
        for line in ("RELEASE 1|note", "release 1 : note", "RELEASE #1 — note"):
            p = rt.parse_sitting(line, scene_count=4)
            self.assertEqual(len(p["releases"]), 1, line)


class BlockTests(unittest.TestCase):
    def test_numbered_block_shape(self):
        block = rt.build_continuity_block(_prior(), numbered=True)
        self.assertIn("  1 [2026-09-20] KEEP: the cold glass morning", block)
        self.assertIn("  3 [2026-09-18] KEEP: a quiet wake", block)
        self.assertIn("RELEASE <number>", block)
        self.assertIn("It is not deleted", block)

    def test_unnumbered_is_legacy_bytes(self):
        prior = _prior()
        legacy = rt.build_continuity_block(prior)
        self.assertEqual(legacy, rt.build_continuity_block(prior,
                                                           numbered=False))
        self.assertNotIn("RELEASE", legacy)
        self.assertNotIn("  1 [", legacy)

    def test_limit_twelve(self):
        prior = [{"date": f"2026-09-{d:02d}", "scene_id": i,
                  "meaning": f"keep {i}", "uids": [], "unit": "scene"}
                 for i, d in enumerate(range(28, 0, -2), 1)]
        block = rt.build_continuity_block(prior, numbered=True)
        self.assertNotIn("keep 13", block)   # only the newest 12 render
        self.assertIn("keep 12", block)


class SubtractionTests(unittest.TestCase):
    def test_marked_uids_subtract_released(self):
        w = _World()
        w.write_mark()   # u-a, u-b kept
        with open(os.path.join(w.marks, "2026-09-20.jsonl"), "a") as f:
            f.write(json.dumps({"date": "2026-09-20", "instance": "t",
                                "scene_id": 3, "uids": ["u-z"], "kept": True,
                                "meaning": "still kept"}) + "\n")
        with w:
            rt.resolve_releases("t", TODAY, _prior()[:1],
                                [{"n": 1, "note": "let it go"}])
            # the real subtraction: marked_uids reads BOTH dirs (tmp marks;
            # the release reader patched to the tmp releases)
            with patch.object(rec, "released_uids",
                              lambda inst, root=None:
                              w.released_uids_direct()):
                marked = rec.marked_uids("t", marks_root=w.marks)
        self.assertIn("u-z", marked)     # the keep that stayed
        self.assertNotIn("u-a", marked)  # released
        self.assertNotIn("u-b", marked)

    def test_released_still_recallable(self):
        """The store keeps everything — a release only changes the chosen
        set. The uids still exist in the marks file (never rewritten)."""
        w = _World()
        w.write_mark()
        with w:
            rt.resolve_releases("t", TODAY, _prior()[:1],
                                [{"n": 1, "note": "gone"}])
        rows = [json.loads(l) for l in
                open(os.path.join(w.marks, "2026-09-20.jsonl"))]
        self.assertEqual(rows[0]["uids"], ["u-a", "u-b"])  # the mark row intact
        with patch.object(rec, "released_uids",
                          lambda inst, root=None: w.released_uids_direct()):
            marked = rec.marked_uids("t", marks_root=w.marks)
        self.assertNotIn("u-a", marked)              # out of the chosen set


class GateTests(unittest.TestCase):
    def test_gate_env_wins(self):
        cfg = {"continua": {"ritual": {"release": True}}}
        os.environ["CONTINUA_RITUAL_RELEASE"] = "0"
        try:
            on = (os.getenv("CONTINUA_RITUAL_RELEASE", "1") != "0"
                  and bool(((cfg.get("continua") or {}).get("ritual") or {})
                           .get("release", False)))
            self.assertFalse(on)
        finally:
            os.environ.pop("CONTINUA_RITUAL_RELEASE", None)

    def test_real_yaml_gates_on(self):
        import yaml
        for inst in ("residentb", "residenta"):
            cfg = yaml.safe_load(open(f"configs/{inst}.yaml"))
            self.assertTrue(((cfg.get("continua") or {}).get("ritual") or {})
                            .get("release", False), inst)


if __name__ == "__main__":
    unittest.main()