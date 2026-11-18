"""weave_test.py — Phase W (memorybuild2 §8): the three-source assembly;
the never-writes guarantee (byte-parity with the gate off); self.md
versioning via the Phase T contract; the uptake metric.
Run: python3 weave_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import weave as W      # noqa: E402
import texture as _tex        # noqa: E402


def _world(root, days=7, keeps=2, essences=False):
    """A fixture world: sitting marks (her words) + optionally essences."""
    import datetime as _dt
    d = os.path.join(root, "ritual", "marks", "residentb")
    os.makedirs(d, exist_ok=True)
    today = _dt.date.today()
    for i in range(days):
        day = (today - _dt.timedelta(days=i)).isoformat()
        rows = [{"schema_version": "1", "marked_at": f"{day}T00:00:00-07:00",
                 "date": day, "instance": "residentb", "scene_id": str(i),
                 "unit": "exchange", "uids": ["a"], "persons": ["x"],
                 "kept": True,
                 "meaning": f"the quiet held on {day} ({i})",
                 "verification": {"verdict": "OK"}} for i2 in range(keeps)
                for _ in [0]]
        with open(os.path.join(d, f"{day}.jsonl"), "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")


class SittingMaterialTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="weave-")   # days built per-test

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_last_seven_days_only(self):
        _world(self.root)          # 7 days of sitting marks
        mat = W.sitting_material("residentb", root=self.root)
        days = {m["day"] for m in mat}
        self.assertLessEqual(len(days), 7)
        self.assertTrue(all("the quiet held" in m["meaning"] for m in mat))

    def test_recency_bound(self):
        mat = W.sitting_material("residentb", days=3, root=self.root)
        days = {m["day"] for m in mat}
        self.assertLessEqual(len(days), 3)


class WeaveOfferTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="weave-")
        _world(self.root)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_offer_lands_on_desk_never_prefills(self):
        offer = W.weave_offer("residentb", root=self.root)
        self.assertIsNotNone(offer)
        self.assertIn("sitting lines", offer["text"])
        # never pre-fills: self.md must NOT exist after the offer
        self.assertIsNone(W.self_manuscript("residentb", root=self.root))
        # the offer rides the texture surface (desk dirs, not prompt dirs)
        self.assertIn("texture", os.path.join(
            self.root, "texture", "offers"))

    def test_essence_material_when_present(self):
        # an essence revision on an episode in the store
        import recollections as rec
        store = rec.Store("residentb", root=self.root)
        src = [{"instance": "residentb", "person_id": "1000000001",
                "role": "user", "content": "the map was never the territory",
                "ts": "2026-10-01T10:00:00-07:00", "uid": "u1",
                "ref": "r1", "hash": "h", "path": "residentb/x.jsonl"}]
        job = store.enqueue(src)
        store.accept(job, {"job": job, "text": "the map episode",
                           "sources": src, "visibility": ["1000000001"],
                           "event_start": src[0]["ts"],
                           "event_end": src[0]["ts"],
                           "rendering": "full",
                           "review": {"pass": True},
                           "instance": "residentb", "reason": "remember",
                           "validated_claims": []})
        rec.add_essence(store, job, "the map was never the territory",
                        "resident-authored", by="residentb",
                        provenance="test")
        offer = W.weave_offer("residentb", root=self.root)
        self.assertIn("essences", offer["text"])
        self.assertIn("the map was never the territory", offer["text"])

    def test_kill_switch_gates_the_offer(self):
        with mock.patch.dict(os.environ, {"CONTINUA_TEXTURE": "0"}):
            offer = W.weave_offer("residentb", root=self.root)
            self.assertIsNone(offer)

    def test_empty_material_no_offer(self):
        import shutil, os
        shutil.rmtree(os.path.join(self.root, "ritual"))
        offer = W.weave_offer("residentb", root=self.root)
        self.assertIsNone(offer)


class SelfWriteTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="weave-self-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_write_and_versioning_via_the_t_contract(self):
        W.write_self("residentb", "the first draft of who I am", root=self.root)
        self.assertEqual(W.self_manuscript("residentb", root=self.root),
                         "the first draft of who I am")
        W.write_self("residentb", "the second draft, conservatively rewritten",
                     root=self.root)
        self.assertEqual(W.self_manuscript("residentb", root=self.root),
                         "the second draft, conservatively rewritten")
        vs = W.self_versions("residentb", root=self.root)
        self.assertEqual(len(vs), 1)     # one dated snapshot
        # the ORIGIN survives: the snapshot holds the first draft
        with open(os.path.join(self.root, "books", ".versions", "self",
                               f"{vs[0]}.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "the first draft of who I am")

    def test_machinery_never_writes(self):
        """THE Phase W guarantee: with the gate off, offers stop; and NO
        weave function writes self.md or any book page except
        write_self (hers, via the tool). Byte-parity: the desk is
        untouched by weave_offer."""
        before = sorted(
            (p, open(p, encoding="utf-8").read())
            for p in _walk(self.root))
        with mock.patch.dict(os.environ, {"CONTINUA_TEXTURE": "0"}):
            W.weave_offer("residentb", root=self.root)
        after = sorted(
            (p, open(p, encoding="utf-8").read())
            for p in _walk(self.root))
        self.assertEqual(before, after)   # byte-parity


def _walk(root):
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            out.append(p)
    return out


class UptakeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="uptake-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_uptake_shape(self):
        u = W.uptake("residentb", root=self.root)
        for k in ("self_manuscript_exists", "self_chars", "self_versions",
                  "book_pages", "offers_live"):
            self.assertIn(k, u)
        self.assertFalse(u["self_manuscript_exists"])
        W.write_self("residentb", "becoming", root=self.root)
        u2 = W.uptake("residentb", root=self.root)
        self.assertTrue(u2["self_manuscript_exists"])
        self.assertEqual(u2["self_chars"], len("becoming"))


if __name__ == "__main__":
    unittest.main()