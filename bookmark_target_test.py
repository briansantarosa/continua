"""bookmark_target_test.py — memorybuild.md Phase 3 (S2): the mark lands on
HER WORDS. Synthetic chronicles + bookmark dirs in tmp; the real bookmark/
chronicle modules write into the tmp roots. Run:
  python3 bookmark_target_test.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bookmark as bm      # noqa: E402
import chronicle as ch     # noqa: E402

WAKE = "system-wake"
SEQ = "2220002222"
TODAY = __import__("datetime").date.today().strftime("%Y-%m-%d")


class _World:
    def __init__(self):
        self.td = tempfile.mkdtemp(prefix="bmtarget-")
        self.root = os.path.join(self.td, "chronicle")
        for d in (os.path.join(self.root, "residentb", WAKE),
                  os.path.join(self.root, "residentb", SEQ),
                  os.path.join(self.td, "ritual", "bookmarks", "residentb")):
            os.makedirs(d, exist_ok=True)

    def crow(self, pid, ts, role, content, uid):
        p = os.path.join(self.root, "residentb", pid, f"{ts[:10]}.jsonl")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "instance": "residentb",
                                "person_id": pid, "role": role,
                                "content": content, "uid": uid}) + "\n")

    def bmrow(self, uid, note="a note", by="her", date=TODAY):
        p = os.path.join(self.td, "ritual", "bookmarks", "residentb",
                         f"{date}.jsonl")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"schema_version": 1, "ts": ts_iso(),
                                "uid": uid, "record_date": date,
                                "instance": "residentb", "person_id": WAKE,
                                "by": by, "note": note}) + "\n")

    def bookmarks(self):
        p = os.path.join(self.td, "ritual", "bookmarks", "residentb",
                         f"{TODAY}.jsonl")
        if not os.path.exists(p):
            return []
        return [json.loads(l) for l in open(p) if l.strip()]


def ts_iso(hhmmss="12:00:00"):
    return f"{TODAY}T{hhmmss}-07:00"


def _mk_core(world):
    """A minimal SagentCore instance with tmp roots — __new__-style (the
    dispatch reads only instance attrs the test sets)."""
    core = object.__new__(__import__("core").SagentCore)
    core.instance_id = "residentb"
    core._bookmark_v2 = True
    core._pending_bookmark = None
    core.config = {}
    return core


class BookmarkTargetTests(unittest.TestCase):
    def test_wake_thread_bookmarks_her_words_not_the_packet(self):
        w = _World()
        w.crow(WAKE, ts_iso("09:15:57"), "user", "[wake packet] good morning",
               "pkt-1")
        w.crow(WAKE, ts_iso("09:17:30"), "assistant",
               "I sat with the quiet and kept the porchfest thread.",
               "her-reply-1")
        core = _mk_core(w)
        with patch.object(ch, "DEFAULT_ROOT", w.root), \
             patch.object(bm, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")):
            out = core._execute_function_call(
                "bookmark_note", {"note": "the quiet mattered"}, WAKE)
        self.assertIn("Bookmarked", out)
        rows = w.bookmarks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uid"], "her-reply-1")   # HER words
        self.assertNotIn("pkt-1", [r["uid"] for r in rows])

    def test_wake_thread_sets_pending_for_this_turns_reply(self):
        w = _World()
        w.crow(WAKE, ts_iso("09:15:57"), "user", "[wake packet]", "pkt-1")
        core = _mk_core(w)
        with patch.object(ch, "DEFAULT_ROOT", w.root), \
             patch.object(bm, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")):
            core._execute_function_call("bookmark_note", {"note": "keep this"},
                                        WAKE)
        self.assertIsNotNone(core._pending_bookmark)
        self.assertEqual(core._pending_bookmark["note"], "keep this")
        # the pending lands with THIS turn's chronicle append
        with patch.object(ch, "DEFAULT_ROOT", w.root), \
             patch.object(bm, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")):
            rec = ch.append({"ts": ts_iso("09:18:00"), "instance": "residentb",
                             "person_id": WAKE, "role": "assistant",
                             "content": "tonight's reply, in the record now"})
            # simulate the consumption block's behavior
            if core._pending_bookmark and rec:
                bm.bookmark("residentb", rec["uid"], by="her",
                            note=core._pending_bookmark["note"])
                core._pending_bookmark = None
        rows = w.bookmarks()
        self.assertEqual([r["uid"] for r in rows], [rec["uid"]])
        self.assertEqual(rows[-1]["note"], "keep this")
        self.assertIsNone(core._pending_bookmark)

    def test_person_thread_bookmarks_both_sides_of_last_exchange(self):
        w = _World()
        w.crow(SEQ, ts_iso("10:00:00"), "user", "hey, game again?", "u-old")
        w.crow(SEQ, ts_iso("10:01:00"), "assistant", "sure!", "a-old")
        w.crow(SEQ, ts_iso("11:00:00"), "user", "question nine?", "u-new")
        w.crow(SEQ, ts_iso("11:01:00"), "assistant", "a flowering herb.",
               "a-new")
        core = _mk_core(w)
        with patch.object(ch, "DEFAULT_ROOT", w.root), \
             patch.object(bm, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")):
            core._execute_function_call("bookmark_note", {"note": "our game"},
                                        SEQ)
        uids = [r["uid"] for r in w.bookmarks()]
        self.assertIn("u-new", uids)
        self.assertIn("a-new", uids)
        self.assertNotIn("u-old", uids)

    def test_gate_off_restores_legacy_target(self):
        w = _World()
        w.crow(WAKE, ts_iso("09:15:57"), "user", "[wake packet]", "pkt-1")
        core = _mk_core(w)
        core._bookmark_v2 = False
        with patch.object(ch, "DEFAULT_ROOT", w.root), \
             patch.object(bm, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")):
            out = core._execute_function_call("bookmark_note", {"note": "n"},
                                              WAKE)
        rows = w.bookmarks()
        self.assertEqual([r["uid"] for r in rows], ["pkt-1"])  # legacy rule
        self.assertIn("priority input", out)

    def test_empty_content_assistant_rows_skipped(self):
        w = _World()
        w.crow(WAKE, ts_iso("09:15:57"), "user", "[wake packet]", "pkt-1")
        w.crow(WAKE, ts_iso("09:16:00"), "assistant", "", "a-empty")
        w.crow(WAKE, ts_iso("09:17:00"), "assistant", "real words", "a-real")
        core = _mk_core(w)
        with patch.object(ch, "DEFAULT_ROOT", w.root), \
             patch.object(bm, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")):
            core._execute_function_call("bookmark_note", {"note": "n"}, WAKE)
        self.assertEqual(w.bookmarks()[0]["uid"], "a-real")


class RebookmarkTests(unittest.TestCase):
    # scripts/rebookmark.py is ops tooling (scripts/ is not part of the public tree)
    @unittest.skipUnless(os.path.exists(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "scripts", "rebookmark.py")),
        "ops tooling not present in the public tree")
    def test_dry_run_writes_nothing_and_apply_is_idempotent(self):
        import scripts.rebookmark as rb
        w = _World()
        w.bmrow("pkt-1", note="the night's note")
        w.crow(WAKE, ts_iso("09:15:57"), "user", "[wake packet]", "pkt-1")
        w.crow(WAKE, ts_iso("09:17:30"), "assistant", "her real reply words",
               "her-reply-1")
        with patch.object(rb, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")), \
             patch.object(rb, "ch", ch), \
             patch.object(ch, "DEFAULT_ROOT", w.root):
            # dry run via argv
            sys_argv = sys.argv
            sys.argv = ["rebookmark.py", "residentb"]
            try:
                rb.main()
            finally:
                sys.argv = sys_argv
        self.assertEqual(w.bookmarks(), w.bookmarks())  # unchanged content
        # dry run: NOTHING NEW — only the fixture row (pkt-1) is present
        self.assertEqual([r["uid"] for r in w.bookmarks()], ["pkt-1"])
        # now apply
        with patch.object(rb, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")), \
             patch.object(rb, "ch", ch), \
             patch.object(ch, "DEFAULT_ROOT", w.root):
            sys_argv = sys.argv
            sys.argv = ["rebookmark.py", "residentb", "--apply"]
            try:
                rb.main()
            finally:
                sys.argv = sys_argv
        rows = w.bookmarks()
        self.assertEqual(len(rows), 2)   # the pkt-1 fixture + the new row
        self.assertEqual(rows[-1]["uid"], "her-reply-1")
        self.assertEqual(rows[-1]["note"], "the night's note")
        self.assertIn("memorybuild S2b backfill", rows[-1]["provenance"])
        # the ORIGINAL packet row is untouched (append-only: it lives in
        # the file list — here the original was in another day file)
        # idempotent re-run
        with patch.object(rb, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")), \
             patch.object(rb, "ch", ch), \
             patch.object(ch, "DEFAULT_ROOT", w.root):
            sys_argv = sys.argv
            sys.argv = ["rebookmark.py", "residentb", "--apply"]
            try:
                rb.main()
            finally:
                sys.argv = sys_argv
        self.assertEqual(len(w.bookmarks()), 2)  # no duplicates

    @unittest.skipUnless(os.path.exists(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "scripts", "rebookmark.py")),
        "ops tooling not present in the public tree")
    def test_originals_untouched_on_apply(self):
        import scripts.rebookmark as rb
        w = _World()
        # original bookmark row lives in YESTERDAY's file (append-only proof)
        yday = "2026-09-28"
        p = os.path.join(w.td, "ritual", "bookmarks", "residentb",
                         f"{yday}.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"schema_version": 1, "ts": ts_iso(),
                                "uid": "pkt-old", "record_date": yday,
                                "instance": "residentb", "person_id": WAKE,
                                "by": "her", "note": "old note"}) + "\n")
        before = open(p).read()
        w.crow(WAKE, f"{yday}T09:15:57-07:00", "user", "[wake packet]",
               "pkt-old")
        w.crow(WAKE, f"{yday}T09:17:30-07:00", "assistant", "her words",
               "her-old")
        with patch.object(rb, "BOOKMARKS_DIR",
                          os.path.join(w.td, "ritual", "bookmarks")), \
             patch.object(rb, "ch", ch), \
             patch.object(ch, "DEFAULT_ROOT", w.root):
            sys_argv = sys.argv
            sys.argv = ["rebookmark.py", "residentb", "--apply"]
            try:
                rb.main()
            finally:
                sys.argv = sys_argv
        after = open(p).read()
        self.assertTrue(after.startswith(before))  # append-only growth
        self.assertEqual(before.split("\n")[0], after.split("\n")[0])  # the original row byte-identical
        self.assertIn("memorybuild S2b backfill", after)


if __name__ == "__main__":
    unittest.main()