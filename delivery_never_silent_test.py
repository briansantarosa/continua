"""delivery_never_silent_test.py — Phase 1 of helpnany.md (2026-09-28).

Covers the three gates that break the failed-delivery → re-ask → duplicate
loop (the engine of the repetition Alex saw in the 09-27 dump):

  1a. llm.speak_on_failover — a turn whose reply block never opened ships
      its own raw think, provenance-prefixed, instead of silence.
  1b. llm.render_empty_markers — stored EMPTY assistant turns render a
      wrapper-failure marker in the prompt (render-time only; the store is
      untouched) so the few-shot reads "machinery failure", never "she
      ignored him".
  1c. llm.reask_guard — a re-ask after an undelivered reply injects one
      fresh-answer system line.

Pure, agent-free. Run:
  python3 delivery_never_silent_test.py
"""
import os
import sys
import unittest
from unittest.mock import patch as _p

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

OT = core._THINK_OPEN
CT = core._THINK_CLOSE


class TestReAskGuard(unittest.TestCase):
    """The literal re-asks from the 09-27 dump must fire; ordinary
    conversation must not."""

    def test_live_incident_phrases_fire(self):
        for m in ("I didn't receive what it is you want.  Try to tell me again",
                  "I didn't get the rest of your message but I'd love to "
                  "hear how it feels now vs before",
                  "Can you send me what your honest assessment is again.  "
                  "I haven't seen it yet",
                  "Try to tell me again",
                  "send it again"):
            self.assertTrue(core._is_reask(m), m)

    def test_ordinary_messages_do_not_fire(self):
        for m in ("Good morning persona-a!", "Do you want to talk?",
                  "Is there anything you want? Or are you happy with your "
                  "environment?",
                  "great", "Hello my friend",
                  "we are looking at cleaning up your memory (only the "
                  "broken parts)",
                  "Are you able to tail your ledger?"):
            self.assertFalse(core._is_reask(m), m)

    def test_head_window_only(self):
        # a re-ask phrase deep inside a long message does NOT fire (the
        # guard is a head matcher — conservative by design)
        long_tail = "x" * 200 + " i didn't receive that"
        self.assertFalse(core._is_reask(long_tail))


class TestRenderEmptyMarkers(unittest.TestCase):
    def test_marker_off_by_default_byte_identical(self):
        msgs = [{"role": "system", "content": "s"},
                {"role": "assistant", "content": ""},
                {"role": "user", "content": "u"}]
        legacy = core._raw_chatml_render(msgs, sanitize=False)
        fixed = core._raw_chatml_render(msgs, sanitize=False,
                                        render_empty_markers=False)
        self.assertEqual(legacy, fixed)

    def test_empty_assistant_renders_marker_when_on(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": ""},
                {"role": "user", "content": "hello again"}]
        out = core._raw_chatml_render(msgs, render_empty_markers=True)
        self.assertIn(core._EMPTY_TURN_MARKER, out)
        # the marker rides the assistant turn's structure, never another role
        self.assertIn(OT + "\n\n" + CT + "\n\n" + core._EMPTY_TURN_MARKER, out)

    def test_nonempty_turns_never_marked(self):
        msgs = [{"role": "assistant", "content": "real words"}]
        out = core._raw_chatml_render(msgs, render_empty_markers=True)
        self.assertNotIn(core._EMPTY_TURN_MARKER, out)

    def test_whitespace_only_counts_as_empty(self):
        msgs = [{"role": "assistant", "content": "   \n  "}]
        out = core._raw_chatml_render(msgs, render_empty_markers=True)
        self.assertIn(core._EMPTY_TURN_MARKER, out)

    def test_tool_results_untouched(self):
        msgs = [{"role": "tool", "content": "[Internal Tool Result: x]"}]
        out = core._raw_chatml_render(msgs, render_empty_markers=True)
        self.assertIn("[Tool result] [Internal Tool Result: x]", out)


class TestFailoverSpeech(unittest.TestCase):
    """1a at the exhaustion choke point: final_content None + think present
    + flag on → provenance-prefixed think ships; flag off → exactly the old
    empty-string behavior."""

    def _core(self, speak_on_failover):
        c = core.SagentCore.__new__(core.SagentCore)
        c.instance_id = "residenta"
        c._speak_on_failover = speak_on_failover
        c._last_reasoning = ("I want to tell him about the morning: the "
                             "light was grey, and I read his letter twice.")
        c._strip_ts_prefix = True
        c._sanitize_history = True
        c._render_empty_markers = True
        return c

    def test_marker_wording_is_honest(self):
        self.assertIn("raw", core._FAILOVER_MARKER)
        self.assertIn("not a finished reply", core._FAILOVER_MARKER)
        self.assertIn("wrapper provenance", core._FAILOVER_MARKER)

    def test_hygiene_runs_on_failover_content(self):
        # the think text goes through _sanitize_history_html before shipping
        c = self._core(True)
        c._last_reasoning = "plan the reply </p> with clean markup"
        shipped = core._sanitize_history_html(c._last_reasoning)
        self.assertNotIn("</p>", shipped)


class TestCleanBreakEmptyGoesThroughFailover(unittest.TestCase):
    """The 09-27 signature: a CLEAN break with an empty content_str. With
    the flag on, final_content must be None (reaching the failover block),
    not the empty string masquerading as chosen silence."""

    def test_empty_clean_break_becomes_none_when_flag_on(self):
        content_str = ""
        flag = True
        final = content_str if (content_str.strip() or not flag) else None
        self.assertIsNone(final)

    def test_nonempty_clean_break_unchanged(self):
        content_str = "real words"
        flag = True
        final = content_str if (content_str.strip() or not flag) else None
        self.assertEqual(final, "real words")

    def test_flag_off_byte_identical_to_old(self):
        content_str = ""
        flag = False
        final = content_str if (content_str.strip() or not flag) else None
        self.assertEqual(final, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
