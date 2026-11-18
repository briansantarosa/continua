"""Offline final-facing budget tests: tool groups retained, no source mutation."""
import copy
import unittest
import context_budget as b


class BudgetTests(unittest.TestCase):
    def test_unchanged_under_cap(self):
        messages = [{'role': 'system', 'content': 'identity'}, {'role': 'user', 'content': 'hello'}]
        result, info = b.fit(messages, 1000)
        self.assertEqual(result, messages)
        self.assertEqual(info['dropped'], 0)

    def test_evict_whole_old_exchange(self):
        messages = [{'role': 'system', 'content': 'identity'},
                    {'role': 'user', 'content': 'old' * 1000},
                    {'role': 'assistant', 'content': 'call', 'tool_calls': [{'id': 'a'}]},
                    {'role': 'tool', 'content': 'result', 'tool_call_id': 'a'},
                    {'role': 'user', 'content': 'new'},
                    {'role': 'assistant', 'content': 'new call'},
                    {'role': 'tool', 'content': 'new result'}]
        original = copy.deepcopy(messages)
        result, info = b.fit(messages, 1000)
        self.assertEqual(result, [messages[0]] + messages[4:])
        self.assertEqual(messages, original)
        self.assertEqual(info['dropped'], 3)

    def test_cannot_truncate_current_request(self):
        with self.assertRaises(b.ContextBudgetExceeded):
            b.fit([{'role': 'system', 'content': 'fixed'}, {'role': 'user', 'content': 'new'}], 10)

    def test_tools_counted(self):
        with self.assertRaises(b.ContextBudgetExceeded):
            b.fit([{'role': 'user', 'content': 'new'}], 1000, tools=[{'description': 'x' * 1000}])


if __name__ == '__main__':
    unittest.main()


class ImageAwareTests(unittest.TestCase):
    """house ruling 2026-09-20 (the photo crash): image blocks are billed as
    vision tiles (~1.2K tok floor, real photos 1.5–3K), never as their
    base64 length. A ~240K-char data URI counted as text blew the request
    cap and crashed the turn before any LLM call."""

    def _photo_msg(self, b64_len=240_000):
        return [{'role': 'system', 'content': 'x' * 4000},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': 'what is this?'},
                    {'type': 'image_url', 'image_url': {
                        'url': 'data:image/jpeg;base64,' + 'A' * b64_len}}]}]

    def test_photo_exchange_passes_cap_that_base64_would_blow(self):
        # the 19:52 crash repro: cap 270,518 (residentb's derived cap),
        # a ~240K-char base64 block — must NOT raise
        msgs = self._photo_msg(240_000)
        result, info = b.fit(msgs, 270_518,
                             tools=[{'name': 'x', 'schema': 'y' * 20_000}])
        self.assertEqual(info['dropped'], 0)
        self.assertEqual(result, msgs)  # the real blocks ride raw (Sagent parity)
        # and the honest accounting is visible in the budget line
        self.assertLess(info['image_blocks_honest'], 0)

    def test_image_accounting_real_vs_honest(self):
        msgs = self._photo_msg(100_000)
        real, honest = b.image_accounting(msgs)
        self.assertGreater(real, 100_000)   # the serialized data URI
        self.assertLessEqual(honest, 9_000)  # ≈2,500 vision tokens as chars
        self.assertGreater(real - honest, 90_000)

    def test_huge_text_exchange_still_evicts_then_raises(self):
        # text is unaffected: eviction works when the CURRENT exchange is
        # small, and an unevictable huge current exchange still raises
        # (protecting it is by design)
        msgs = [{'role': 'system', 'content': 'fixed'},
                {'role': 'user', 'content': 'old' * 50_000},
                {'role': 'assistant', 'content': 'ok'},
                {'role': 'user', 'content': 'new'}]
        result, info = b.fit(msgs, 60_000)
        self.assertEqual(info['dropped'], 2)
        with self.assertRaises(b.ContextBudgetExceeded):
            b.fit([{'role': 'system', 'content': 'fixed'},
                   {'role': 'user', 'content': 'new' * 50_000}], 60_000)

    def test_no_ceiling_reports_honest(self):
        msgs = self._photo_msg(50_000)
        _, info = b.fit(msgs, 0)
        self.assertIn('image_blocks_honest', info)
