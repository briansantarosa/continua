"""tier_probe_test.py — P1 (fixtoolcalls.md §2.1) tier assignment tests.

Agent-free: the fetch is mocked; no network, no model.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tier_probe as tp


def _cfg(base="http://127.0.0.1:8090/v1", model="m", raw=False, pin=None,
         tools=True):
    llm = {"chat_base_url": base, "model": model}
    if raw:
        llm["raw_chatml"] = True
    if pin:
        llm["tier"] = pin
    return {"llm": llm, "tools": ({"save_my_memory": {}} if tools else {})}


def _native_response():
    return {"choices": [{"message": {"role": "assistant", "content": "",
            "tool_calls": [{"id": "x", "type": "function",
                            "function": {"name": "save_my_memory",
                                         "arguments": "{\"content\": \"p\"}"}}]}}]}


class TestTierAssignment(unittest.TestCase):
    def test_tier_a_native_roundtrip(self):
        calls = []
        def fetch(url, payload=None, timeout=8.0, api_key=None):
            calls.append(url)
            if url.endswith("/tokenize"):
                return {"tokens": [1, 2, 3]}
            if url.endswith("/chat/completions"):
                return _native_response()
            raise AssertionError(f"unexpected url {url}")
        r = tp.probe_instance(_cfg(), fetch=fetch)
        self.assertEqual(r["tier"], "A")
        self.assertEqual(r["dialect"], "native")
        self.assertEqual(r["template_hash"], tp._sha16(json.dumps([1, 2, 3])))
        self.assertFalse(r["pinned"])

    def test_native_fail_template_renders_means_tier_b(self):
        def fetch(url, payload=None, timeout=8.0, api_key=None):
            if url.endswith("/chat/completions"):
                return {"choices": [{"message": {"role": "assistant",
                        "content": "I would save it.", "tool_calls": None}}]}
            if url.endswith("/apply-template"):
                return {"prompt": "<tools>{...}</tools>\n<tool_call><function=f>"}
            raise AssertionError(url)
        r = tp.probe_instance(_cfg(base="http://127.0.0.1:8199/v1"), fetch=fetch)
        self.assertEqual(r["tier"], "B")
        self.assertEqual(r["dialect"], "qwen-xml")

    def test_native_fail_no_template_evidence_means_tier_c(self):
        def fetch(url, payload=None, timeout=8.0, api_key=None):
            if url.endswith("/chat/completions"):
                return {"choices": [{"message": {"role": "assistant",
                        "content": "sure", "tool_calls": None}}]}
            if url.endswith("/apply-template"):
                return {"prompt": "system\nuser\nassistant"}  # no tool markup
            raise AssertionError(url)
        r = tp.probe_instance(_cfg(base="http://127.0.0.1:8199/v1"), fetch=fetch)
        self.assertEqual(r["tier"], "C")

    def test_raw_chatml_caps_below_a_even_on_capable_backend(self):
        # the raw composer is the serving path: the native channel is never
        # consulted, no matter how capable the backend is — and with no
        # template tool-markup evidence, it stays C
        def fetch(url, payload=None, timeout=8.0, api_key=None):
            if url.endswith("/chat/completions"):
                raise AssertionError("raw path must never probe native")
            if url.endswith("/apply-template"):
                return {"prompt": "system\nuser\nassistant"}  # no markup
            raise AssertionError(url)
        r = tp.probe_instance(_cfg(base="http://127.0.0.1:8199/v1", raw=True),
                              fetch=fetch)
        self.assertEqual(r["tier"], "C")
        self.assertEqual(r["dialect"], "house-raw")

    def test_raw_chatml_with_template_markup_is_tier_b(self):
        def fetch(url, payload=None, timeout=8.0, api_key=None):
            if url.endswith("/apply-template"):
                return {"prompt": "<tools>{...}</tools>"}
            raise AssertionError(url)
        r = tp.probe_instance(_cfg(base="http://127.0.0.1:8199/v1", raw=True),
                              fetch=fetch)
        self.assertEqual(r["tier"], "B")

    def test_config_pin_honored_without_generation(self):
        gen_calls = []
        def fetch(url, payload=None, timeout=8.0, api_key=None):
            calls = url.endswith("/chat/completions")
            if calls:
                gen_calls.append(url)
            if url.endswith("/tokenize"):
                return {"tokens": [9]}
            raise AssertionError(url)
        r = tp.probe_instance(_cfg(pin="A"), fetch=fetch)
        self.assertEqual(r["tier"], "A")
        self.assertTrue(r["pinned"])
        self.assertEqual(gen_calls, [])   # pin: assignment without generation

    def test_no_tools_declared_skips(self):
        r = tp.probe_instance(_cfg(tools=False), fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
        self.assertEqual(r["tier"], "-")

    def test_failopen_on_errors(self):
        def fetch(url, payload=None, timeout=8.0, api_key=None):
            raise ConnectionError("server down")
        r = tp.probe_instance(_cfg(), fetch=fetch)
        self.assertEqual(r["tier"], "C")   # fails open to the scaffolded tier
        self.assertIn("fail", " ".join(r["evidence"]).lower()
                      + r["detail"].lower())


class TestStateAndChurn(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = tp.STATE_DIR
        tp.STATE_DIR = self._tmp.name

    def tearDown(self):
        tp.STATE_DIR = self._old
        self._tmp.cleanup()

    def test_state_roundtrip_and_current_for(self):
        tp.save_state("residentb", {"tier": "A", "dialect": "native",
                                "template_hash": "abc123"})
        st = tp.load_state("residentb")
        self.assertEqual(st["tier"], "A")
        self.assertIn("probed_at", st)
        cur = tp.current_for("residentb")
        self.assertEqual(cur, {"tier": "A", "dialect": "native",
                               "template_hash": "abc123"})

    def test_current_for_missing_resident_fails_open(self):
        self.assertEqual(tp.current_for("nobody"), {"tier": None,
                                                    "dialect": None,
                                                    "template_hash": None})

    def test_churn_logs_warning_and_rerecords(self):
        tp.save_state("residentb", {"tier": "A", "dialect": "native",
                                "template_hash": "OLD"})
        logs = []
        class _L:
            def info(self, *a, **k): pass
            def warning(self, *a, **k): logs.append(a[0] % a[1:] if len(a) > 1 else a[0])
        def fetch(url, payload=None, timeout=8.0, api_key=None):
            if url.endswith("/tokenize"):
                return {"tokens": [1, 2, 3]}
            return _native_response()
        cfgs = {"residentb": _cfg()}
        tp.probe_all(cfgs, log=_L(), fetch=fetch)
        self.assertTrue(any("TEMPLATE CHANGED" in l for l in logs))
        self.assertEqual(tp.load_state("residentb")["template_hash"],
                         tp._sha16(json.dumps([1, 2, 3])))

    def test_stable_hash_no_warning(self):
        tp.save_state("residentb", {"tier": "A", "dialect": "native",
                                "template_hash": tp._sha16(json.dumps([1, 2, 3]))})
        logs = []
        class _L:
            def info(self, *a, **k): pass
            def warning(self, *a, **k):
                try: logs.append(a[0] % a[1:] if len(a) > 1 else a[0])
                except Exception: logs.append(str(a))
        tp.probe_all({"residentb": _cfg()}, log=_L())
        self.assertFalse(any("TEMPLATE CHANGED" in str(a) for a in logs))


if __name__ == "__main__":
    unittest.main()
