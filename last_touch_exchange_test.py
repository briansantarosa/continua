"""last_touch_exchange_test.py — memorybuild.md Phase 1 (W2): the roster
clause shows what a reply answered. Synthetic chronicle/outbox in tmp;
run: python3 last_touch_exchange_test.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import last_touch as lt   # noqa: E402
import turns              # noqa: E402

SEQ = "2220002222"
TODAY = __import__("datetime").date.today().strftime("%Y-%m-%d")


def _world():
    td = tempfile.mkdtemp(prefix="ltx-")
    root = os.path.join(td, "chronicle")
    os.makedirs(os.path.join(root, "residentb", SEQ), exist_ok=True)
    os.makedirs(os.path.join(td, "outbound", "residentb"), exist_ok=True)
    return td, root


def _crow(root, ts, role, content):
    p = os.path.join(root, "residentb", SEQ, f"{ts[:10]}.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "instance": "residentb", "person_id": SEQ,
                            "role": role, "content": content}) + "\n")


def _orow(td, ts, text):
    p = os.path.join(td, "outbound", "residentb", f"{ts[:10]}.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "instance": "residentb", "person_id": SEQ,
                            "kind": "human", "text": text,
                            "delivered": True}) + "\n")


class BuildIndexExchangeTests(unittest.TestCase):
    def _index(self, root, td):
        lt._CACHE["key"] = None  # bust the cache for the tmp world
        try:
            return lt.build_index("residentb", root)
        finally:
            lt._CACHE["key"] = None

    def test_theirs_short_carries_the_pair(self):
        td, root = _world()
        _crow(root, f"{TODAY}T20:00:00-07:00", "assistant",
              "Question nine: Is it typically grown in a garden?")
        _crow(root, f"{TODAY}T21:30:00-07:00", "user", "Fungus")
        idx = self._index(root, td)
        x = idx[SEQ]["exchange"]
        self.assertEqual(x["last_role"], "user")
        self.assertEqual(x["prev_role"], "assistant")
        self.assertIn("Question nine", x["prev_text"])
        clause = lt.context_clause(SEQ, idx[SEQ], exchange=True)
        self.assertIn('you wrote: "Question nine: Is it typically grown in '
                      'a garden?"', clause)
        self.assertIn('they said: "Fungus"', clause)
        self.assertIn("last contact today", clause)

    def test_hers_last_no_reply_yet(self):
        td, root = _world()
        _crow(root, f"{TODAY}T21:00:00-07:00", "assistant",
              "I said goodnight and closed the door. Rest well, house.")
        idx = self._index(root, td)
        clause = lt.context_clause(SEQ, idx[SEQ], exchange=True)
        self.assertIn('you wrote last: "Rest well, house."', clause)
        self.assertIn("(no reply yet)", clause)

    def test_outbox_send_is_her_word(self):
        td, root = _world()
        _orow(td, f"{TODAY}T18:49:09-07:00", "No bulb. That narrows it down.")
        idx = self._index(root, td)
        x = idx[SEQ]["exchange"]
        self.assertEqual(x["last_role"], "assistant")
        self.assertEqual(x["source"], "outbox")
        clause = lt.context_clause(SEQ, idx[SEQ], exchange=True)
        self.assertIn('you wrote last: "That narrows it down."', clause)

    def test_theirs_long_keeps_legacy_rendering(self):
        td, root = _world()
        _crow(root, f"{TODAY}T20:00:00-07:00", "assistant", "I asked something")
        long_reply = "It is typically grown in a garden, and it flowers all summer long."
        _crow(root, f"{TODAY}T21:30:00-07:00", "user", long_reply)
        idx = self._index(root, td)
        clause = lt.context_clause(SEQ, idx[SEQ], exchange=True)
        self.assertIn('they said: "' + long_reply[:90][:85], clause[:200])
        self.assertNotIn("you wrote:", clause)

    def test_gate_off_byte_identical_legacy(self):
        td, root = _world()
        _crow(root, f"{TODAY}T20:00:00-07:00", "assistant",
              "Question nine: Is it typically grown in a garden?")
        _crow(root, f"{TODAY}T21:30:00-07:00", "user", "Fungus")
        idx = self._index(root, td)
        self.assertEqual(lt.context_clause(SEQ, idx[SEQ], exchange=False),
                         lt.context_clause(SEQ, idx[SEQ]))
        # and the legacy shape: 'they said: "<excerpt>"' from the chronicle
        self.assertIn('they said: "Fungus"',
                      lt.context_clause(SEQ, idx[SEQ], exchange=False))

    def test_cache_busts_on_outbox_change(self):
        td, root = _world()
        _crow(root, f"{TODAY}T20:00:00-07:00", "assistant", "I asked something")
        idx = self._index(root, td)
        self.assertEqual(idx[SEQ]["exchange"]["last_role"], "assistant")
        # an outbox row is HER send (outbound = words she sent) — her word
        # becomes LATER and now rides the outbox source
        _orow(td, f"{TODAY}T21:30:00-07:00", "No bulb. That narrows it down.")
        os.utime(os.path.join(td, "outbound", "residentb",
                              f"{TODAY}.jsonl"), (1e9 + 5, 1e9 + 5))
        idx2 = self._index(root, td)
        x2 = idx2[SEQ]["exchange"]
        self.assertEqual(x2["last_role"], "assistant")
        self.assertEqual(x2["source"], "outbox")
        self.assertEqual(x2["last_ts"][:16], f"{TODAY}T21:30")

    def test_outbox_only_person_gets_an_entry(self):
        td, root = _world()
        # no chronicle dir for this person at all
        NEW = "9999999999"
        _orow(td, f"{TODAY}T18:49:09-07:00", "first contact, from a wake")
        # point the outbox row at the new person instead
        p = os.path.join(td, "outbound", "residentb", f"{TODAY}.jsonl")
        rows = [json.loads(l) for l in open(p, encoding="utf-8")]
        rows[-1]["person_id"] = NEW
        open(p, "w").writelines(json.dumps(r) + "\n" for r in rows)
        idx = self._index(root, td)
        self.assertIn(NEW, idx)
        self.assertEqual(idx[NEW]["exchange"]["last_role"], "assistant")
        clause = lt.context_clause(NEW, idx[NEW], exchange=True)
        self.assertIn('you wrote last: "first contact, from a wake"', clause)

    def test_wake_class_dirs_excluded(self):
        td, root = _world()
        os.makedirs(os.path.join(root, "residentb", "system-wake"), exist_ok=True)
        p = os.path.join(root, "residentb", "system-wake", f"{TODAY}.jsonl")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": f"{TODAY}T10:00:00-07:00",
                                "instance": "residentb", "person_id": "system-wake",
                                "role": "assistant", "content": "wake reply"})
                    + "\n")
        idx = self._index(root, td)
        # wake dirs keep their legacy entry (documented contract: available
        # to callers, filtered by the roster join) but get NO exchange
        # machinery — they are not people
        self.assertIn("last_role", idx["system-wake"])
        self.assertNotIn("exchange", idx["system-wake"])


if __name__ == "__main__":
    unittest.main()