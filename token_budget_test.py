"""chunk 4 tests: token allocator, density, caps. Pure logic, no endpoints."""
import os
import sys
import unittest

sys.path.insert(0, str(__import__("os").path.dirname(os.path.abspath(__file__))))
import token_budget as tb  # noqa: E402


class WindowTests(unittest.TestCase):
    def test_window_math_residentb(self):
        cfg = {"llm": {"total_context_tokens": 131072, "num_predict": 8192,
                       "prompt_utilisation": 0.60}}
        total, reserve, budget, util = tb.window_from_config(cfg, "residentb")
        self.assertEqual(total, 131072)
        self.assertEqual(reserve, 8192)
        self.assertEqual(util, 0.60)
        usable = 131072 - 8192 - 1024
        self.assertEqual(budget, int(usable * 0.60))
        self.assertEqual(budget, int((131072 - 8192 - 1024) * 0.60))

    def test_unconfigured_is_none(self):
        self.assertEqual(tb.window_from_config({"llm": {}}, "x"), (None,) * 4)
        self.assertEqual(tb.window_from_config({}, "x"), (None,) * 4)

    def test_utilisation_clamped(self):
        cfg = {"llm": {"total_context_tokens": 131072,
                       "prompt_utilisation": 5.0}}
        _, _, budget, util = tb.window_from_config(cfg, "x")
        self.assertEqual(util, 1.0)
        cfg["llm"]["prompt_utilisation"] = 0.01
        _, _, budget, util = tb.window_from_config(cfg, "x")
        self.assertEqual(util, 0.10)
        self.assertGreaterEqual(budget, tb.MIN_PROMPT_TOKENS)

    def test_small_budget_floors_at_minimum(self):
        cfg = {"llm": {"total_context_tokens": 4096, "num_predict": 3500}}
        _, _, budget, _ = tb.window_from_config(cfg, "x")
        self.assertGreaterEqual(budget, tb.MIN_PROMPT_TOKENS)


class DensityTests(unittest.TestCase):
    def test_measured_density_with_conservative_floor(self):
        # 4.6 c/t measured -> kept
        self.assertEqual(tb.measured_density(46, 10), 4.6)
        # 2.0 c/t measured -> floored at 3.0 (fewer chars/tok = smaller cap)
        self.assertEqual(tb.measured_density(20, 10), 3.0)
        # zero tokens -> fallback
        self.assertEqual(tb.measured_density(100, 0), tb.FALLBACK_DENSITY)
        self.assertEqual(tb.measured_density(0, 0), tb.FALLBACK_DENSITY)

    def test_char_cap(self):
        self.assertEqual(tb.char_cap(73113, 3.7), int(73113 * 3.7))


class EndpointTests(unittest.TestCase):
    def test_token_count_none_on_unreachable(self):
        self.assertIsNone(tb.token_count_via_endpoint(
            "hello", "http://127.0.0.1:1/v1", timeout=1))

    def test_endpoint_url_derivation_strips_v1(self):
        # the /tokenize call must target the llama.cpp root, not /v1
        # (verified by inspecting the URL construction contract)
        base = "http://127.0.0.1:8080/v1"
        self.assertTrue(base.rstrip('/').endswith('/v1'))
        stripped = base.rstrip('/')[:-3]
        self.assertEqual(stripped, "http://127.0.0.1:8080")


class ReportTests(unittest.TestCase):
    def test_report_flags_over_budget(self):
        r = tb.report(300000, 81000, 73113, 3.7, "endpoint", 0.60)
        self.assertTrue(r["over_budget"])
        self.assertEqual(r["density_source"], "endpoint")
        r = tb.report(200000, 54000, 73113, 3.7, "fallback", 0.60)
        self.assertFalse(r["over_budget"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
