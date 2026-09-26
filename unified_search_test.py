"""Agent-free tests for unified mem0 search (house ruling: search unified,
injection person-scoped)."""
import os
import sys, os
import unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import _merge_unified_hits
import people as pp

def _fixture_roster(entries):
    """Synthesize a roster directly (people.Person) — no dependence on any
    installed configs, no loader recursion."""
    import people as _pp
    out = {}
    for e in entries:
        p = _pp.Person(person_id=e["id"], name=e.get("name", ""),
                       can_message=e.get("can_message", False),
                       daily_cap=e.get("daily_cap", 0),
                       kind=e.get("kind", "human"))
        out[p.person_id] = p
    return out

def h(mem, score, uid=None):
    d = {"memory": mem, "score": score}
    if uid: d["id"] = uid
    return d

class TestMerge(__import__("unittest").TestCase):
    def test_merge_rank_dedupe_attribute(self):
        by_user = {
            "1000000001": [h("Continuua plan saved", 0.9, "id1"),
                           h("Alex tests carefully", 0.7, "id2")],
            "8737936808": [h("toddler sleep research", 0.8, "id3"),
                           h("Continuua plan saved", 0.6, "id4")],  # dup text
        }
        merged = _merge_unified_hits(by_user, 10)
        # dedup: same text across users kept once (highest score first)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["memory"], "Continuua plan saved")
        self.assertEqual(merged[0]["source_user_id"], "1000000001")
        self.assertTrue(all("source_user_id" in m for m in merged))

    def test_cap(self):
        by_user = {"u1": [h(f"m{i}", 1 - i * 0.01) for i in range(5)]}
        self.assertEqual(len(_merge_unified_hits(by_user, 3)), 3)

    def test_missing_scores_stable(self):
        merged = _merge_unified_hits({"u1": [h("a", None)], "u2": [h("b", 0.5)]}, 10)
        self.assertEqual(len(merged), 2)

    def test_attribution_via_roster(self):
        roster = _fixture_roster([{"id": "1000000001", "name": "Alex",
                                   "can_message": True, "daily_cap": 20}])
        self.assertIn("Alex", pp.name_for(roster, "1000000001"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
