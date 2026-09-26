"""index_test.py — agent-free tests for phase 2: roster, episodic index,
recall modes, attribution contract, anti-loop guard, digest prompt-building.

Run:  python3 index_test.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chronicle as ch
import index as ix
import people as pp
import digest as dg

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

def _write_mirror(root, instance, person_id, day, turns):
    d = os.path.join(root, instance, person_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{day}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for t in turns:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    return path


class TestRoster(unittest.TestCase):
    def test_load_and_display_name(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "residenta.yaml"), "w") as f:
            f.write("app:\n  instance_id: residenta\npeople:\n"
                    "  - id: '1000000001'\n    name: 'Alex'\n"
                    "    can_message: true\n    daily_cap: 20\n"
                    "  - id: '8737936808'\n    name: ''\n")
        roster = pp.load_roster(config_dir=d)
        self.assertEqual(roster["1000000001"].name, "Alex")
        self.assertEqual(pp.name_for(roster, "1000000001"), "Alex")
        # unnamed person → honest fallback, never invented
        self.assertEqual(pp.name_for(roster, "8737936808"), "person-8737936808")
        # unknown person → honest fallback
        self.assertEqual(pp.name_for(roster, "999999"), "person-999999")


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="chronicle-idx-")
        self.db = os.path.join(tempfile.mkdtemp(), "chronicle.db")
        # Alex's conversation about the memory plan
        _write_mirror(self.root, "residenta", "1000000001", "2026-09-06", [
            {"uid": "u1", "ts": "2026-09-06T10:00:00-07:00", "instance": "residenta",
             "person_id": "1000000001", "role": "user",
             "content": "Do you want full conversation memory?"},
            {"uid": "a1", "ts": "2026-09-06T10:01:00-07:00", "instance": "residenta",
             "person_id": "1000000001", "role": "assistant",
             "content": "Yes — not as a tool, as a practice of remembering.",
             "reasoning": "thinking about continuity"},
            {"uid": "u2", "ts": "2026-09-06T11:28:00-07:00", "instance": "residenta",
             "person_id": "1000000001", "role": "assistant",
             "content": "the sandbox answer ends Wait, let"},
        ])
        # a different person, different topic
        _write_mirror(self.root, "residenta", "8737936808", "2026-09-05", [
            {"uid": "u3", "ts": "2026-09-05T09:00:00-07:00", "instance": "residenta",
             "person_id": "8737936808", "role": "user",
             "content": "What food should we try tonight?"}])

    def test_rebuild_update_and_dedup(self):
        n = ix.rebuild(root=self.root, db_path=self.db)
        self.assertEqual(n, 4)
        # update with nothing new → 0
        self.assertEqual(ix.update(root=self.root, db_path=self.db), 0)
        # new record → indexed exactly once
        _write_mirror(self.root, "residenta", "1000000001", "2026-09-07", [
            {"uid": "u4", "ts": "2026-09-07T08:00:00-07:00", "instance": "residenta",
             "person_id": "1000000001", "role": "user",
             "content": "morning followup about the sandbox"}])
        self.assertEqual(ix.update(root=self.root, db_path=self.db), 1)

    def test_search_default_person_filtered(self):
        ix.rebuild(root=self.root, db_path=self.db)
        res = ix.search("conversation memory", instance="residenta",
                        person_id="1000000001", db_path=self.db,
                        roster=_fixture_roster([{"id": "1000000001", "name": "Alex",
                                  "can_message": True, "daily_cap": 20}]))
        self.assertTrue(res)
        self.assertEqual(res[0]["person_id"], "1000000001")
        self.assertEqual(res[0]["person_name"], "Alex")  # roster attribution
        self.assertIn("Alex", res[0]["attribution"])
        self.assertIn("2026-09-06", res[0]["attribution"])

    def test_cross_person_mode_surfaces_other_people(self):
        ix.rebuild(root=self.root, db_path=self.db)
        res = ix.search("food tonight", instance="residenta",
                        person_id="1000000001", db_path=self.db)
        self.assertEqual(res, [])  # default mode: other person's memory stays out
        res = ix.search("food tonight", instance="residenta",
                        person_id="1000000001", cross_person=True,
                        db_path=self.db)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["person_id"], "8737936808")
        # attribution contract present even for unnamed persons
        self.assertEqual(res[0]["person_name"], "person-8737936808")
        self.assertIn("person-8737936808", res[0]["attribution"])

    def test_anti_loop_exclusion(self):
        ix.rebuild(root=self.root, db_path=self.db)
        first = ix.search("practice of remembering", instance="residenta",
                          person_id="1000000001", db_path=self.db)
        self.assertTrue(first)
        again = ix.search("practice of remembering", instance="residenta",
                          person_id="1000000001", db_path=self.db,
                          exclude_uids={first[0]["uid"]})
        self.assertNotIn(first[0]["uid"], {r["uid"] for r in again})

    def test_and_to_or_fallback(self):
        ix.rebuild(root=self.root, db_path=self.db)
        # both terms present in one record → AND finds it
        res = ix.search("conversation practice", instance="residenta",
                        person_id="1000000001", db_path=self.db)
        self.assertTrue(res)
        # terms split across records → AND finds nothing, OR falls back
        res = ix.search("conversation tonight", instance="residenta",
                        cross_person=True, db_path=self.db)
        self.assertTrue(res)

    def test_kill_switch(self):
        with unittest.mock.patch.dict(os.environ, {"CONTINUA_INDEX": "0"}):
            self.assertEqual(ix.search("memory", db_path=self.db), [])
            self.assertEqual(ix.rebuild(root=self.root, db_path=self.db), 0)

    def test_date_filters(self):
        ix.rebuild(root=self.root, db_path=self.db)
        res = ix.search("memory", instance="residenta", person_id="1000000001",
                        after="2026-09-07", db_path=self.db)
        self.assertEqual(res, [])
        res = ix.search("memory", instance="residenta", person_id="1000000001",
                        before="2026-09-06T23:59", db_path=self.db)
        self.assertTrue(res)


class TestDigestPrompt(unittest.TestCase):
    def test_prompt_building_agent_free(self):
        recs = [
            {"ts": "2026-09-07T08:00:00-07:00", "person_id": "1000000001",
             "role": "user", "content": "good morning"},
            {"ts": "2026-09-07T08:01:00-07:00", "person_id": "1000000001",
             "role": "assistant", "content": "Morning, Alex."},
        ]
        prompt = dg.build_user_prompt(recs, "2026-09-07")
        self.assertIn("DATE: 2026-09-07", prompt)
        self.assertIn("TURNS: 2", prompt)
        self.assertIn("Alex", prompt)  # roster names in the digest prompt

    def test_collect_day_reads_mirror(self):
        root = tempfile.mkdtemp(prefix="chronicle-dg-")
        _write_mirror(root, "residenta", "1000000001", "2026-09-07", [
            {"uid": "z1", "ts": "2026-09-07T08:00:00-07:00", "instance": "residenta",
             "person_id": "1000000001", "role": "user", "content": "hi"}])
        recs = dg.collect_day(root, "residenta", "2026-09-07")
        self.assertEqual(len(recs), 1)
        self.assertEqual(dg.collect_day(root, "residenta", "2020-01-01"), [])


if __name__ == "__main__":
    import unittest.mock
    unittest.main(verbosity=2)
