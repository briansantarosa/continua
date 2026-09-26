"""Agent-free tests for the letter wire.

T3 multi-agent (2026-09-11): inboxes are PER RESIDENT — one resident's
check_mail can never see another resident's letters (the never-mix
ruling), and the control-server letter route carries the SENDER'S
per-instance inbox as reply_file.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mail as ml
import send as sd
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

class TestMailPerInstance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_dir = ml.MAIL_DIR
        ml.MAIL_DIR = self.tmp

    def tearDown(self):
        ml.MAIL_DIR = self.old_dir
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _deposit(self, instance, text, ts=None):
        letter = {"from_person_id": "555000777", "from_name": "penpal",
                  "text": f"letter to {instance}"}
        if ts:
            letter["ts"] = ts
        ml.deposit(letter, instance)

    def test_deposit_check_markread(self):
        ml.deposit({"from_person_id": "555000777", "from_name": "penpal",
                    "text": "hello persona-a"}, "residenta")
        ml.deposit({"from_person_id": "555000777", "from_name": "penpal",
                    "text": "second letter"}, "residenta")
        text, n = ml.check_mail("residenta")
        self.assertEqual(n, 2)
        self.assertIn("[UNREAD]", text)
        self.assertEqual(ml.unread_count("residenta"), 0)  # marked read
        # [house ruling 2026-09-12: real-inbox behavior] the read letters STAY
        # visible on the next check — flagged [read], still re-readable.
        text2, n2 = ml.check_mail("residenta")
        self.assertEqual(n2, 0)
        self.assertIn("[read]", text2)
        self.assertIn("hello persona-a", text2)
        self.assertIn("second letter", text2)

    def test_inboxes_are_per_instance_never_mix(self):
        """THE T3 contract: resident A's check_mail must never see
        resident B's letters. The old module-global single inbox was a
        silent cross-persona memory-mixing violation."""
        ml.deposit({"from_person_id": "777000111", "text": "for aa"},
                   "aa")
        ml.deposit({"from_person_id": "111", "text": "for bb"}, "bb")
        ta, na = ml.check_mail("aa")
        tb, nb = ml.check_mail("bb")
        self.assertEqual((na, nb), (1, 1))
        self.assertIn("for aa", ta)
        self.assertNotIn("for bb", ta)
        self.assertIn("for bb", tb)
        self.assertNotIn("for aa", tb)

    def test_bad_instance_id_fails_loud(self):
        with self.assertRaises(ValueError):
            ml.inbox_path("../escape")
        with self.assertRaises(ValueError):
            ml.inbox_path("ResidentA")  # uppercase — ids are [a-z0-9_]+

    def test_empty_inbox(self):
        text, n = ml.check_mail("residenta")
        self.assertEqual(n, 0)


class TestPersonaSendRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_outbox = sd.OUTBOX_DIR
        sd.OUTBOX_DIR = self.tmp

    def tearDown(self):
        sd.OUTBOX_DIR = self.old_outbox

    def test_persona_letter_routes_to_wire_not_telegram(self):
        posted = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return json.dumps(
                {"ok": True, "queued": True}).encode()

        def fake_urlopen(req, timeout):
            posted["body"] = json.loads(req.data.decode())
            posted["url"] = req.full_url
            return FakeResp()
        with patch.dict(os.environ, {"SAGENT_CONTROL_PORT": "9999",
                                     "SAGENT_CONTROL_TOKEN": "tok"}):
            with patch.object(__import__("urllib.request",
                                         fromlist=["urlopen"]),
                              "urlopen", fake_urlopen):
                _real_load = pp.load_roster
                _penpal_roster = _fixture_roster(
                    [{"id": "777000111", "name": "Penpal", "kind": "persona",
                      "can_message": True, "daily_cap": 20}])
                pp.load_roster = lambda **kw: _penpal_roster
                # a fake Sagent configs dir: _persona_config resolves the
                # persona by bot-id/token-prefix match; clear its cache too
                scfg = tempfile.mkdtemp()
                with open(os.path.join(scfg, "penpal.yaml"), "w") as fh:
                    fh.write("token: 777000111:FAKEPENPALTOKEN\n")
                _real_configs = sd.SAGENT_CONFIGS
                sd.SAGENT_CONFIGS = scfg
                sd._PERSONA_CONFIG_CACHE.clear()
                try:
                    entry = sd.send("residenta", "777000111", "a letter to a correspondent")
                finally:
                    pp.load_roster = _real_load
                    sd.SAGENT_CONFIGS = _real_configs
                    sd._PERSONA_CONFIG_CACHE.clear()
        self.assertTrue(entry.get("letter"), "ENTRY: " + json.dumps(entry))
        self.assertTrue(entry["queued"])
        # the letter carries the reply_file INSIDE Continua's mail inbox —
        # and per-instance now: the SENDER's own inbox file
        self.assertEqual(posted["body"]["reply_file"],
                         ml.inbox_path("residenta"))
        self.assertEqual(posted["body"]["user_id"], "continua:residenta")
        self.assertEqual(posted["body"]["config"], "penpal.yaml")
        self.assertNotIn("route", entry)  # control-server route, not inprocess


if __name__ == "__main__":
    unittest.main(verbosity=2)