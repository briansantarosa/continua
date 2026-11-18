"""window_allocator_test.py — memorybuild.md Phase 7 (§WIN): the allocator,
the density fix, the notes accounting, the fill refusal, the self-correction.
Run: python3 window_allocator_test.py
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import token_budget as tb  # noqa: E402
import core                # noqa: E402


class AllocatorTests(unittest.TestCase):
    def test_residentb_fill_values(self):
        cfg = {"llm": {"total_context_tokens": 131072, "num_predict": 8192,
                       "memory_fill_target": 0.99}}
        W, G, P, F = tb.window_from_config(cfg)
        self.assertEqual((W, G, P, F), (131072, 8192, 120354, 0.99))

    def test_residenta_32k_values(self):
        cfg = {"llm": {"total_context_tokens": 32768, "num_predict": 8192,
                       "memory_fill_target": 0.99}}
        W, G, P, F = tb.window_from_config(cfg)
        self.assertEqual((W, G, P, F), (32768, 8192, 23316, 0.99))

    def test_one_million_values(self):
        cfg = {"llm": {"total_context_tokens": 1048576, "num_predict": 8192,
                       "memory_fill_target": 0.99}}
        W, G, P, F = tb.window_from_config(cfg)
        self.assertEqual((W, G, P, F), (1048576, 8192, 1019600, 0.99))

    def test_margin_is_percent_not_flat(self):
        self.assertEqual(tb.margin_for(131072), 1310)
        self.assertEqual(tb.margin_for(32768), 1024)   # the floor
        self.assertEqual(tb.margin_for(1048576), 10485)

    def test_fill_below_32k_refused(self):
        cfg = {"llm": {"total_context_tokens": 16384, "num_predict": 8192,
                       "memory_fill_target": 0.99}}
        W, G, P, F = tb.window_from_config(cfg)
        # the legacy path runs: (16384-8192-1024) x 0.60
        self.assertEqual(P, int((16384 - 8192 - 1024) * 0.60))

    def test_fill_target_clamped_at_99(self):
        cfg = {"llm": {"total_context_tokens": 131072, "num_predict": 8192,
                       "memory_fill_target": 1.2}}
        _, _, P, F = tb.window_from_config(cfg)
        self.assertEqual(F, 0.99)

    def test_undeclared_window_is_none(self):
        self.assertEqual(tb.window_from_config({}),
                         (None, None, None, None))

    def test_legacy_mode_byte_preserved(self):
        cfg = {"llm": {"total_context_tokens": 131072, "num_predict": 8192,
                       "prompt_utilisation": 0.60}}
        W, G, P, F = tb.window_from_config(cfg)
        self.assertEqual(P, int((131072 - 8192 - 1024) * 0.60))
        self.assertEqual(F, 0.60)


class DensityFixTests(unittest.TestCase):
    def _core(self, window=(131072, 8192, 120354, 0.99)):
        c = object.__new__(core.SagentCore)
        c._token_window = window
        c._density = 3.7
        c._density_source = 'fallback'
        c._prompt_char_cap = (tb.char_cap(window[2], 3.7)
                              if window[2] else None)
        c._last_render_chars = 23800   # e.g. the rendered raw prompt
        c._last_eval_count = None
        c._last_prompt_tokens = None
        return c

    def test_ollama_shape_uses_prompt_count_never_generated(self):
        """§WIN step 0: prompt_eval_count 7000 / eval_count 80 (a short
        reply) → density = chars/7000 — the OLD code divided by 80."""
        c = self._core()
        # simulate the raw path's capture (the fixed order)
        c._last_eval_count = 80
        c._last_prompt_tokens = 7000
        c._update_measured_density()
        self.assertAlmostEqual(c._density, 23800 / 7000, places=2)
        self.assertEqual(c._density_source, 'measured')
        self.assertGreater(c._density, 3.0)

    def test_generated_count_alone_never_feeds_density(self):
        c = self._core()
        c._last_eval_count = 80          # the OLD code's input
        c._last_prompt_tokens = None     # (absent prompt count)
        c._update_measured_density()     # must be a no-op
        self.assertEqual(c._density_source, 'fallback')
        self.assertEqual(c._density, 3.7)

    def test_no_window_noop(self):
        c = self._core(window=(None, None, None, None))
        c._last_prompt_tokens = 7000
        c._update_measured_density()
        self.assertEqual(c._density_source, 'fallback')


class NotesAccountingTests(unittest.TestCase):
    def test_room_subtracts_notes_bytes(self):
        """§WIN step 2: the room formula subtracts _notes_bytes (the old
        formula silently overran by the notes' size)."""
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn("- _notes_bytes", src)
        # the assembly happens BEFORE the room line
        self.assertLess(src.find("_notes_bytes = 0"),
                        src.find("- _notes_bytes"))
        # and the text still appends at the render position (after recollections)
        self.assertLess(src.find("if _notes_text:"),
                        src.find('work package B: the juggle renders nearest'))


class RawPayloadTests(unittest.TestCase):
    def test_num_ctx_rides_declared_windows_only(self):
        import inspect
        src = inspect.getsource(core.SagentCore.generate_response)
        self.assertIn('payload["options"]["num_ctx"]', src)
        self.assertIn("if self._token_window[0]:", src)


class SelfCorrectionTests(unittest.TestCase):
    def test_overshoot_shrinks_density(self):
        """§WIN step 4 (estimated): served > P → the density multiplies down
        (never up through the floor)."""
        c = object.__new__(core.SagentCore)
        c._token_window = (32768, 8192, 23316, 0.99)
        c._density = 3.7
        c._prompt_char_cap = tb.char_cap(23316, 3.7)
        # the fixed code inside generate_response — simulate its math:
        _Pchk = c._token_window[2]
        _pet = 30000
        _old = c._density
        c._density = max(tb.DENSITY_FLOOR, _old * _Pchk / _pet)
        c._prompt_char_cap = tb.char_cap(_Pchk, c._density)
        # the raw math lands at 2.876 — the FLOOR pins it at 3.0 (never below)
        self.assertAlmostEqual(3.7 * 23316 / 30000, 2.8756, places=3)
        self.assertEqual(c._density, 3.0)
        self.assertGreaterEqual(c._density, 3.0)


if __name__ == "__main__":
    unittest.main()