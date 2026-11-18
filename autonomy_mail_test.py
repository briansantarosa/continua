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
        letter = {"from_person_id": "4440005555", "from_name": "maggie",
                  "text": f"letter to {instance}"}
        if ts:
            letter["ts"] = ts
        ml.deposit(letter, instance)

    def test_deposit_check_markread(self):
        ml.deposit({"from_person_id": "4440005555", "from_name": "maggie",
                    "text": "hello persona-a"}, "residenta")
        ml.deposit({"from_person_id": "4440005555", "from_name": "maggie",
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
        ml.deposit({"from_person_id": "4440005555", "text": "for aa"},
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
            ml.inbox_path("Residentx")  # uppercase — ids are [a-z0-9_]+

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
        import tempfile
        scfg = tempfile.mkdtemp()
        # the roster id IS the bot id; the persona's bridge config carries the
        # matching token prefix — a synthetic stand-in for Sagent's maggie.yaml
        with open(os.path.join(scfg, "maggie.yaml"), "w") as f:
            f.write("bot_token: 4440005555:AAFlSyntheticPlaceholder\n")
        sd._PERSONA_CONFIG_CACHE.clear()
        # SAGENT_CONFIGS is a module-level constant — patch the attribute,
        # not the env (the env is only read at import time)
        with patch.object(sd, "SAGENT_CONFIGS", scfg), \
             patch.dict(os.environ, {"SAGENT_CONTROL_PORT": "9999",
                                     "SAGENT_CONTROL_TOKEN": "tok",
                                     "SAGENT_CONFIGS": scfg}):
            with patch.object(__import__("urllib.request",
                                         fromlist=["urlopen"]),
                              "urlopen", fake_urlopen):
                entry = sd.send("residenta", "4440005555", "a letter to maggie")
        self.assertTrue(entry.get("letter"))
        self.assertTrue(entry["queued"])
        # the letter carries the reply_file INSIDE Continua's mail inbox —
        # and per-instance now: the SENDER's own inbox file
        self.assertEqual(posted["body"]["reply_file"],
                         ml.inbox_path("residenta"))
        self.assertEqual(posted["body"]["user_id"], "continua:residenta")
        self.assertEqual(posted["body"]["config"], "maggie.yaml")
        self.assertNotIn("route", entry)  # control-server route, not inprocess


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestTwoTierRender(unittest.TestCase):
    """[CONTINUA] 2026-10-07 (house ruling): check_mail renders NEW letters in
    full and OLD letters as a one-line index; read_letter re-reads any
    indexed letter whole by its ts handle. Born from the 21:25 residentb
    incident: 97 letters rendered whole (~126K chars) blew the 266K request
    cap and the turn died — with the fail-loud notice broken besides."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_dir = ml.MAIL_DIR
        ml.MAIL_DIR = self.tmp

    def tearDown(self):
        ml.MAIL_DIR = self.old_dir
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unread_full_read_indexed(self):
        long_text = "First sentence stays short. " + ("filler " * 60)
        ml.deposit({"from_person_id": "4440005555", "from_name": "maggie",
                    "text": long_text,
                    "ts": "2026-10-06T07:55:35-07:00"}, "residenta")
        ml.deposit({"from_person_id": "1000000001", "from_name": "Alex",
                    "text": "fresh letter, shown whole",
                    "ts": "2026-10-07T09:00:00-07:00"}, "residenta")
        text, n = ml.check_mail("residenta")
        self.assertEqual(n, 2)
        # NEW letter: full text on screen, NOT head-cut
        self.assertIn("fresh letter, shown whole", text)
        self.assertIn("filler", text)
        # OLD letter: one-line index carries the head + ts handle; the body
        # stays out of the render
        self.assertIn("[read] From maggie at 2026-10-06T07:55", text)
        self.assertIn("First sentence stays short.", text)
        self.assertNotIn(long_text, text)
        # second check: everything is now indexed — nothing full remains
        text2, n2 = ml.check_mail("residenta")
        self.assertEqual(n2, 0)
        self.assertIn("[read] From Alex at 2026-10-07T09:00", text2)
        self.assertNotIn("fresh letter, shown whole", text2)

    def test_read_letter_roundtrip(self):
        body = "The whole letter, every word of it, back on demand."
        ml.deposit({"from_person_id": "8559916511", "from_name": "persona-a",
                    "text": body, "ts": "2026-10-06T07:55:35-07:00"}, "residentb")
        ml.check_mail("residentb")  # marks read -> indexed on the next render
        out = ml.read_letter("residentb", "2026-10-06T07:55")
        self.assertIn("From persona-a at 2026-10-06T07:55:35-07:00", out)
        self.assertIn(body, out)
        # full ISO stamp hits the same letter, same render
        self.assertEqual(ml.read_letter("residentb", "2026-10-06T07:55:35-07:00"), out)
        # honest failures
        self.assertIn("no letter", ml.read_letter("residentb", "2026-01-01T00:00"))
        self.assertIn("[Tool error:", ml.read_letter("residentb", "   "))

    def test_read_letter_ambiguous_minute(self):
        ml.deposit({"from_person_id": "1", "from_name": "a", "text": "one",
                    "ts": "2026-10-06T07:55:10-07:00"}, "residenta")
        ml.deposit({"from_person_id": "1", "from_name": "a", "text": "two",
                    "ts": "2026-10-06T07:55:20-07:00"}, "residenta")
        out = ml.read_letter("residenta", "2026-10-06T07:55")
        self.assertIn("[Tool note:", out)
        self.assertIn("07:55:10", out)
        self.assertIn("07:55:20", out)
        self.assertIn("two", ml.read_letter("residenta", "2026-10-06T07:55:20-07:00"))

    def test_read_letter_never_mix(self):
        ml.deposit({"from_person_id": "1", "from_name": "a",
                    "text": "residenta-side letter",
                    "ts": "2026-10-06T07:55:35-07:00"}, "residenta")
        self.assertIn("no letter", ml.read_letter("residentb", "2026-10-06T07:55"))
