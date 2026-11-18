"""window_probe_test.py — Phase A1 (memorybuild2 §5): mocked mismatch →
the warning; match → silent; kill switch; fail-open on fetch errors.
Run: python3 window_probe_test.py
"""
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import window_probe as wp   # noqa: E402

residentb_CFG = {"llm": {"chat_base_url": "http://127.0.0.1:8080/v1",
                     "total_context_tokens": 98304,
                     "sitting_num_ctx": 131072,
                     "model": "example-chat:latest"}}
VLLM_CFG = {"llm": {"chat_base_url": "http://127.0.0.1:8090/v1",
                    "total_context_tokens": 98304,
                    "sitting_num_ctx": 131072,
                    "model": "example-chat:latest"}}
residenta_CFG = {"llm": {"chat_base_url": "http://127.0.0.1:11434/v1",
                     "total_context_tokens": 32768,
                     "sitting_num_ctx": 32768,
                     "model": "testmodel-cpu:latest"}}
residentb_VLLM_CFG = {"llm": {"chat_base_url": "http://127.0.0.1:8090/v1",
                          "total_context_tokens": 131072,
                          "sitting_num_ctx": 131072,
                          "model": "example-chat:latest"}}


class ProbeTests(unittest.TestCase):
    def test_llamacpp_match_silent(self):
        fetch = lambda url, payload=None, timeout=5.0: {   # noqa: E731
            "default_generation_settings": {"n_ctx": 131072},
            "total_slots": 4}
        r = wp.probe_instance(residentb_CFG, fetch=fetch)
        self.assertEqual(r["warnings"], [])
        self.assertIn("n_ctx=131072", r["detail"])

    def test_llamacpp_sitting_mismatch_warns(self):
        fetch = lambda url, payload=None, timeout=5.0: {   # noqa: E731
            "default_generation_settings": {"n_ctx": 16384},
            "total_slots": 4}
        r = wp.probe_instance(residentb_CFG, fetch=fetch)
        self.assertTrue(any("sitting_num_ctx" in w for w in r["warnings"]))
        self.assertFalse(r["ok"])

    def test_llamacpp_budget_above_served_warns(self):
        cfg = {"llm": dict(residentb_CFG["llm"], total_context_tokens=200000)}
        fetch = lambda url, payload=None, timeout=5.0: {   # noqa: E731
            "default_generation_settings": {"n_ctx": 131072},
            "total_slots": 4}
        r = wp.probe_instance(cfg, fetch=fetch)
        self.assertTrue(any("total_context_tokens" in w
                            for w in r["warnings"]))

    def test_ollama_fits_silent(self):
        fetch = lambda url, payload=None, timeout=5.0: {   # noqa: E731
            "model_info": {"testmodel.context_length": 81920}}
        r = wp.probe_instance(residenta_CFG, fetch=fetch)
        self.assertEqual(r["warnings"], [])

    def test_ollama_too_small_warns(self):
        fetch = lambda url, payload=None, timeout=5.0: {   # noqa: E731
            "model_info": {"testmodel.context_length": 16384}}
        r = wp.probe_instance(residenta_CFG, fetch=fetch)
        self.assertTrue(len(r["warnings"]) == 2)   # total AND sitting

    def test_vllm_match_silent(self):
        # 2026-10-01 cutover: lab residentb moved to vLLM :8090; the probe reads
        # /v1/models max_model_len instead of llama.cpp /props.
        fetch = lambda url, payload=None, timeout=5.0: {   # noqa: E731
            "data": [{"id": "example-chat:latest", "max_model_len": 131072}]}
        r = wp.probe_instance(residentb_VLLM_CFG, fetch=fetch)
        self.assertEqual(r["warnings"], [])
        self.assertIn("max_model_len=131072", r["detail"])

    def test_vllm_too_small_warns(self):
        fetch = lambda url, payload=None, timeout=5.0: {   # noqa: E731
            "data": [{"id": "example-chat:latest", "max_model_len": 32768}]}
        r = wp.probe_instance(residentb_VLLM_CFG, fetch=fetch)
        self.assertTrue(len(r["warnings"]) == 2)   # total AND sitting

    def test_vllm_404_fails_open(self):
        def fetch(url, payload=None, timeout=5.0):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        r = wp.probe_instance(residentb_VLLM_CFG, fetch=fetch)
        self.assertTrue(r["ok"])
        self.assertIn("fail-open", r["detail"])

    def test_fail_open_on_fetch_error(self):
        def fetch(url, payload=None, timeout=5.0):
            raise ConnectionError("server down")
        r = wp.probe_instance(residentb_CFG, fetch=fetch)
        self.assertEqual(r["warnings"], [])
        self.assertIn("fail-open", r["detail"])

    def test_undeclared_window_is_noop(self):
        r = wp.probe_instance({"llm": {"chat_base_url": "http://x/v1"}},
                              fetch=lambda *a, **k: {})
        self.assertEqual(r["warnings"], [])

    def test_vllm_reads_max_model_len(self):
        """The lab's vllm branch (09-30/10-01 cutover): :8090 → /v1/models
        max_model_len, compared against the declared windows. The probe
        hits the /v1 ROOT (vllm serves /v1/models; /models 404s)."""
        calls = []
        def fetch(url, payload=None, timeout=5.0):
            calls.append(url)
            return {"data": [{"id": "example-chat:latest",
                              "max_model_len": 131072}]}
        r = wp.probe_instance(VLLM_CFG, fetch=fetch)
        self.assertEqual(r["warnings"], [])
        self.assertIn("max_model_len=131072", r["detail"])
        self.assertTrue(any(c.endswith("/v1/models") for c in calls),
                        calls)

    def test_vllm_mismatch_warns(self):
        def fetch(url, payload=None, timeout=5.0):
            return {"data": [{"id": "example-chat:latest",
                              "max_model_len": 65536}]}
        r = wp.probe_instance(VLLM_CFG, fetch=fetch)
        self.assertTrue(len(r["warnings"]) == 2)   # budget + sitting over

    def test_server_root_strips_v1(self):
        self.assertEqual(wp._server_root("http://h:8080/v1"),
                         "http://h:8080")
        self.assertEqual(wp._server_root("http://h:8080"),
                         "http://h:8080")

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"CONTINUA_WINDOW_PROBE": "0"}):
            self.assertFalse(wp.enabled())

    def test_probe_all_never_raises(self):
        wp.probe_all({"residentb": residentb_CFG, "residenta": residenta_CFG},
                     log=_NullLog())


class _NullLog:
    def log(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


if __name__ == "__main__":
    unittest.main()