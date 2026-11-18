"""mute_test.py — agent-free tests for the mute feature (muteplan.md).

Covers: state file + fail-open, validations (humans only, can_mute
exemption, roster resolution, idempotency), send.py mute denial + post-
unmute behavior + arrived_muted filter in the no-reply cooldown, wake
unread/thread/counts filters, notice-once-per-episode, chronicle
arrived_muted passthrough, kill switch, and the PEOPLE YOU KNOW renderer.

Run:  python3 mute_test.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chronicle as ch
import mute as mu
import people as pp
import send as sd
import wake as wk

Alex = "1000000001"
ASHLEY = "8794791992"
PERSONA = "8740223704"


def _roster():
    return {
        Alex: pp.Person(Alex, "Alex", can_message=True, daily_cap=20),
        ASHLEY: pp.Person(ASHLEY, "Ashley", can_message=True, daily_cap=10),
        PERSONA: pp.Person(PERSONA, "Hakim_Sina", kind="persona",
                           can_message=True, chat_id=None, daily_cap=10),
    }


def _roster_exempt():
    r = _roster()
    r[Alex] = pp.Person(Alex, "Alex", can_message=True, can_mute=False)
    return r


class MuteTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_mute_dir = mu.MUTE_DIR
        self.old_root = ch.DEFAULT_ROOT
        mu.MUTE_DIR = self.tmp
        ch.DEFAULT_ROOT = self.tmp
        os.makedirs(os.path.join(self.tmp, "residenta", Alex), exist_ok=True)
        self.roster = _roster()

    def tearDown(self):
        mu.MUTE_DIR = self.old_mute_dir
        ch.DEFAULT_ROOT = self.old_root

    def _chron(self, person_id, role, content, ts=None, **kw):
        # root passed EXPLICITLY: ch.append's root default is bound at
        # import time — patching ch.DEFAULT_ROOT does not move it (the same
        # gotcha latest_reasoning documents). Forgetting this writes test
        # records into her real chronicle.
        rec = {"ts": ts or datetime.now().astimezone().isoformat(
                   timespec="seconds"),
               "instance": "residenta", "person_id": person_id, "role": role,
               "content": content}
        rec.update(kw)
        return ch.append(rec, root=self.tmp)


class TestMuteState(MuteTestBase):
    def test_mute_unmute_roundtrip(self):
        res = mu.mute("residenta", ASHLEY, "too much", by="self", roster=self.roster)
        self.assertTrue(res["ok"])
        self.assertTrue(mu.is_muted("residenta", ASHLEY))
        entry = mu.muted_entry("residenta", ASHLEY)
        self.assertEqual(entry["reason"], "too much")
        self.assertIsNone(entry["notice_last_ts"])
        mu.unmute("residenta", ASHLEY, roster=self.roster)
        self.assertFalse(mu.is_muted("residenta", ASHLEY))

    def test_mute_refuses_persona(self):
        res = mu.mute("residenta", PERSONA, "", roster=self.roster)
        self.assertFalse(res["ok"])
        self.assertIn("persona", res["error"])
        self.assertFalse(mu.is_muted("residenta", PERSONA))

    def test_mute_refuses_dan_exempt(self):
        res = mu.mute("residenta", Alex, "", roster=_roster_exempt())
        self.assertFalse(res["ok"])
        self.assertIn("exempt", res["error"])

    def test_mute_refuses_unknown(self):
        res = mu.mute("residenta", "999999", "", roster=self.roster)
        self.assertFalse(res["ok"])
        self.assertIn("roster", res["error"])

    def test_idempotent(self):
        mu.mute("residenta", ASHLEY, "first", roster=self.roster)
        res = mu.mute("residenta", ASHLEY, "second", roster=self.roster)
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("already"))
        self.assertEqual(res["entry"]["reason"], "first")  # original kept

    def test_unmute_when_not_muted(self):
        res = mu.unmute("residenta", ASHLEY, roster=self.roster)
        self.assertTrue(res["ok"])  # state already what she asked for
        self.assertEqual(res["error"], "was not muted")

    def test_fail_open_corrupt_state(self):
        mu.mute("residenta", ASHLEY, "", roster=self.roster)
        with open(mu.state_path("residenta"), "w") as f:
            f.write("{corrupt json")
        self.assertEqual(mu.load("residenta"), {})
        self.assertFalse(mu.is_muted("residenta", ASHLEY))

    def test_kill_switch(self):
        os.environ["CONTINUA_MUTE"] = "0"
        try:
            self.assertFalse(mu.enabled())
        finally:
            del os.environ["CONTINUA_MUTE"]

    def test_notice_once_per_episode(self):
        mu.mute("residenta", ASHLEY, "", roster=self.roster)
        self.assertTrue(mu.should_notice("residenta", ASHLEY))
        mu.mark_noticed("residenta", ASHLEY)
        self.assertFalse(mu.should_notice("residenta", ASHLEY))  # the designer: once per episode
        # a fresh episode (unmute → re-mute) may notice again
        mu.unmute("residenta", ASHLEY, roster=self.roster)
        mu.mute("residenta", ASHLEY, "", roster=self.roster)
        self.assertTrue(mu.should_notice("residenta", ASHLEY))

    def test_instance_id_validation(self):
        with self.assertRaises(ValueError):
            mu.state_path("../evil")


class TestSendGovernance(MuteTestBase):
    def test_send_denied_while_muted_with_unmute_direction(self):
        mu.mute("residenta", ASHLEY, "", roster=self.roster)
        ok, why = sd.governance_check("residenta", ASHLEY, self.roster,
                                      root=self.tmp)
        self.assertFalse(ok)
        self.assertIn("muted Ashley", why)
        self.assertIn("unmute_person", why)

    def test_send_allowed_again_after_unmute(self):
        mu.mute("residenta", ASHLEY, "", roster=self.roster)
        mu.unmute("residenta", ASHLEY, roster=self.roster)
        ok, _ = sd.governance_check("residenta", ASHLEY, self.roster,
                                    root=self.tmp)
        self.assertTrue(ok)

    def test_narrow_only_can_message_false_after_unmute(self):
        roster = _roster()
        roster[ASHLEY] = pp.Person(ASHLEY, "Ashley", can_message=False)
        mu.mute("residenta", ASHLEY, "", roster=roster)
        mu.unmute("residenta", ASHLEY, roster=roster)
        ok, why = sd.governance_check("residenta", ASHLEY, roster, root=self.tmp)
        self.assertFalse(ok)
        self.assertIn("allowlist", why)  # the designer's floor, not a mute artifact

    def test_can_message_false_checked_before_mute(self):
        # mute denial must never suggest unmute fixes a the designer-level off
        roster = _roster()
        roster[ASHLEY] = pp.Person(ASHLEY, "Ashley", can_message=False)
        mu.mute("residenta", ASHLEY, "", roster=roster)
        ok, why = sd.governance_check("residenta", ASHLEY, roster, root=self.tmp)
        self.assertFalse(ok)
        self.assertNotIn("unmute_person", why)

    def test_muted_arrivals_not_replies_no_reply_cooldown(self):
        # The cooldown counts OUTBOX sends (her real sends) vs chronicle
        # replies. 10 unanswered sends in 12h → cooldown; muted arrivals
        # between/after must NOT clear it; one REAL reply does.
        roster = _roster()
        roster[ASHLEY] = pp.Person(ASHLEY, "Ashley", can_message=True,
                                   daily_cap=0)  # no cap — isolate cooldown
        old_outbox = sd.OUTBOX_DIR
        sd.OUTBOX_DIR = self.tmp
        try:
            now = datetime.now().astimezone()
            os.makedirs(os.path.join(sd.OUTBOX_DIR, "residenta"), exist_ok=True)
            with open(sd.outbox_path("residenta", now.strftime("%Y-%m-%d")),
                      "w") as f:
                for i in range(10):
                    ts = (now - timedelta(minutes=90 - i)).isoformat(
                        timespec="seconds")
                    f.write(json.dumps({"person_id": ASHLEY, "ts": ts,
                                        "delivered": True}) + "\n")
            ok, why = sd.governance_check("residenta", ASHLEY, roster,
                                          root=self.tmp)
            self.assertFalse(ok)
            self.assertIn("no-reply", why)
            # 10 muted arrivals after her last send — still a paused channel
            for i in range(10):
                ts = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
                self._chron(ASHLEY, "user", f"muted burst {i}", ts=ts,
                            arrived_muted=True)
            ok, why = sd.governance_check("residenta", ASHLEY, roster,
                                          root=self.tmp)
            self.assertFalse(ok)
            self.assertIn("no-reply", why)  # archive ≠ conversation
            # a REAL reply clears it
            self._chron(ASHLEY, "user", "a real reply")
            ok, _ = sd.governance_check("residenta", ASHLEY, roster,
                                        root=self.tmp)
            self.assertTrue(ok)
        finally:
            sd.OUTBOX_DIR = old_outbox


class TestWakeFilters(MuteTestBase):
    def test_muted_latest_record_no_unread_pressure(self):
        self._chron(ASHLEY, "user", "hello before mute")
        self._chron(ASHLEY, "user", "hello while muted", arrived_muted=True)
        unread = wk.unread_replies(self.tmp, "residenta", lookback_days=1)
        self.assertEqual([u["person_id"] for u in unread], [])  # paused channel

    def test_muted_arrivals_ignored_in_unread(self):
        self._chron(ASHLEY, "user", "hello before mute")
        self._chron(ASHLEY, "user", "muted noise", arrived_muted=True)
        self._chron(ASHLEY, "assistant", "her reply")
        unread = wk.unread_replies(self.tmp, "residenta", lookback_days=1)
        self.assertEqual([u["person_id"] for u in unread], [])

    def test_recent_threads_skip_muted(self):
        self._chron(ASHLEY, "user", "genuine thread line")
        self._chron(ASHLEY, "user", "muted secret plans", arrived_muted=True)
        block = wk.recent_threads("residenta", root=self.tmp, days=2)
        self.assertIn("genuine thread", block)
        self.assertNotIn("muted secret", block)

    def test_since_last_wake_muted_not_chats(self):
        self._chron(ASHLEY, "user", "muted", arrived_muted=True)
        counts = wk.since_last_wake("residenta", root=self.tmp)
        # wake_*/done has no files → counts all zero regardless; assert the
        # muted record specifically isn't counted as a chat by patching done
        os.makedirs(os.path.join(self.tmp, "wakes", "residenta", "done"),
                    exist_ok=True)
        done = os.path.join(self.tmp, "wakes", "residenta", "done", "wake_x.json")
        old = datetime.now() - timedelta(hours=2)
        with open(done, "w") as f:
            json.dump({"prompt": "x"}, f)
        os.utime(done, (old.timestamp(), old.timestamp()))
        self._chron(ASHLEY, "user", "muted after wake", arrived_muted=True)
        counts = wk.since_last_wake("residenta", root=self.tmp)
        self.assertEqual(counts["chats"], 0)


class TestChroniclePassthrough(MuteTestBase):
    def test_arrived_muted_persisted(self):
        rec = self._chron(ASHLEY, "user", "while muted", arrived_muted=True)
        self.assertTrue(rec["arrived_muted"])
        path = ch.day_path(self.tmp, "residenta", ASHLEY, rec["ts"][:10])
        stored = list(ch.iter_records(path))[-1]
        self.assertTrue(stored.get("arrived_muted"))

    def test_default_false(self):
        rec = self._chron(ASHLEY, "user", "normal")
        self.assertFalse(rec["arrived_muted"])


class TestRosterRenderer(MuteTestBase):
    def test_parity_shapes(self):
        p = self.roster[ASHLEY]
        self.assertEqual(pp.roster_render_line(p),
                         f"- Ashley ({ASHLEY})")
        nomsg = pp.Person(ASHLEY, "Ashley", can_message=False)
        self.assertEqual(pp.roster_render_line(nomsg),
                         f"- Ashley ({ASHLEY}) — messaging off")
        line = pp.roster_render_line(p, {"ts": "2026-09-28T10:00:00+02:00",
                                         "reason": "needs quiet"})
        self.assertEqual(
            line,
            f"- Ashley ({ASHLEY}) — muted since 2026-09-28: needs quiet "
            "(contact paused; unmute to exchange)")
        line2 = pp.roster_render_line(p, {"ts": "2026-09-28T10:00:00+02:00"})
        self.assertIn("no reason recorded", line2)
        # muted marker wins over messaging-off (more specific live state)
        line3 = pp.roster_render_line(nomsg, {"ts": "2026-09-28T10:00:00+02:00"})
        self.assertIn("muted since", line3)
        self.assertNotIn("messaging off", line3)

    def test_resolve_person_matches_send_message(self):
        pid, person = pp.resolve_person(self.roster, "Ashley")
        self.assertEqual(pid, ASHLEY)
        pid, person = pp.resolve_person(self.roster, ASHLEY)
        self.assertEqual(pid, ASHLEY)
        pid, person = pp.resolve_person(self.roster, "nobody")
        self.assertEqual((pid, person), ("nobody", None))


class TestArchivedCount(MuteTestBase):
    def test_count_since_mute(self):
        mu.mute("residenta", ASHLEY, "", roster=self.roster)
        entry = mu.muted_entry("residenta", ASHLEY)
        before = (datetime.now().astimezone()
                  - timedelta(minutes=5)).isoformat(timespec="seconds")
        # an arrival BEFORE the mute ts shouldn't count (ts ordering guard)
        self._chron(ASHLEY, "user", "old", ts=before, arrived_muted=True)
        after = (datetime.now().astimezone()
                 + timedelta(seconds=1)).isoformat(timespec="seconds")
        self._chron(ASHLEY, "user", "new 1", ts=after, arrived_muted=True)
        self._chron(ASHLEY, "user", "new 2", arrived_muted=True)
        self._chron(ASHLEY, "user", "not muted")
        n = mu.archived_count_since("residenta", ASHLEY, entry["ts"])
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
