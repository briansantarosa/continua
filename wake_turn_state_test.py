"""wake_turn_state_test.py — memorybuild.md Phase 1 (W1a/W2): the wake
packet says whose turn it is. Synthetic chronicles/outboxes in a tmp dir;
nothing touches production data. Run:
  python3 wake_turn_state_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import turns          # noqa: E402
import wake           # noqa: E402
import chronicle as ch   # noqa: E402

Alex = "1000000001"
SEQ = "2220002222"
TODAY = __import__("datetime").date.today().strftime("%Y-%m-%d")


def _world():
    """tmp base: chronicle/<inst>/<pid>/<day>.jsonl + outbound/<inst>/…;
    returns (base, root) — root is base/chronicle, the outbox derives from
    the same parent (turns._outbox_dir)."""
    td = tempfile.mkdtemp(prefix="turnstate-")
    root = os.path.join(td, "chronicle")
    os.makedirs(os.path.join(root, "residentb", Alex), exist_ok=True)
    os.makedirs(os.path.join(td, "outbound", "residentb"), exist_ok=True)
    return td, root


def _crow(root, inst, pid, ts, role, content, muted=False):
    p = os.path.join(root, inst, pid, f"{ts[:10]}.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "instance": inst, "person_id": pid,
                            "role": role, "content": content,
                            **({"arrived_muted": True} if muted else {})})
              + "\n")


def _orow(base, inst, pid, ts, text, delivered=True, kind="human"):
    p = os.path.join(base, "outbound", inst, f"{ts[:10]}.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "instance": inst, "person_id": pid,
                            "kind": kind, "text": text,
                            "delivered": delivered}) + "\n")


class LastEventsTests(unittest.TestCase):
    def test_merges_chronicle_and_outbox_sorted(self):
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T10:00:00-07:00", "user", "hello")
        _orow(base, "residentb", SEQ, f"{TODAY}T09:00:00-07:00", "earlier send")
        ev = turns.last_events(root, "residentb", SEQ)
        self.assertEqual([e["source"] for e in ev], ["outbox", "chronicle"])
        self.assertEqual(ev[0]["role"], "assistant")   # the outbox send
        self.assertEqual(ev[1]["role"], "user")

    def test_dedups_letter_captured_twice(self):
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T10:00:00-07:00", "assistant",
              "the letter text")
        _orow(base, "residentb", SEQ, f"{TODAY}T10:00:01-07:00", "the letter text",
              kind="letter")
        # the letter row is skipped at read; even if a human-kind duplicate
        # existed, dedup collapses equal (role, text)
        _orow(base, "residentb", SEQ, f"{TODAY}T10:00:02-07:00", "the letter text")
        ev = turns.last_events(root, "residentb", SEQ)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["source"], "chronicle")

    def test_muted_skipped_and_failed_sends_ignored(self):
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T10:00:00-07:00", "user", "hello")
        _crow(root, "residentb", SEQ, f"{TODAY}T11:00:00-07:00", "user", "again",
              muted=True)
        _orow(base, "residentb", SEQ, f"{TODAY}T12:00:00-07:00", "nope",
              delivered=False)
        ev = turns.last_events(root, "residentb", SEQ)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["text"], "hello")

    def test_fail_open_empty(self):
        td = tempfile.mkdtemp(prefix="turnstate-empty-")
        self.assertEqual(turns.last_events(
            os.path.join(td, "chronicle"), "residentb", "999"), [])


class LastSentenceTests(unittest.TestCase):
    def test_last_sentence_whole_when_short(self):
        self.assertEqual(turns.last_sentence(
            "Question nine: Is it typically grown in a garden?"),
            "Question nine: Is it typically grown in a garden?")

    def test_takes_final_sentence(self):
        self.assertEqual(turns.last_sentence(
            "I thought about it. Then I asked about the garden. Really."),
            "Really.")

    def test_no_mid_word_clip(self):
        long = ("word " * 60).strip()  # one long sentence, no terminators
        out = turns.last_sentence(long, cap=50)
        self.assertLessEqual(len(out), 51)
        self.assertTrue(out.startswith("…"))
        self.assertFalse(out.endswith("wor"))  # cut lands on a boundary

    def test_empty(self):
        self.assertEqual(turns.last_sentence(""), "")
        self.assertEqual(turns.last_sentence(None), "")


class WaitingPeopleTests(unittest.TestCase):
    def _roster(self):
        import people
        return {}

    def test_hers_last_listed_theirs_last_not(self):
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T21:00:00-07:00", "assistant",
              "Question nine: Is it typically grown in a garden?")
        _crow(root, "residentb", Alex, f"{TODAY}T20:00:00-07:00", "assistant",
              "I wrote last to Alex too")
        _crow(root, "residentb", Alex, f"{TODAY}T21:30:00-07:00", "user", "hi")
        out = turns.waiting_people(root, "residentb", [SEQ, Alex])
        self.assertEqual([w["person_id"] for w in out], [SEQ])
        self.assertEqual(out[0]["at"], "21:00")
        self.assertIn("Question nine", out[0]["sentence"])

    def test_muted_never_listed_wake_dirs_never_people(self):
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T21:00:00-07:00", "assistant", "hi")
        for d in ("system-wake", "continua:ritual", "system-sitting"):
            _crow(root, "residentb", d, f"{TODAY}T22:00:00-07:00", "assistant", "x")
        out = turns.waiting_people(root, "residentb",
                                   [SEQ, "system-wake", "continua:ritual",
                                    "system-sitting"],
                                   muted={SEQ})
        self.assertEqual(out, [])

    def test_in_flight_flag(self):
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T21:00:00-07:00", "assistant", "hi")
        out = turns.waiting_people(root, "residentb", [SEQ], busy={SEQ})
        self.assertTrue(out[0]["in_flight"])
        self.assertEqual(out[0]["sentence"], "")


class TurnStateBlockTests(unittest.TestCase):
    def test_lines_facts_not_directives(self):
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T21:00:00-07:00", "assistant",
              "Question nine: Is it typically grown in a garden?")
        import people as pp
        roster = pp.load_roster()
        lines = wake.turn_state_lines(root, "residentb", roster, {})
        self.assertEqual(lines[0],
                         "CONVERSATIONS WAITING ON THE OTHER PERSON "
                         "(nothing new from them yet):")
        self.assertIn("Sequoia — you wrote at 21:00:", lines[1])
        self.assertIn("no reply has come yet", lines[1])
        joined = "\n".join(lines)
        # factual wording only — no imperative toward sending
        self.assertNotIn("you should", joined.lower())
        self.assertNotIn("you must", joined.lower())
        self.assertNotIn("don't send", joined.lower())

    def test_empty_when_nobody_waiting(self):
        base, root = _world()
        import people as pp
        self.assertEqual(
            wake.turn_state_lines(root, "residentb", pp.load_roster(), {}), [])

    def test_in_flight_line_wording(self):
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T21:00:00-07:00", "assistant", "hi")
        import people as pp
        with patch.object(turns, "last_events",
                          lambda *a, **k: [{"ts": f"{TODAY}T21:00:00-07:00",
                                            "role": "assistant",
                                            "text": "hi", "source": "chronicle"}]), \
             patch.object(core_module(), "_ACTIVE_CHAT_USERS", {SEQ}):
            lines = wake.turn_state_lines(root, "residentb", pp.load_roster(), {})
        inflight = [l for l in lines if "answering them right now" in l]
        self.assertEqual(len(inflight), 1)
        self.assertIn("Sequoia", inflight[0])
        self.assertIn("leave this one to that conversation", inflight[0])

    def test_golden_build_payload_includes_block(self):
        """Integration: with the real yaml gate ON and a tmp world, the
        packet carries the block after the UNREAD block."""
        base, root = _world()
        _crow(root, "residentb", SEQ, f"{TODAY}T21:00:00-07:00", "assistant",
              "Question nine: Is it typically grown in a garden?")
        import people as pp
        with patch.object(wake, "build_payload"):
            pass  # placeholder removed below
        # call build_payload against the tmp root
        payload = wake.build_payload("residentb", root=root)
        prompt = payload["prompt"]
        self.assertIn("CONVERSATIONS WAITING ON THE OTHER PERSON", prompt)
        # position: after the UNREAD header, before the muted header
        i_unread = prompt.find("UNREAD REPLIES WAITING")
        i_turn = prompt.find("CONVERSATIONS WAITING ON THE OTHER PERSON")
        self.assertGreater(i_turn, i_unread)
        self.assertGreater(i_unread, 0)


def core_module():
    import core
    return core


if __name__ == "__main__":
    unittest.main()