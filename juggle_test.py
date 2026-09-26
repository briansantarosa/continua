"""Work package B tests (memory plan §4a juggling, §6e-B, §6d.5).

Prove (the plan's own list):
  * rendering order and labels are exact (oldest activity first, participant
    + timestamp range, wake as its own labeled thread)
  * a 6-thread day drops whole oldest threads rather than truncating each
  * the active block always ends at the current message (this module never
    touches chat_history — asserted by never receiving it)
  * the 24h window excludes idle threads (they live in standing memory)
  * per-thread tail: whole messages, a tool result never separated from its
    call context
  * dedup: recollections whose sources sit inside a rendered window are
    suppressed and counted as dedup (verbatim beats summary)
  * stable between turns: identical inputs → identical bytes
  * fail-open: a broken history file yields no blocks, no crash

Offline fixtures only — no model calls, no production writes.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import juggle
import recollections as rec


def _iso(dt):
    return dt.isoformat()


def _base():
    return datetime(2026, 9, 19, 12, 0, 0)


def _mk(path, uid, msgs):
    (Path(path) / f"{uid}.json").write_text(json.dumps(msgs), encoding="utf-8")


class JuggleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.at = _base()

    def test_order_labels_and_window(self):
        # the designer active; persona-a active 2h ago; Alex active 5h ago → Alex first
        _mk(self.dir, "1000000001", [  # active — must be excluded
            {"role": "user", "content": "hello", "ts": _iso(self.at - timedelta(minutes=5))}])
        _mk(self.dir, "8737936808", [
            {"role": "user", "content": "from persona-a", "ts": _iso(self.at - timedelta(hours=2))},
            {"role": "assistant", "content": "to persona-a", "ts": _iso(self.at - timedelta(hours=2))}])
        _mk(self.dir, "111122223333", [
            {"role": "user", "content": "from Alex", "ts": _iso(self.at - timedelta(hours=5))},
            {"role": "assistant", "content": "to Alex", "ts": _iso(self.at - timedelta(hours=5))}])
        names = {"8737936808": "persona-a", "111122223333": "Alex"}
        res = juggle.assemble(self.dir, "1000000001", at=self.at, names=names)
        self.assertEqual([b["uid"] for b in res["blocks"]], ["111122223333", "8737936808"])
        self.assertIn("[Other conversations in my head]", res["text"])
        self.assertIn("With Alex — 2026-09-19 07:00", res["text"])
        self.assertIn("With persona-a — 2026-09-19 10:00", res["text"])
        self.assertIn("[07:00] Alex: from Alex", res["text"])
        self.assertIn("[10:00] Me: to persona-a", res["text"])
        self.assertIn("respond only in the active conversation", res["text"])
        # windows registered for dedup, per rendered thread
        self.assertEqual(len(res["windows"]), 2)
        self.assertIn(("8737936808", _iso(self.at - timedelta(hours=2)),
                       _iso(self.at - timedelta(hours=2))), res["windows"])

    def test_24h_window_excludes_idle(self):
        _mk(self.dir, "111122223333", [
            {"role": "user", "content": "old", "ts": _iso(self.at - timedelta(hours=30))},
            {"role": "assistant", "content": "old reply", "ts": _iso(self.at - timedelta(hours=30))}])
        _mk(self.dir, "8737936808", [
            {"role": "user", "content": "new", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "new reply", "ts": _iso(self.at - timedelta(hours=1))}])
        res = juggle.assemble(self.dir, "1000000001", at=self.at)
        self.assertEqual([b["uid"] for b in res["blocks"]], ["8737936808"])

    def test_cap_drops_whole_oldest(self):
        # §6d.5: cap ~5; a 6-thread day drops whole oldest threads
        uids = []
        for i in range(6):
            uid = f"{100000000000 + i}"
            uids.append(uid)
            _mk(self.dir, uid, [
                {"role": "user", "content": f"hi {i}", "ts": _iso(self.at - timedelta(hours=i + 1))},
                {"role": "assistant", "content": f"reply {i}", "ts": _iso(self.at - timedelta(hours=i + 1))}])
        res = juggle.assemble(self.dir, "1000000001", at=self.at, max_threads=5)
        rendered = [b["uid"] for b in res["blocks"]]
        self.assertEqual(len(rendered), 5)
        self.assertNotIn(uids[5], rendered)          # the oldest dropped whole
        self.assertIn(uids[0], rendered)             # the newest kept
        self.assertEqual(res["dropped"], [uids[5]])
        for b in res["blocks"]:                       # no truncation into fragments
            self.assertIn(f"reply {int(b['uid']) - 100000000000}", res["text"])

    def test_tail_pair_safety(self):
        msgs = [
            {"role": "user", "content": "q1", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "using tool", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "tool", "tool_call_id": "t1", "name": "sandbox_read",
             "content": "R" * 9000, "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "after tool", "ts": _iso(self.at - timedelta(minutes=30))},
            {"role": "user", "content": "final", "ts": _iso(self.at - timedelta(minutes=10))},
        ]
        window = juggle.tail_window(msgs, 400)
        # the tail must not OPEN on a tool result (its call context included)
        self.assertNotEqual(window[0].get("role"), "tool")
        self.assertLessEqual(len(json.dumps(window).encode("utf-8")), 400 + 5000)

    def test_wake_thread_labels(self):
        _mk(self.dir, "system-wake", [
            {"role": "user", "content": "YOUR TOOLS: ...", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "my wake musing", "ts": _iso(self.at - timedelta(minutes=50))},
            {"role": "user", "content": "packet 2", "ts": _iso(self.at - timedelta(minutes=20))},
            {"role": "assistant", "content": "second musing", "ts": _iso(self.at - timedelta(minutes=10))}])
        res = juggle.assemble(self.dir, "1000000001", at=self.at)
        self.assertIn("With My wake reflections — 2026-09-19 11:00", res["text"])
        self.assertIn("[wake packet — machinery voice]", res["text"])
        self.assertIn("[11:10] Me: my wake musing", res["text"])
        self.assertNotIn("YOUR TOOLS", res["text"])   # machinery voice not duplicated
        self.assertEqual(res["windows"][0][0], "system-wake")

    def test_stable_between_turns(self):
        _mk(self.dir, "8737936808", [
            {"role": "user", "content": "hello", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "hi", "ts": _iso(self.at - timedelta(minutes=59))}])
        a = juggle.assemble(self.dir, "1000000001", at=self.at)
        b = juggle.assemble(self.dir, "1000000001", at=self.at)
        self.assertEqual(a["text"], b["text"])
        self.assertEqual(a["bytes"], b["bytes"])

    def test_fail_open_on_broken_file(self):
        (Path(self.dir) / "8737936808.json").write_text("{not json", encoding="utf-8")
        res = juggle.assemble(self.dir, "1000000001", at=self.at)
        self.assertEqual(res["text"], "")
        self.assertEqual(res["blocks"], [])

    def test_budget_drops_whole_oldest(self):
        for i, size in enumerate((9000, 9000, 900)):
            uid = f"{200000000000 + i}"
            _mk(self.dir, uid, [
                {"role": "user", "content": "x" * size, "ts": _iso(self.at - timedelta(hours=i + 1))},
                {"role": "assistant", "content": "ok", "ts": _iso(self.at - timedelta(hours=i + 1))}])
        res = juggle.assemble(self.dir, "1000000001", at=self.at, budget_bytes=12000)
        rendered = [b["uid"] for b in res["blocks"]]
        # oldest threads drop WHOLE until the total fits; the newest (9K) fits
        self.assertEqual(rendered, ["200000000000"])
        self.assertEqual(res["dropped"], ["200000000002", "200000000001"])
        self.assertLessEqual(res["bytes"], 12000 + 800)

    def test_dedup_suppresses_covered_recollections(self):
        """§4a: deduplicate against standing memory — a recollection whose
        sources sit entirely inside a rendered window is suppressed."""
        at = _iso(self.at)
        # recollection source rows carry ref+ts with offsets in production
        inside = {"instance": "residentb", "person_id": "8737936808",
                  "ts": _iso(self.at - timedelta(hours=2)) + "-07:00",
                  "role": "user", "content": "from persona-a",
                  "ref": "residentb/1000000001/20260919T100000-a1b2c3d4.jsonl"}
        outside = {"instance": "residentb", "person_id": "8737936808",
                   "ts": _iso(self.at - timedelta(days=3)) + "-07:00",
                   "role": "user", "content": "old persona-a",
                   "ref": "residentb/1000000001/20260916T120000-e5f6a7b8.jsonl"}
        bodies = [
            {"instance": "residentb", "job": "job-inside", "text": "summary of the persona-a exchange",
             "event_start": inside["ts"], "event_end": inside["ts"],
             "visibility": ["8737936808"], "sources": [inside],
             "review": {"pass": True}},
            {"instance": "residentb", "job": "job-outside", "text": "old persona-a summary",
             "event_start": outside["ts"], "event_end": outside["ts"],
             "visibility": ["8737936808"], "sources": [outside],
             "review": {"pass": True}},
        ]
        _mk(self.dir, "8737936808", [
            {"role": "user", "content": "from persona-a", "ts": _iso(self.at - timedelta(hours=2))},
            {"role": "assistant", "content": "to persona-a", "ts": _iso(self.at - timedelta(hours=2))}])
        jres = juggle.assemble(self.dir, "1000000001", at=self.at,
                               names={"8737936808": "persona-a"})
        view = rec.select_view(bodies, "residentb", "1000000001", 100000,
                               at=_iso(self.at) + "-07:00",
                               names={"8737936808": "persona-a"},
                               dedup_windows=jres["windows"])
        jobs = [b["job"] for b in view["selected"]]
        self.assertNotIn("job-inside", jobs)          # verbatim above covers it
        self.assertIn("job-outside", jobs)            # outside the window stays
        self.assertEqual(view["dedup"], ["job-inside"])
        self.assertEqual(view["omitted"], [])         # dedup is not omission

    def test_active_history_never_touched(self):
        """The active block ends at the current message — the juggle takes no
        part in the active history; assemble() only reads OTHER files."""
        _mk(self.dir, "1000000001", [
            {"role": "user", "content": "ACTIVE THREAD", "ts": _iso(self.at - timedelta(minutes=1))}])
        _mk(self.dir, "8737936808", [
            {"role": "user", "content": "other", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "ok", "ts": _iso(self.at - timedelta(minutes=30))}])
        res = juggle.assemble(self.dir, "1000000001", at=self.at)
        self.assertNotIn("ACTIVE THREAD", res["text"])
        self.assertEqual([b["uid"] for b in res["blocks"]], ["8737936808"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
