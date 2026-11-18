"""history_curate_test.py — Phase 3 of helpnany.md (2026-09-28).

Compose-time history curation: collapse failed tool rounds, stub giant
pastes, mark unanswered gaps. Selection, never rewriting — every test also
asserts the INPUT list is unmutated and the store-shape bytes survive.

Pure, agent-free. Run:
  python3 history_curate_test.py
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import history_curate as hc  # noqa: E402

TEACH = ("[system] [Tool call NOT executed — {fn} came through with no "
         "'{p}'. You invoked it with empty arguments; the call needs the "
         "real content. The correct format is EXACTLY: "
         "<call><function>{fn}</function>"
         '<parameter name="{p}">value</parameter></call> '
         "Re-issue with what you meant to do.")


def _teach_round(fn="search_my_memories", p="query"):
    return {"role": "user", "content": TEACH.format(fn=fn, p=p)}


class TestCollapse(unittest.TestCase):
    def test_three_identical_rounds_collapse_to_one_plus_last(self):
        msgs = [{"role": "user", "content": "hi"},
                _teach_round(), _teach_round(), _teach_round(),
                {"role": "assistant", "content": "ok"}]
        out, n = hc.collapse_failed_tool_rounds(msgs)
        self.assertEqual(n, 2)
        self.assertEqual(len(out), 4)  # was 5
        self.assertIn("3 consecutive failed tool rounds", out[1]["content"])
        self.assertIn("search_my_memories", out[1]["content"])
        self.assertIn("missing 'query'", out[1]["content"])
        # last round verbatim
        self.assertEqual(out[2], msgs[3])

    def test_input_never_mutated(self):
        msgs = [{"role": "user", "content": "hi"},
                _teach_round(), _teach_round()]
        before = json.dumps(msgs)
        hc.collapse_failed_tool_rounds(msgs)
        self.assertEqual(json.dumps(msgs), before)

    def test_single_failure_never_collapsed(self):
        msgs = [_teach_round()]
        out, n = hc.collapse_failed_tool_rounds(msgs)
        self.assertEqual(n, 0)
        self.assertEqual(out, msgs)

    def test_run_broken_by_assistant_never_joined(self):
        msgs = [_teach_round(),
                {"role": "assistant", "content": "retrying"},
                _teach_round()]
        out, n = hc.collapse_failed_tool_rounds(msgs)
        self.assertEqual(n, 0)
        self.assertEqual(len(out), 3)

    def test_flag_off_passthrough(self):
        msgs = [_teach_round(), _teach_round()]
        out, n = hc.collapse_failed_tool_rounds(msgs, enabled=False)
        self.assertEqual(n, 0)
        self.assertEqual(out, msgs)


class TestStub(unittest.TestCase):
    def _paste(self, chars=9183):
        body = "".join(f"plan line {i:04d}: attack failure family "
                       f"#{i % 5} next.\n" for i in range(chars // 44))
        return body[:chars]

    def test_giant_paste_stubbed(self):
        paste = self._paste(9183)
        msgs = [{"role": "user", "content": paste},
                {"role": "assistant", "content": "reviewed"}]
        out, n = hc.stub_giant_pastes(msgs, paste_stub_chars=2000)
        self.assertEqual(n, 1)
        c = out[0]["content"]
        self.assertLess(len(c), 1200, "stub must be small")
        self.assertIn("9183 chars", c)
        self.assertIn("Alex pasted a document", c)
        self.assertIn("your shell (sed -n / tail -c) / deep_recall", c)
        self.assertTrue(c.startswith("plan line 0000"))
        self.assertIn(paste[-60:], c)  # tail verbatim

    def test_small_conversation_never_stubbed(self):
        msgs = [{"role": "user", "content": "Do you want to talk?"},
                {"role": "user", "content": "hello my friend"}]
        out, n = hc.stub_giant_pastes(msgs, paste_stub_chars=2000)
        self.assertEqual(n, 0)
        self.assertEqual(out, msgs)

    def test_teaching_lines_never_stubbed(self):
        long_teach = {"role": "user", "content": _teach_round()["content"]
                      + " " * 3000}
        msgs = [long_teach]
        out, n = hc.stub_giant_pastes(msgs, paste_stub_chars=2000)
        self.assertEqual(n, 0)

    def test_idempotent(self):
        paste = self._paste(9183)
        msgs = [{"role": "user", "content": paste}]
        out, _ = hc.stub_giant_pastes(msgs, paste_stub_chars=2000)
        out2, n = hc.stub_giant_pastes(out, paste_stub_chars=2000)
        self.assertEqual(n, 0)
        self.assertEqual(out2, out)

    def test_input_never_mutated(self):
        msgs = [{"role": "user", "content": self._paste(9183)}]
        before = json.dumps(msgs)
        hc.stub_giant_pastes(msgs, paste_stub_chars=2000)
        self.assertEqual(json.dumps(msgs), before)


class TestUnanswered(unittest.TestCase):
    def test_consecutive_users_marked(self):
        msgs = [{"role": "user", "content": "Hello my friend"},
                {"role": "user", "content": "we have made some changes"}]
        out, n = hc.mark_unanswered(msgs)
        self.assertEqual(n, 1)
        # one marker per RUN, at the run's head: marker first, then both users
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("no reply was composed", out[0]["content"])
        self.assertEqual(out[1], msgs[0])
        self.assertEqual(out[2], msgs[1])

    def test_answered_pairs_unmarked(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "bye"}]
        out, n = hc.mark_unanswered(msgs)
        self.assertEqual(n, 0)
        self.assertEqual(len(out), 3)


class TestCompose(unittest.TestCase):
    def test_curate_full_pipeline_stats(self):
        msgs = [{"role": "user", "content": "Hello my friend"},
                {"role": "user", "content": "x" * 3000},
                _teach_round(), _teach_round(), _teach_round(),
                {"role": "assistant", "content": "done"}]
        out, stats = hc.curate(msgs, paste_stub_chars=2000)
        self.assertEqual(stats["collapsed_rounds"], 2)
        self.assertEqual(stats["stubbed_pastes"], 1)
        # the raw teaching rounds carry ~430 chars of template each —
        # collapse keeps ONE verbatim, so the dominant win is the paste:
        # assert the paste's 3000 chars became a <1200 stub
        self.assertLess(
            sum(len(m.get("content") or "") for m in out),
            sum(len(m.get("content") or "") for m in msgs) - 1500,
            "net shrink must exceed 1500 chars (the paste stub alone)")
        # original untouched
        self.assertEqual(len(msgs), 6)

    def test_empty_history_passthrough(self):
        out, stats = hc.curate([])
        self.assertEqual(out, [])
        self.assertEqual(stats["collapsed_rounds"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
