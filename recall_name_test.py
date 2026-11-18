"""recall_name_test.py — 2026-09-29 evening (residentb's investigation).
She called recall_my_experience(person='TolkienForest') and got "doesn't
exist": the person filter matched only raw numeric ids while the roster
carries the names she knows people by. Fixes tested here:
1. the tool resolves person names via the roster (send_message's exact
   resolution) before filtering;
2. the rendered lines carry the names she knows (not raw ids);
3. the retired search_my_memories description no longer advertises search.
Run: python3 recall_name_test.py
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import people          # noqa: E402
import recollections as rec   # noqa: E402
import core            # noqa: E402


class ResolveTests(unittest.TestCase):
    def test_roster_resolves_her_names(self):
        r = people.load_roster()
        pid, p = people.resolve_person(r, 'TolkienForest')
        self.assertEqual(pid, '3330001111')
        pid2, _ = people.resolve_person(r, 'Sequoia')
        self.assertEqual(pid2, '2220002222')
        # bare digits round-trip (the send_message contract)
        pid3, p3 = people.resolve_person(r, '2220002222')
        self.assertEqual(pid3, '2220002222')
        self.assertIsNotNone(p3)

    def test_unknown_name_round_trips(self):
        r = people.load_roster()
        pid, p = people.resolve_person(r, 'NobodyAtAll')
        self.assertEqual(pid, 'NobodyAtAll')
        self.assertIsNone(p)


class RecallHitsTests(unittest.TestCase):
    @staticmethod
    def _rev():
        # the recollections store is ops-live data; a synthetic accepted
        # revision exercises the same render path in the public tree
        return {'job': 'j1', 'instance': 'residentb',
                'text': 'a letter from Pinewood about the forest path',
                'visibility': ['3330001111'],
                'sources': [{'instance': 'residentb', 'person_id': '3330001111',
                             'role': 'user',
                             'content': 'a letter about the forest path',
                             'ts': '2026-11-14T10:00:00-08:00', 'uid': 'u1',
                             'ref': 'r1', 'hash': 'h1', 'path': 'x'}],
                'rendering': '', 'reason': 'remember',
                'event_start': '2026-11-14T10:00:00-08:00',
                'event_end': '2026-11-14T10:00:00-08:00',
                'review': {'pass': True}, 'validated_claims': []}

    def test_resolved_person_finds_episodes(self):
        r = people.load_roster()
        pid, _ = people.resolve_person(r, 'TolkienForest')
        res = rec.recall_experience('residentb', '', person=pid,
                                    revisions=[self._rev()])
        self.assertGreater(len(res.get('hits') or []), 0)
        self.assertIn('letter', res['text'].lower())

    def test_lines_render_roster_names(self):
        """The attribution line carries the name she knows, not the raw id."""
        r = people.load_roster()
        pid, _ = people.resolve_person(r, 'TolkienForest')
        res = rec.recall_experience('residentb', '', person=pid,
                                    revisions=[self._rev()])
        self.assertNotIn(f'with {pid}:', res['text'])
        self.assertIn('with TolkienForest:', res['text'])


class DescriptionTests(unittest.TestCase):
    def test_retired_tool_describes_itself_honestly(self):
        import inspect
        src = inspect.getsource(core)
        self.assertIn("RETIRED — searches nothing", src)
        # the old advertising sentence is gone from the whole module
        self.assertNotIn("Search your own long-term memory store for facts",
                         src)


if __name__ == "__main__":
    unittest.main()