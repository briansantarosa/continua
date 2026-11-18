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
from unittest.mock import patch

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
        _mk(self.dir, "2220002222", [
            {"role": "user", "content": "from persona-a", "ts": _iso(self.at - timedelta(hours=2))},
            {"role": "assistant", "content": "to persona-a", "ts": _iso(self.at - timedelta(hours=2))}])
        _mk(self.dir, "111122223333", [
            {"role": "user", "content": "from Alex", "ts": _iso(self.at - timedelta(hours=5))},
            {"role": "assistant", "content": "to Alex", "ts": _iso(self.at - timedelta(hours=5))}])
        names = {"2220002222": "persona-a", "111122223333": "Alex"}
        res = juggle.assemble(self.dir, "1000000001", at=self.at, names=names)
        self.assertEqual([b["uid"] for b in res["blocks"]], ["111122223333", "2220002222"])
        self.assertIn("[Other conversations in my head]", res["text"])
        self.assertIn("With Alex — 2026-09-19 07:00", res["text"])
        self.assertIn("With persona-a — 2026-09-19 10:00", res["text"])
        self.assertIn("[07:00] Alex: from Alex", res["text"])
        self.assertIn("[10:00] Me: to persona-a", res["text"])
        self.assertIn("respond only in the active conversation", res["text"])
        # windows registered for dedup, per rendered thread
        self.assertEqual(len(res["windows"]), 2)
        self.assertIn(("2220002222", _iso(self.at - timedelta(hours=2)),
                       _iso(self.at - timedelta(hours=2))), res["windows"])

    def test_24h_window_excludes_idle(self):
        _mk(self.dir, "111122223333", [
            {"role": "user", "content": "old", "ts": _iso(self.at - timedelta(hours=30))},
            {"role": "assistant", "content": "old reply", "ts": _iso(self.at - timedelta(hours=30))}])
        _mk(self.dir, "2220002222", [
            {"role": "user", "content": "new", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "new reply", "ts": _iso(self.at - timedelta(hours=1))}])
        res = juggle.assemble(self.dir, "1000000001", at=self.at)
        self.assertEqual([b["uid"] for b in res["blocks"]], ["2220002222"])

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
        _mk(self.dir, "2220002222", [
            {"role": "user", "content": "hello", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "hi", "ts": _iso(self.at - timedelta(minutes=59))}])
        a = juggle.assemble(self.dir, "1000000001", at=self.at)
        b = juggle.assemble(self.dir, "1000000001", at=self.at)
        self.assertEqual(a["text"], b["text"])
        self.assertEqual(a["bytes"], b["bytes"])

    def test_fail_open_on_broken_file(self):
        (Path(self.dir) / "2220002222.json").write_text("{not json", encoding="utf-8")
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
        inside = {"instance": "residentb", "person_id": "2220002222",
                  "ts": _iso(self.at - timedelta(hours=2)) + "-07:00",
                  "role": "user", "content": "from persona-a",
                  "ref": "residentb/1000000001/20260919T100000-a1b2c3d4.jsonl"}
        outside = {"instance": "residentb", "person_id": "2220002222",
                   "ts": _iso(self.at - timedelta(days=3)) + "-07:00",
                   "role": "user", "content": "old persona-a",
                   "ref": "residentb/1000000001/20260916T120000-e5f6a7b8.jsonl"}
        bodies = [
            {"instance": "residentb", "job": "job-inside", "text": "summary of the persona-a exchange",
             "event_start": inside["ts"], "event_end": inside["ts"],
             "visibility": ["2220002222"], "sources": [inside],
             "review": {"pass": True}},
            {"instance": "residentb", "job": "job-outside", "text": "old persona-a summary",
             "event_start": outside["ts"], "event_end": outside["ts"],
             "visibility": ["2220002222"], "sources": [outside],
             "review": {"pass": True}},
        ]
        _mk(self.dir, "2220002222", [
            {"role": "user", "content": "from persona-a", "ts": _iso(self.at - timedelta(hours=2))},
            {"role": "assistant", "content": "to persona-a", "ts": _iso(self.at - timedelta(hours=2))}])
        jres = juggle.assemble(self.dir, "1000000001", at=self.at,
                               names={"2220002222": "persona-a"})
        view = rec.select_view(bodies, "residentb", "1000000001", 100000,
                               at=_iso(self.at) + "-07:00",
                               names={"2220002222": "persona-a"},
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
        _mk(self.dir, "2220002222", [
            {"role": "user", "content": "other", "ts": _iso(self.at - timedelta(hours=1))},
            {"role": "assistant", "content": "ok", "ts": _iso(self.at - timedelta(minutes=30))}])
        res = juggle.assemble(self.dir, "1000000001", at=self.at)
        self.assertNotIn("ACTIVE THREAD", res["text"])
        self.assertEqual([b["uid"] for b in res["blocks"]], ["2220002222"])


class JuggleClipMarkerTests(unittest.TestCase):
    """2026-10-08 (residentc's packet-honesty ask, the designer go): a clipped tail is
    labeled as clipped — completeness is stated as a fact, never silent.
    Kill switch CONTINUA_JUGGLE_MARKERS=0 restores the legacy render."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.at = _base()

    def _thread(self, rows=12):
        return [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"line {i}",
                 "ts": _iso(self.at - timedelta(minutes=90 - i))}
                for i in range(rows)]

    def test_clipped_thread_states_omission(self):
        msgs = self._thread(12)
        _mk(self.dir, "111122223333", msgs)
        window = juggle.tail_window(msgs, 300)
        n_omitted = len(msgs) - len(window)
        self.assertGreater(n_omitted, 0)
        res = juggle.assemble(self.dir, "1000000001", at=self.at,
                              thread_bytes=300)
        self.assertIn("the earlier part of this thread is not shown",
                      res["text"])
        self.assertIn(f"{n_omitted} earlier message", res["text"])
        self.assertIn("recall_my_experience finds them", res["text"])

    def test_unclipped_thread_carries_no_marker(self):
        _mk(self.dir, "111122223333", self._thread(2))
        res = juggle.assemble(self.dir, "1000000001", at=self.at,
                              thread_bytes=10000)
        self.assertNotIn("the earlier part of this thread", res["text"])

    def test_kill_switch_restores_legacy_bytes(self):
        msgs = self._thread(12)
        _mk(self.dir, "111122223333", msgs)
        res_marked = juggle.assemble(self.dir, "1000000001", at=self.at,
                                     thread_bytes=300)
        self.assertIn("the earlier part of this thread", res_marked["text"])
        with patch.dict(os.environ, {"CONTINUA_JUGGLE_MARKERS": "0"}):
            res_legacy = juggle.assemble(self.dir, "1000000001", at=self.at,
                                         thread_bytes=300)
        self.assertNotIn("the earlier part of this thread",
                         res_legacy["text"])
        # legacy is the marked render minus exactly the note line
        legacy_expected = "\n".join(
            l for l in res_marked["text"].split("\n")
            if "earlier part" not in l)
        self.assertEqual(res_legacy["text"], legacy_expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ContinuaAliasTests(unittest.TestCase):
    """2026-10-08 letters rekey: legacy continua:<instance> uids resolve by
    roster display name — a live friendship never renders as a machine
    string ('person-continua:residentc' is how residentb guessed Alex). Unknown
    stays honest."""

    def setUp(self):
        self.names = {"8703803298": "residentc", "111122223333": "Alex"}
        self.msgs = [{"role": "user", "content": "an unsigned letter",
                      "ts": _iso(_base())}]

    def test_header_resolves_continua_alias(self):
        text, *_ = juggle.render_thread(
            "continua:residentc", self.msgs, names=self.names)
        self.assertIn("With residentc —", text)
        self.assertNotIn("person-continua", text)

    def test_speaker_line_resolves_continua_alias(self):
        text, *_ = juggle.render_thread(
            "continua:residentc", self.msgs, names=self.names)
        self.assertIn("residentc: an unsigned letter", text)

    def test_unknown_continua_stays_honest(self):
        text, *_ = juggle.render_thread(
            "continua:zzz", self.msgs, names=self.names)
        self.assertIn("person-continua:zzz", text)
        self.assertNotIn("With residentc", text)

    def test_pid_direct_lookup_unaffected(self):
        text, *_ = juggle.render_thread(
            "8703803298", self.msgs, names=self.names)
        self.assertIn("With residentc —", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
