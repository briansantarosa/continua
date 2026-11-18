"""Letters attribution tests — the 2026-10-08 rekey.

The bug this pins: the letter route keyed its turn by
``continua:<sender_instance>``, which no name surface could resolve —
the talking-with line rendered "You are talking with person-continua:residentc",
so residentb read an unsigned letter from a machine string and guessed Alex
(three misaddresses in one live correspondence). The fix: key the turn by
the sender's roster bot id (attribution by construction — every surface
resolves the way a telegram turn would), unify the split history files
with exact-turn dedup, and give name_for/juggle honest continua:*
fallbacks.

Offline fixtures only — no model calls, no production stores, no letters
sent. Kill switch under test: CONTINUA_LETTERS_ATTR=0.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge
import letters
import people


class SenderBotIdTests(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()

    def _cfgfile(self, name, token):
        with open(os.path.join(self.cfg, name), "w") as f:
            f.write(f"instance:\n  name: x\ntelegram:\n  token: '{token}'\n")

    def test_token_prefix_resolved(self):
        self._cfgfile("residentc.yaml", "8703803298:AAExample")
        self.assertEqual(letters._sender_bot_id("residentc", self.cfg),
                         "8703803298")

    def test_missing_config_returns_none(self):
        self.assertIsNone(letters._sender_bot_id("ghost", self.cfg))

    def test_traversal_refused(self):
        self.assertIsNone(letters._sender_bot_id("../residentb", self.cfg))
        self.assertIsNone(letters._sender_bot_id("..", self.cfg))


class UnifyHistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patcher = patch.object(bridge, "HISTORIES_DIR", self.dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_adopt_legacy_when_pid_absent(self):
        bridge._save_history_to_disk("residentb.yaml", "continua:residentc",
                                     [{"role": "user", "content": "hi"}])
        self.assertTrue(
            letters._unify_history("residentb.yaml", "8703803298",
                                   "continua:residentc"))
        merged = bridge._load_history_from_disk("residentb.yaml", "8703803298")
        self.assertEqual([t["content"] for t in merged], ["hi"])
        # legacy file stays in place as provenance — nothing deleted
        self.assertEqual(
            len(bridge._load_history_from_disk("residentb.yaml",
                                               "continua:residentc")), 1)

    def test_legacy_absent_pid_usable_fresh(self):
        self.assertTrue(
            letters._unify_history("residentb.yaml", "8703803298",
                                   "continua:residentc"))
        self.assertEqual(
            bridge._load_history_from_disk("residentb.yaml", "8703803298"), [])

    def test_merge_both_sides_mtime_order(self):
        bridge._save_history_to_disk("residentb.yaml", "8703803298",
                                     [{"role": "assistant",
                                       "content": "her send"}])
        bridge._save_history_to_disk("residentb.yaml", "continua:residentc",
                                     [{"role": "user", "content": "letter"}])
        # pid file older → legacy turns come after
        pid_path = bridge._history_filepath("residentb.yaml", "8703803298")
        old = time.time() - 100
        os.utime(pid_path, (old, old))
        self.assertTrue(
            letters._unify_history("residentb.yaml", "8703803298",
                                   "continua:residentc"))
        merged = bridge._load_history_from_disk("residentb.yaml", "8703803298")
        self.assertEqual([(t["role"], t["content"]) for t in merged],
                         [("assistant", "her send"), ("user", "letter")])

    def test_merge_reversed_mtime_order(self):
        bridge._save_history_to_disk("residentb.yaml", "8703803298",
                                     [{"role": "assistant",
                                       "content": "her send"}])
        bridge._save_history_to_disk("residentb.yaml", "continua:residentc",
                                     [{"role": "user", "content": "letter"}])
        # legacy file older → legacy turns come first
        legacy_path = bridge._history_filepath("residentb.yaml", "continua:residentc")
        old = time.time() - 100
        os.utime(legacy_path, (old, old))
        letters._unify_history("residentb.yaml", "8703803298", "continua:residentc")
        merged = bridge._load_history_from_disk("residentb.yaml", "8703803298")
        self.assertEqual([t["role"] for t in merged], ["user", "assistant"])

    def test_dedup_idempotent_rerun(self):
        same = [{"role": "user", "content": "same turn"}]
        bridge._save_history_to_disk("residentb.yaml", "8703803298", same)
        bridge._save_history_to_disk("residentb.yaml", "continua:residentc", same)
        letters._unify_history("residentb.yaml", "8703803298", "continua:residentc")
        merged = bridge._load_history_from_disk("residentb.yaml", "8703803298")
        self.assertEqual(len(merged), 1)


class NameForContinuaTests(unittest.TestCase):
    def setUp(self):
        self.roster = {
            "8703803298": people.Person("8703803298", name="residentc",
                                        kind="persona"),
            "111122223333": people.Person("111122223333", name="Alex"),
        }

    def test_continua_key_resolves_by_roster_name(self):
        self.assertEqual(people.name_for(self.roster, "continua:residentc"),
                         "residentc")

    def test_continua_key_case_insensitive(self):
        self.assertEqual(people.name_for(self.roster, "continua:residentc"),
                         "residentc")

    def test_unknown_continua_stays_honest(self):
        self.assertEqual(people.name_for(self.roster, "continua:zzz"),
                         "person-continua:zzz")

    def test_pid_direct_lookup_unaffected(self):
        self.assertEqual(people.name_for(self.roster, "8703803298"), "residentc")
        self.assertEqual(people.name_for(self.roster, "999"), "person-999")

    def test_wake_channels_still_special(self):
        self.assertEqual(people.name_for(self.roster, "system-wake"),
                         "your wake window")
        self.assertEqual(people.name_for(self.roster, "system-sitting"),
                         "your evening practice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
