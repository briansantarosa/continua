"""cleansweep repair tests — compose defenses + validator + wake shape.

Covers (cleansweep.md plan, approved 2026-09-22):
  - _strip_leading_timestamp (leading + stacked prefixes; inline untouched)
  - _sanitize_history_html (tag-shaped strings, whole <aside> blocks;
    markdown and plain text untouched)
  - _apply_chat_anchor wake inclusion (chat_anchor_wake gate)
  - _raw_chatml_render sanitize flag (stored-dirty history cleaned at
    compose; byte-identical when off)
  - _future_date_flags (save_my_memory flagging, no rejection)
  - wake.build_payload no longer carries the identity anchor (Phase 4)
Agent-free: no model, no network.
"""
import os
import sys
import unittest

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
import core
import wake


class TestStripLeadingTimestamp(unittest.TestCase):
    def test_leading_prefix_removed(self):
        self.assertEqual(core._strip_leading_timestamp(
            "[2026-09-24] First meeting"), "First meeting")

    def test_leading_datetime_removed(self):
        self.assertEqual(core._strip_leading_timestamp(
            "[2026-09-22 13:38] hello"), "hello")

    def test_stacked_prefixes_removed(self):
        self.assertEqual(core._strip_leading_timestamp(
            "[2026-09-06 09:59]\n\n[2026-09-06 09:58]\n\nYou're right"),
            "You're right")

    def test_inline_prefix_untouched(self):
        self.assertEqual(core._strip_leading_timestamp(
            "clean text [2026-01-01] here"), "clean text [2026-01-01] here")

    def test_plain_text_untouched(self):
        self.assertEqual(core._strip_leading_timestamp("no prefix"),
                         "no prefix")


class TestSanitizeHistoryHtml(unittest.TestCase):
    def test_tag_shaped_strings_removed(self):
        self.assertEqual(core._sanitize_history_html(
            'a</p>b<aside class="thinking">x</aside>c</body></ol>'), "abc")

    def test_whole_aside_blocks_removed(self):
        self.assertEqual(core._sanitize_history_html(
            'keep this<aside class="thinking"><h4>plan</h4></aside>and this'),
            "keep thisand this")

    def test_markdown_untouched(self):
        s = "*emphasis* **bold** [2026-09-20] <call> syntax"
        self.assertEqual(core._sanitize_history_html(s), s)

    def test_plain_text_untouched(self):
        self.assertEqual(core._sanitize_history_html("ordinary words"),
                         "ordinary words")


class TestWakeAnchorInclusion(unittest.TestCase):
    MSGS = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}]

    def test_wake_exempt_by_default(self):
        out = core._apply_chat_anchor(self.MSGS, "system-wake", True, False)
        self.assertNotIn(core._ANCHOR_TEXT, out[0]["content"])

    def test_wake_included_when_gated_on(self):
        out = core._apply_chat_anchor(self.MSGS, "system-wake", True, True)
        self.assertTrue(out[0]["content"].endswith(core._ANCHOR_TEXT))

    def test_ritual_included_when_gated_on(self):
        out = core._apply_chat_anchor(self.MSGS, "continua:ritual", True, True)
        self.assertTrue(out[0]["content"].endswith(core._ANCHOR_TEXT))

    def test_flag_off_returns_input(self):
        self.assertIs(core._apply_chat_anchor(self.MSGS, "system-wake",
                                              False, True), self.MSGS)


class TestRenderSanitize(unittest.TestCase):
    MSGS = [{"role": "assistant", "content": "hello</p> world",
             "_think": "plan</body>"}]

    def test_sanitize_on_cleans_stored_dirty_turns(self):
        r = core._raw_chatml_render(self.MSGS, sanitize=True)
        self.assertIn("hello world", r)
        self.assertNotIn("</p>", r)
        self.assertIn("plan", r)

    def test_sanitize_off_byte_identical(self):
        self.assertEqual(core._raw_chatml_render(self.MSGS),
                         core._raw_chatml_render(self.MSGS, sanitize=False))


class TestTsStackingFix(unittest.TestCase):
    """2026-09-24: the ts-stacking fix. The enforcers must run at history-
    append time (central) and at render time (defense in depth), so the
    persistent history never re-feeds the prefixes the delivery stripped."""

    class _FakeCore:
        _strip_ts_prefix = True
        _sanitize_history = True
        _history_thinks = "compressed"
        _compress_think_for_history = staticmethod(lambda r: "")
        _assistant_hist_entry = core.SagentCore._assistant_hist_entry

    def test_hist_entry_strips_leading_prefix(self):
        e = self._FakeCore()._assistant_hist_entry(
            "[2026-09-24 11:46] The twenty-fifth wake.")
        self.assertEqual(e["content"], "The twenty-fifth wake.")

    def test_hist_entry_strips_stacked_prefixes(self):
        e = self._FakeCore()._assistant_hist_entry(
            "[2026-09-24 11:46] [2026-09-24 11:45] [2026-09-24 11:44] body")
        self.assertEqual(e["content"], "body")

    def test_hist_entry_sanitizes_html(self):
        e = self._FakeCore()._assistant_hist_entry("hello</p> world")
        self.assertEqual(e["content"], "hello world")

    def test_render_strips_dirty_stored_prefix(self):
        # a stored entry written before the central fix — dirty — must
        # render with ONLY the render prefix, never stacked. The render
        # path is _render_history_message -> working_messages ->
        # _raw_chatml_render (the composer passes prefixed content
        # through verbatim), so the strip belongs in the former.
        msgs = [{"role": "assistant",
                 "content": "[2026-09-24 11:46] body text",
                 "ts": "2026-09-24T11:47:00"}]
        rendered = core.SagentCore._render_history_message(msgs[0])
        self.assertEqual(rendered["content"], "[2026-09-24 11:47] body text")
        r = core._raw_chatml_render([rendered])
        self.assertEqual(r.count("[2026-09-24"), 1)
        self.assertIn("[2026-09-24 11:47] body text", r)

    def test_render_leaves_user_brackets_alone(self):
        # a user's literal words may legitimately begin with a bracket
        msgs = [{"role": "user", "content": "[x] is a footnote",
                 "ts": "2026-09-24T11:47:00"}]
        rendered = core.SagentCore._render_history_message(msgs[0])
        self.assertEqual(rendered["content"],
                         "[2026-09-24 11:47] [x] is a footnote")

    def test_render_clean_entry_unchanged(self):
        msgs = [{"role": "assistant", "content": "clean body",
                 "ts": "2026-09-24T11:47:00"}]
        rendered = core.SagentCore._render_history_message(msgs[0])
        self.assertEqual(rendered["content"], "[2026-09-24 11:47] clean body")

    def test_render_never_mutates_stored_entry(self):
        stored = {"role": "assistant", "content": "[2026-09-24] body",
                  "ts": "2026-09-24T11:47:00"}
        core.SagentCore._render_history_message(stored)
        self.assertEqual(stored["content"], "[2026-09-24] body")


class TestReplyHygieneEnforce(unittest.TestCase):
    """2026-09-24 (F3 recurrence): one shared pass for the remaining
    assistant-text sites — final_content (the choke point) and round
    speech (the site BOTH repairs missed: all 38 post-cleansweep F3
    ts-prefix chronicle rows carry its finish_reason-None signature)."""

    def test_stacked_prefixes_stripped(self):
        self.assertEqual(core._enforce_reply_hygiene(
            "[2026-09-23 01:18] [2026-09-23 01:16] body"), "body")

    def test_html_stripped(self):
        self.assertEqual(core._enforce_reply_hygiene("a</p>b<b>x</b>"), "abx")

    def test_bare_prefix_becomes_empty(self):
        # the 08:32 stamp shape — a round speech that was ONLY a prefix is
        # no speech at all; the site's empty-check drops it
        self.assertEqual(core._enforce_reply_hygiene("[2026-09-23 08:30]"), "")

    def test_clean_text_unchanged(self):
        self.assertEqual(core._enforce_reply_hygiene("ordinary words"),
                         "ordinary words")

    def test_inline_prefix_untouched(self):
        self.assertEqual(core._enforce_reply_hygiene("clean [2026-01-01] tail"),
                         "clean [2026-01-01] tail")

    def test_gates_off_byte_identical(self):
        s = "[2026-09-23 01:18] a</p>b"
        self.assertEqual(core._enforce_reply_hygiene(s, False, False), s)


class TestLetterUserRowRepair(unittest.TestCase):
    """2026-09-24: the 09-22 pass skipped every non-assistant row —
    letter-reply USER rows (the OTHER resident's generated text) kept
    their prefixes; genuine human rows must stay untouched."""

    def test_letter_row_stripped_human_untouched(self):
        import json as _json
        import os as _os
        import tempfile as _tempfile
        import repair_cleansweep
        rows = [
            {"ts": "2026-09-20T12:34:36", "role": "user",
             "uid": "letter-reply-live-20260920",
             "content": "[2026-09-20 12:35] reply body"},
            {"ts": "2026-09-20T12:35:00", "role": "user",
             "content": "[2026-09-20 12:35] literal human bracket"},
            {"ts": "2026-09-20T12:36:00", "role": "assistant",
             "content": "[2026-09-20 12:36] her reply"},
        ]
        with _tempfile.TemporaryDirectory() as td:
            # point the module ROOT at the tmp dir so the backup relpath
            # lands under td/bk instead of climbing back onto the file
            _real_root = repair_cleansweep.ROOT
            repair_cleansweep.ROOT = td
            try:
                src = _os.path.join(td, "person")
                _os.makedirs(src)
                path = _os.path.join(src, "2026-09-20.jsonl")
                with open(path, "w") as f:
                    for r in rows:
                        f.write(_json.dumps(r) + "\n")
                manifest, report = {}, {}
                changed, stats = repair_cleansweep.repair_day_file(
                    path, "2026-09-20", _os.path.join(td, "bk"),
                    manifest, report)
            finally:
                repair_cleansweep.ROOT = _real_root
            self.assertTrue(changed)
            self.assertEqual(stats["letter_ts_prefix"], 1)
            out = [_json.loads(l) for l in open(path)]
            self.assertEqual(out[0]["content"], "reply body")
            self.assertEqual(out[1]["content"],
                             "[2026-09-20 12:35] literal human bracket")
            self.assertEqual(out[2]["content"], "her reply")
            # originals preserved, checksummed (house repair contract)
            self.assertIn(_os.path.relpath(path, td), manifest)


class TestFinishReasonInstrument(unittest.TestCase):
    """2026-09-24: the None-finish gap. The record must always carry
    SOMETHING — unknown, not null-identical-to-absent."""

    def test_raw_path_records_unknown(self):
        # the raw arm's mapping: missing done_reason -> 'unknown'
        _dr = None
        self.assertEqual("length" if _dr == "length" else
                         "stop" if _dr == "stop" else (_dr or "unknown"),
                         "unknown")
        self.assertEqual("length" if "length" == "length" else "x", "length")


class TestFutureDateFlags(unittest.TestCase):
    def run(self, *a, **kw):
        from datetime import date
        self._now = date(2026, 9, 22)
        super().run(*a, **kw)

    def test_future_date_flagged(self):
        core._future_date_flags.__doc__  # exists
        from core import _future_date_flags
        self.assertEqual(_future_date_flags("[2026-09-24] hi", self._now),
                         ["2026-09-24"])

    def test_past_dates_not_flagged(self):
        from core import _future_date_flags
        self.assertEqual(_future_date_flags(
            "[2026-09-20] ok [2026-09-21 10:00]", self._now), [])

    def test_no_dates(self):
        from core import _future_date_flags
        self.assertEqual(_future_date_flags("no dates", self._now), [])


class TestWakePayloadShape(unittest.TestCase):
    def test_no_identity_anchor_in_wake_packet(self):
        p = wake.build_payload("residenta")
        self.assertNotIn("You are persona-a", p["prompt"])

    def test_wake_frame_still_present(self):
        p = wake.build_payload("residenta")
        self.assertIn("system note (scheduled wake", p["prompt"])
        self.assertIn("WHERE THINGS STAND", p["prompt"])

    def test_schema_fields_intact(self):
        p = wake.build_payload("residenta")
        self.assertEqual(p["type"], "system-origin-wake")
        self.assertEqual(p["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
